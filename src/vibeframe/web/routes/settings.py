from __future__ import annotations

from datetime import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from vibeframe.db import set_setting
from vibeframe.web.deps import AppState, get_state, require_token

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
async def view_settings(request: Request, state: AppState = Depends(get_state)):
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {"s": state.settings},
    )


def _parse_time(value: str) -> time:
    hh, mm = value.split(":", 1)
    return time(int(hh), int(mm))


@router.post("", dependencies=[Depends(require_token)])
async def update_settings(
    request: Request,
    state: AppState = Depends(get_state),
    orientation: int = Form(...),
    refresh_seconds: int = Form(...),
    selection_mode: str = Form(...),
    dither: str = Form(...),
    crop_mode: str = Form(...),
    saturation: float = Form(...),
    contrast: float = Form(...),
    quiet_start: str = Form(...),
    quiet_end: str = Form(...),
):
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
