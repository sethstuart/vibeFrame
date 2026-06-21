from __future__ import annotations

import hashlib
import io
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from vibeframe.library import IMAGE_EXTS
from vibeframe.processor.pipeline import process
from vibeframe.web.deps import AppState, get_state, require_token

THUMB_MAX_SIDE = 320
THUMB_QUALITY = 80
THUMB_CACHE_HEADERS = {"Cache-Control": "public, max-age=86400"}

router = APIRouter(prefix="/images", tags=["images"])


@router.get("", response_class=HTMLResponse)
async def list_images(
    request: Request,
    favorites_only: bool = False,
    limit: int = 60,
    offset: int = 0,
    state: AppState = Depends(get_state),
):
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
        },
    )


@router.post("/upload", dependencies=[Depends(require_token)])
def upload(
    file: UploadFile = File(...),
    state: AppState = Depends(get_state),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported file type: {suffix}")
    target_dir = state.settings.upload_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{int(time.time())}-{Path(file.filename or 'upload').name}"
    target = target_dir / safe_name
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    state.library.add_path(target)
    return {"path": str(target)}


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
    processed = process(Path(img.path), state.settings, state.cache)
    buf = io.BytesIO()
    processed.image.convert("RGB").save(buf, format="PNG")
    return Response(
        content=buf.getvalue(), media_type="image/png", headers=THUMB_CACHE_HEADERS
    )


def _thumb_cache_path(state: AppState, src: Path) -> Path:
    stat = src.stat()
    key = hashlib.sha256(f"{src}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()
    return state.settings.cache_dir / "thumbs" / f"{key}.jpg"


@router.get("/{image_id}/thumb.png")
def thumb(image_id: int, state: AppState = Depends(get_state)):
    from PIL import Image as PILImage
    from PIL import ImageOps

    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    src_path = Path(img.path)
    cached = _thumb_cache_path(state, src_path)
    if cached.is_file():
        return Response(
            content=cached.read_bytes(),
            media_type="image/jpeg",
            headers=THUMB_CACHE_HEADERS,
        )

    with PILImage.open(src_path) as src:
        src = ImageOps.exif_transpose(src).convert("RGB")
        src.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        src.save(buf, format="JPEG", quality=THUMB_QUALITY)

    data = buf.getvalue()
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    except OSError:
        pass
    return Response(content=data, media_type="image/jpeg", headers=THUMB_CACHE_HEADERS)
