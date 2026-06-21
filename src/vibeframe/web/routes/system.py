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


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, state: AppState = Depends(get_state)):
    last_path = state.scheduler.last_path
    last_id = None
    if last_path:
        for img in state.library.list(limit=200):
            if img.path == last_path:
                last_id = img.id
                break
    now_local = datetime.now(tz=state.settings.zoneinfo)
    return request.app.state.templates.TemplateResponse(
        request,
        "home.html",
        {
            "last_path": last_path,
            "last_id": last_id,
            "last_shown_at": state.scheduler.last_shown_at,
            "in_quiet": is_quiet(now_local, state.settings.quiet_start, state.settings.quiet_end),
            "s": state.settings,
        },
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
