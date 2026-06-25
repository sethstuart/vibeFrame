from __future__ import annotations

import io
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from vibeframe.library import IMAGE_EXTS
from vibeframe.processor.pipeline import process
from vibeframe.thumb_warmer import generate_thumb, thumb_cache_path
from vibeframe.web.deps import AppState, get_state, require_token

THUMB_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}

router = APIRouter(prefix="/images", tags=["images"])


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


@router.get("", response_class=HTMLResponse)
async def list_images(
    request: Request,
    favorites_only: bool = False,
    limit: int = PAGE_SIZE_DEFAULT,
    offset: int = 0,
    state: AppState = Depends(get_state),
):
    limit = max(1, min(limit, PAGE_SIZE_MAX))
    offset = max(0, offset)
    total = state.library.count(favorites_only=favorites_only)
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    current_page = offset // limit + 1
    images = state.library.list(limit=limit, offset=offset, favorites_only=favorites_only)
    favorite_ids = set(state.library.all_ids(favorites_only=True))
    return request.app.state.templates.TemplateResponse(
        request,
        "images.html",
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
        },
    )


def _save_one_upload(file: UploadFile, target_dir: Path) -> tuple[Path | None, str | None]:
    """Save a single upload. Returns (target_path, error_message)."""
    name = Path(file.filename or "").name or "upload"
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_EXTS:
        return None, f"{name}: unsupported file type ({suffix or 'no extension'})"
    safe_name = f"{int(time.time() * 1000)}-{name}"
    target = target_dir / safe_name
    try:
        with target.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    except OSError as e:
        return None, f"{name}: write failed ({e})"
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
        path, err = _save_one_upload(f, target_dir)
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
    try:
        Path(img.path).unlink(missing_ok=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    state.library.remove_path(Path(img.path))
    return {"deleted": image_id}


@router.get("/{image_id}/preview.png")
def preview(image_id: int, state: AppState = Depends(get_state)):
    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    processed = process(Path(img.path), state.settings, state.cache, img.sha256)
    buf = io.BytesIO()
    processed.image.convert("RGB").save(buf, format="PNG")
    return Response(
        content=buf.getvalue(), media_type="image/png", headers=THUMB_CACHE_HEADERS
    )


@router.get("/{image_id}/thumb.png")
def thumb(image_id: int, state: AppState = Depends(get_state)):
    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    src_path = Path(img.path)
    try:
        cached = thumb_cache_path(state.settings, src_path)
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
