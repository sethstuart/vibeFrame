from __future__ import annotations

import io
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageDraw

from vibeframe.processor.palette import SPECTRA6
from vibeframe.scheduler import is_quiet
from vibeframe.web.deps import AppState, get_state, require_token

router = APIRouter(tags=["system"])


def _now_showing_context(state: AppState) -> dict:
    last_path = state.scheduler.last_path
    last_id = None
    if last_path:
        # Resolve the current path to its image id via a single direct DB
        # lookup rather than scanning the recent list.
        from pathlib import Path

        from vibeframe.db import Image as DbImage
        from sqlmodel import Session, select

        with Session(state.engine) as session:
            row = session.exec(
                select(DbImage.id).where(DbImage.path == str(Path(last_path)))
            ).first()
            if row is not None:
                last_id = row
    now_local = datetime.now(tz=state.settings.zoneinfo)
    last_shown_at = state.scheduler.last_shown_at
    return {
        "last_path": last_path,
        "last_id": last_id,
        "last_shown_at": last_shown_at,
        # Used as a cache-busting query string on the thumb so the browser
        # actually re-fetches when the image changes.
        "last_shown_ts": int(last_shown_at.timestamp()) if last_shown_at else 0,
        "in_quiet": is_quiet(now_local, state.settings.quiet_start, state.settings.quiet_end),
        "s": state.settings,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, state: AppState = Depends(get_state)):
    return request.app.state.templates.TemplateResponse(
        request, "home.html", _now_showing_context(state)
    )


@router.get("/system/now-showing", response_class=HTMLResponse)
async def now_showing_fragment(request: Request, state: AppState = Depends(get_state)):
    """HTMX-polled fragment rendering only the current-image block."""
    return request.app.state.templates.TemplateResponse(
        request, "_now_showing.html", _now_showing_context(state)
    )


@router.get("/healthz")
async def healthz():
    return {"ok": True}


@router.post("/system/next", dependencies=[Depends(require_token)])
async def trigger_next(state: AppState = Depends(get_state)):
    state.scheduler.kick.set()
    return {"queued": True}


def _build_test_pattern(orientation: int) -> Image.Image:
    w, h = 800, 480
    if orientation in (90, 270):
        w, h = h, w
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    n = len(SPECTRA6)
    bar_w = w // n
    for i, color in enumerate(SPECTRA6):
        draw.rectangle([i * bar_w, 0, (i + 1) * bar_w, h], fill=color)
    return img


@router.get("/system/test-pattern.png")
def test_pattern(state: AppState = Depends(get_state)):
    img = _build_test_pattern(state.settings.orientation)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.post("/system/test-pattern", dependencies=[Depends(require_token)])
def show_test_pattern(state: AppState = Depends(get_state)):
    img = _build_test_pattern(state.settings.orientation)
    state.driver.show(img)
    return {"shown": True}
