from __future__ import annotations

from datetime import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from vibeframe.db import set_setting
from vibeframe.web.deps import AppState, get_state, require_token

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
async def view_settings(request: Request, state: AppState = Depends(get_state)):
    # Prefer the currently-displayed image — its pipeline cache is already
    # warm so the live preview loads instantly and updates fast.
    last_path = state.scheduler.last_path
    preview_id: int | None = None
    if last_path:
        from pathlib import Path

        from sqlmodel import Session, select

        from vibeframe.db import Image as DbImage

        with Session(state.engine) as session:
            preview_id = session.exec(
                select(DbImage.id).where(DbImage.path == str(Path(last_path)))
            ).first()
    if preview_id is None:
        recent = state.library.list(limit=1)
        preview_id = recent[0].id if recent else None
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {"s": state.settings, "preview_id": preview_id},
    )


def _parse_time(value: str) -> time:
    hh, mm = value.split(":", 1)
    return time(int(hh), int(mm))


@router.post("", dependencies=[Depends(require_token)])
async def update_settings(
    request: Request,
    state: AppState = Depends(get_state),
    orientation: int = Form(...),
    refresh_minutes: int = Form(...),
    selection_mode: str = Form(...),
    dither: str = Form(...),
    crop_mode: str = Form(...),
    saturation: float = Form(...),
    contrast: float = Form(...),
    quiet_start: str = Form(...),
    quiet_end: str = Form(...),
):
    refresh_seconds = max(60, int(refresh_minutes) * 60)
    s = state.settings
    s.orientation = orientation  # type: ignore[assignment]
    s.refresh_seconds = refresh_seconds
    s.selection_mode = selection_mode  # type: ignore[assignment]
    s.dither = dither  # type: ignore[assignment]
    s.crop_mode = crop_mode  # type: ignore[assignment]
    s.saturation = saturation
    s.contrast = contrast
    s.quiet_start = _parse_time(quiet_start)
    s.quiet_end = _parse_time(quiet_end)

    for k, v in {
        "orientation": str(orientation),
        "refresh_seconds": str(refresh_seconds),
        "selection_mode": selection_mode,
        "dither": dither,
        "crop_mode": crop_mode,
        "saturation": str(saturation),
        "contrast": str(contrast),
        "quiet_start": quiet_start,
        "quiet_end": quiet_end,
    }.items():
        set_setting(state.engine, k, v)

    return RedirectResponse(url="/settings", status_code=303)
