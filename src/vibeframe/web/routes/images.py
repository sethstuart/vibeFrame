from __future__ import annotations

import contextlib
import io
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from vibeframe.db import Image as DBImage
from vibeframe.db import get_collection, list_collections
from vibeframe.library import IMAGE_EXTS
from vibeframe.processor.pipeline import cached_png_bytes, process
from vibeframe.thumb_warmer import generate_thumb, thumb_cache_path
from vibeframe.timing import timed
from vibeframe.web.deps import AppState, get_state, require_token

THUMB_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}

router = APIRouter(prefix="/images", tags=["images"])

log = logging.getLogger("vibeframe")


PAGE_SIZE_DEFAULT = 24
PAGE_SIZE_MAX = 200


def _page_numbers(current: int, total: int, window: int = 2) -> list[int | None]:
    """Numbered pagination with ellipses (None) for gaps. window = neighbours each side."""
    if total <= 1:
        return [1] if total == 1 else []
    pages: set[int] = {1, total, current}
    for d in range(1, window + 1):
        pages.add(current - d)
        pages.add(current + d)
    ordered = sorted(p for p in pages if 1 <= p <= total)
    out: list[int | None] = []
    prev = 0
    for p in ordered:
        if p != prev + 1 and prev != 0:
            out.append(None)
        out.append(p)
        prev = p
    return out


def _get_source(state: AppState, image_id: int) -> tuple[DBImage, Path]:
    """Fetch a library row and resolve its path into the library root.

    404s if the row is missing or escapes photos_dir (e.g. via symlink), so no
    route can read or delete a file outside the photos directory through a
    stored record."""
    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    src = state.library.safe_path(img.path)
    if src is None:
        raise HTTPException(status_code=404, detail="not found")
    return img, src


def _next_partial_url(
    offset: int,
    limit: int,
    favorites_only: bool,
    q: str | None,
    sort: str,
    collection_id: int | None = None,
) -> str:
    """URL the infinite-scroll sentinel fetches for the page after this one.

    Built here rather than in the template so the query string goes through
    urlencode — the pager's Jinja macro interpolates `q` raw, which a search
    term containing & would break."""
    params: dict[str, str] = {"offset": str(offset + limit), "limit": str(limit), "partial": "true"}
    if favorites_only:
        params["favorites_only"] = "true"
    if collection_id is not None:
        params["collection_id"] = str(collection_id)
    if q:
        params["q"] = q
    if sort and sort != "newest":
        params["sort"] = sort
    return f"/images?{urlencode(params)}"


@router.get("", response_class=HTMLResponse)
async def list_images(
    request: Request,
    favorites_only: bool = False,
    limit: int = PAGE_SIZE_DEFAULT,
    offset: int = 0,
    q: str | None = None,
    sort: str = "newest",
    partial: bool = False,
    collection_id: int | None = None,
    state: AppState = Depends(get_state),
):
    limit = max(1, min(limit, PAGE_SIZE_MAX))
    offset = max(0, offset)
    # A collection that has since been deleted would otherwise render an empty
    # grid with no explanation; fall back to the whole library instead.
    active_collection = (
        get_collection(state.engine, collection_id) if collection_id is not None else None
    )
    if active_collection is None:
        collection_id = None
    total = state.library.count(favorites_only=favorites_only, collection_id=collection_id)
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    current_page = offset // limit + 1
    images = state.library.list(
        limit=limit,
        offset=offset,
        favorites_only=favorites_only,
        query=q,
        sort=sort,
        collection_id=collection_id,
    )
    favorite_ids = set(state.library.all_ids(favorites_only=True))
    next_partial = (
        _next_partial_url(offset, limit, favorites_only, q, sort, collection_id)
        if current_page < total_pages
        else None
    )
    # partial=true returns the cards alone (plus the next sentinel), which is
    # what the mobile infinite scroll appends. Same template either way, so an
    # appended card can't drift from a first-page one.
    return request.app.state.templates.TemplateResponse(
        request,
        "_photo_cards.html" if partial else "images.html",
        {
            "images": images,
            "favorite_ids": favorite_ids,
            "favorites_only": favorites_only,
            "offset": offset,
            "limit": limit,
            "total": total,
            "current_page": current_page,
            "total_pages": total_pages,
            "page_numbers": _page_numbers(current_page, total_pages),
            "q": q or "",
            "sort": sort,
            "next_partial_url": next_partial,
            "collections": list_collections(state.engine),
            "active_collection": active_collection,
        },
    )


def _save_one_upload(
    file: UploadFile, target_dir: Path, max_bytes: int
) -> tuple[Path | None, str | None]:
    """Save one uploaded file into the photos dir. Returns (target_path, error).

    Streams in 1 MB chunks with a hard byte cap so an unauthenticated client
    can't fill the disk; a short uuid suffix keeps same-millisecond uploads
    distinct."""
    name = Path(file.filename or "").name or "upload"
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_EXTS:
        return None, f"{name}: unsupported file type ({suffix or 'no extension'})"
    target = target_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}-{name}"
    written = 0
    try:
        with timed("nfs.write"), target.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    break
                out.write(chunk)
    except OSError as e:
        return None, f"{name}: write failed ({e})"
    if written > max_bytes:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        return None, f"{name}: exceeds {max_bytes // (1024 * 1024)} MB upload limit"
    return target, None


@router.post("/upload", dependencies=[Depends(require_token)])
def upload(
    request: Request,
    files: list[UploadFile] = File(..., description="One or more image files"),
    state: AppState = Depends(get_state),
):
    target_dir = state.settings.upload_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    errors: list[str] = []
    for f in files:
        max_bytes = state.settings.max_upload_mb * 1024 * 1024
        path, err = _save_one_upload(f, target_dir, max_bytes)
        if path:
            state.library.add_path(path)
            saved.append(path.name)
        elif err:
            errors.append(err)

    if request.headers.get("HX-Request"):
        return request.app.state.templates.TemplateResponse(
            request, "_upload_result.html", {"saved": saved, "errors": errors}
        )
    return {"saved": saved, "errors": errors}


@router.delete("/{image_id}", dependencies=[Depends(require_token)])
def delete_image(image_id: int, state: AppState = Depends(get_state)):
    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    target = state.library.safe_path(img.path)
    if target is None:
        log.warning("delete_image: refusing id %s — path escapes library root", image_id)
        raise HTTPException(status_code=404, detail="not found")
    try:
        Path(target).unlink(missing_ok=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    state.library.remove_path(Path(img.path))
    return {"deleted": image_id}


@router.get("/{image_id}/preview.png")
def preview(image_id: int, state: AppState = Depends(get_state)):
    img, src = _get_source(state, image_id)
    # Fast path: the scheduler already filled the pipeline cache when it last
    # rendered this image. Skip the PIL decode + re-encode and stream the
    # cached PNG straight to the client.
    fast = cached_png_bytes(src, state.settings, state.cache, img.sha256)
    if fast is not None:
        return Response(
            content=fast, media_type="image/png", headers=THUMB_CACHE_HEADERS
        )
    processed = process(src, state.settings, state.cache, img.sha256)
    buf = io.BytesIO()
    processed.image.convert("RGB").save(buf, format="PNG")
    return Response(
        content=buf.getvalue(), media_type="image/png", headers=THUMB_CACHE_HEADERS
    )


@router.post("/{image_id}/show", dependencies=[Depends(require_token)])
def show_now(image_id: int, state: AppState = Depends(get_state)):
    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    state.scheduler.show_now(image_id)
    return {"queued": image_id}


def _parse_ids(payload: dict) -> list[int]:
    """Coerce the JSON `ids` field to a list of ints, returning 422 (not 500)
    on a malformed body."""
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(status_code=422, detail="ids must be a list")
    try:
        return [int(i) for i in ids]
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail="ids must be integers") from e


@router.post("/bulk/favorite", dependencies=[Depends(require_token)])
def bulk_favorite(payload: dict, state: AppState = Depends(get_state)):
    ids = _parse_ids(payload)
    favorited = bool(payload.get("favorited", True))
    n = state.library.bulk_favorite(ids, favorited)
    return {"changed": n}


@router.post("/bulk/delete", dependencies=[Depends(require_token)])
def bulk_delete(payload: dict, state: AppState = Depends(get_state)):
    ids = _parse_ids(payload)
    n = state.library.bulk_delete(ids)
    return {"deleted": n}


# Deliberately not behind require_token. The settings page loads this as an
# <img> src, and an <img> cannot send the X-Vibeframe-Token header the check
# reads — gating it would break the live preview for anyone who sets a token.
# Making the token usable from the browser at all is a separate piece of work
# (nothing in the frontend sends that header for any route today).
@router.get("/{image_id}/render-with.png")
def render_with(
    image_id: int,
    dither: str | None = None,
    crop_mode: str | None = None,
    saturation: float | None = None,
    contrast: float | None = None,
    orientation: int | None = None,
    state: AppState = Depends(get_state),
):
    """Preview the pipeline using ad-hoc settings (no DB write, no cache write
    on miss — caller passes hypotheticals for the Settings page slider preview).
    """
    img, src = _get_source(state, image_id)
    # Build a transient Settings copy with any overrides applied.
    base = state.settings.model_dump()
    if dither is not None:
        base["dither"] = dither
    if crop_mode is not None:
        base["crop_mode"] = crop_mode
    if saturation is not None:
        base["saturation"] = saturation
    if contrast is not None:
        base["contrast"] = contrast
    if orientation is not None:
        base["orientation"] = orientation
    from pydantic import ValidationError as _ValidationError

    from vibeframe.config import Settings as _Settings

    try:
        transient = _Settings(**base)
    except _ValidationError as e:
        # Out-of-range slider values are bad client input, not a server fault.
        raise HTTPException(status_code=422, detail=str(e)) from e
    state.preview_tracker.start(image_id, img.path)
    try:
        # Pass the real cache so the rendered PNG is written under the cache
        # key derived from these (proposed) settings. After the user saves,
        # state.settings carries the same values, /preview.png hits this
        # cache key, and the "before" image updates without re-rendering.
        processed = process(
            src,
            transient,
            cache=state.cache,
            sha256=img.sha256,
            tracker=state.preview_tracker,
        )
    except Exception as e:
        state.preview_tracker.mark_failed(repr(e))
        raise
    buf = io.BytesIO()
    processed.image.convert("RGB").save(buf, format="PNG")
    state.preview_tracker.mark_done()
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/{image_id}/source-cropped.jpg")
def source_cropped(image_id: int, state: AppState = Depends(get_state)):
    """Full-quality JPEG of the source image cropped to mirror the panel's
    composition. Uses the same EXIF-transpose + crop step as the dither
    pipeline (no tonemap, no dither, no palette quantize). Drives the home
    page hero so the user sees a sharp, full-colour reference of what's on
    the panel — not the dithered render itself."""
    from PIL import Image as PILImage
    from PIL import ImageOps

    from vibeframe.processor import crop as crop_mod
    from vibeframe.processor.pipeline import _target_size

    _img, src = _get_source(state, image_id)

    target_w, target_h = _target_size(state.settings.orientation)
    with timed("source_cropped"):
        with PILImage.open(src) as raw:
            # Decode large JPEGs at a reduced scale — same rationale as the
            # render pipeline. The hero only displays at panel size, so a full
            # 12 MP decode is wasted. draft() never upscales and no-ops on
            # non-JPEG, so the crop still has ample resolution to downscale from.
            hint = max(target_w, target_h)
            raw.draft("RGB", (hint, hint))
            oriented = ImageOps.exif_transpose(raw).convert("RGB")
        cropped = crop_mod.crop_to(
            oriented, target_w, target_h, state.settings.crop_mode
        )
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers=THUMB_CACHE_HEADERS,
    )


@router.get("/{image_id}/full.jpg")
def full_image(image_id: int, state: AppState = Depends(get_state)):
    """Full-aspect (uncropped) EXIF-corrected JPEG of the source image,
    downscaled to a sane max so the lightbox shows the whole photo — not the
    panel-cropped composition that source-cropped.jpg / preview.png use."""
    from PIL import Image as PILImage
    from PIL import ImageOps

    _img, src = _get_source(state, image_id)
    with timed("full_preview"), PILImage.open(src) as raw:
        # Decode at a reduced scale (same rationale as the render pipeline);
        # draft() never upscales and no-ops on non-JPEG sources.
        raw.draft("RGB", (1600, 1600))
        oriented = ImageOps.exif_transpose(raw).convert("RGB")
        oriented.thumbnail((1600, 1600), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        oriented.save(buf, format="JPEG", quality=85)
    return Response(
        content=buf.getvalue(), media_type="image/jpeg", headers=THUMB_CACHE_HEADERS
    )


@router.get("/{image_id}/thumb.png")
def thumb(image_id: int, state: AppState = Depends(get_state)):
    img, src_path = _get_source(state, image_id)
    try:
        # Key on the stored path, not the resolved one: thumb_cache_path hashes
        # the path string, and ThumbWarmer warms with the DB path. Resolving
        # here would give a different key and miss every warmed thumb whenever
        # photos_dir contains a symlinked component.
        cached = thumb_cache_path(state.settings, Path(img.path))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if cached.is_file():
        return Response(
            content=cached.read_bytes(),
            media_type="image/jpeg",
            headers=THUMB_CACHE_HEADERS,
        )
    data = generate_thumb(src_path)
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError:
        pass
    return Response(content=data, media_type="image/jpeg", headers=THUMB_CACHE_HEADERS)
