from __future__ import annotations

import io
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from vibeframe.library import IMAGE_EXTS
from vibeframe.processor.pipeline import process
from vibeframe.web.deps import AppState, get_state, require_token

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
        "images.html",
        {
            "request": request,
            "images": images,
            "favorite_ids": favorite_ids,
            "favorites_only": favorites_only,
            "offset": offset,
            "limit": limit,
        },
    )


@router.post("/upload", dependencies=[Depends(require_token)])
async def upload(
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
async def delete_image(image_id: int, state: AppState = Depends(get_state)):
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
async def preview(image_id: int, state: AppState = Depends(get_state)):
    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    processed = process(Path(img.path), state.settings, state.cache)
    buf = io.BytesIO()
    processed.image.convert("RGB").save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/{image_id}/thumb.png")
async def thumb(image_id: int, state: AppState = Depends(get_state)):
    from PIL import Image as PILImage
    from PIL import ImageOps

    img = state.library.get(image_id)
    if not img:
        raise HTTPException(status_code=404, detail="not found")
    with PILImage.open(img.path) as src:
        src = ImageOps.exif_transpose(src).convert("RGB")
        src.thumbnail((320, 320), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        src.save(buf, format="JPEG", quality=80)
    return Response(content=buf.getvalue(), media_type="image/jpeg")
