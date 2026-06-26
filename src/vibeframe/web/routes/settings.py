from __future__ import annotations

import time
from datetime import time as dtime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from vibeframe.db import set_setting
from vibeframe.web.deps import AppState, get_state, require_token

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
async def view_settings(
    request: Request,
    saved: int = 0,
    pushable: int = 0,
    state: AppState = Depends(get_state),
):
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
        {
            "s": state.settings,
            "preview_id": preview_id,
            "saved": bool(saved),
            # Only render-affecting saves produce a new panel image worth
            # pushing, so only they raise the "push to frame?" prompt.
            "pushable": bool(pushable),
            # Cache-buster for the current-img <img src> so the browser
            # re-fetches /preview.png after a save (the underlying cache
            # file's contents change but the URL is the same).
            "ts": int(time.time()),
        },
    )


def _parse_time(value: str) -> dtime:
    hh, mm = value.split(":", 1)
    return dtime(int(hh), int(mm))


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
    quiet_hours_enabled: bool = Form(False),
    quiet_start: str = Form(...),
    quiet_end: str = Form(...),
    metrics_refresh_seconds: int = Form(...),
    cache_max_mb: int = Form(...),
):
    refresh_seconds = max(60, int(refresh_minutes) * 60)
    metrics_refresh_seconds = max(1, int(metrics_refresh_seconds))
    cache_max_bytes = max(1, int(cache_max_mb)) * 1024 * 1024
    s = state.settings
    # Only a change to a render-affecting setting produces a new panel render
    # worth pushing — so only those should raise the "push to frame?" prompt.
    # Schedule/UI settings (refresh interval, selection mode, quiet hours,
    # metrics refresh) save silently.
    render_changed = (
        orientation != s.orientation
        or crop_mode != s.crop_mode
        or dither != s.dither
        or saturation != s.saturation
        or contrast != s.contrast
    )
    s.orientation = orientation  # type: ignore[assignment]
    s.refresh_seconds = refresh_seconds
    s.selection_mode = selection_mode  # type: ignore[assignment]
    s.dither = dither  # type: ignore[assignment]
    s.crop_mode = crop_mode  # type: ignore[assignment]
    s.saturation = saturation
    s.contrast = contrast
    s.quiet_hours_enabled = quiet_hours_enabled
    s.quiet_start = _parse_time(quiet_start)
    s.quiet_end = _parse_time(quiet_end)
    s.metrics_refresh_seconds = metrics_refresh_seconds
    s.cache_max_bytes = cache_max_bytes
    # The live Cache holds its own copy of the cap; update it and enforce a
    # lowered limit immediately rather than waiting for the next write.
    state.cache.max_bytes = cache_max_bytes
    state.cache.evict_if_needed()

    for k, v in {
        "orientation": str(orientation),
        "refresh_seconds": str(refresh_seconds),
        "selection_mode": selection_mode,
        "dither": dither,
        "crop_mode": crop_mode,
        "saturation": str(saturation),
        "contrast": str(contrast),
        "quiet_hours_enabled": "1" if quiet_hours_enabled else "0",
        "quiet_start": quiet_start,
        "quiet_end": quiet_end,
        "metrics_refresh_seconds": str(metrics_refresh_seconds),
        "cache_max_bytes": str(cache_max_bytes),
    }.items():
        set_setting(state.engine, k, v)

    url = "/settings?saved=1&pushable=1" if render_changed else "/settings?saved=1"
    return RedirectResponse(url=url, status_code=303)


@router.post("/clear-cache", dependencies=[Depends(require_token)])
async def clear_cache(state: AppState = Depends(get_state)):
    """Delete cached renders to free disk, keeping the image currently on the
    panel so the home hero's panel-render link and the live-preview "before"
    stay instant (no re-render hitch right after clearing)."""
    keep: set[str] = set()
    current = state.scheduler.last_path
    if not current:
        # Nothing fully shown yet (e.g. just booted / mid-first-refresh) — fall
        # back to whatever is being rendered right now.
        current = state.scheduler.refresh_tracker.snapshot().get("image_path")
    if current:
        img = state.library.get_by_path(current)
        if img is not None:
            keep.add(img.sha256)
    return state.cache.clear(keep_sources=keep)
