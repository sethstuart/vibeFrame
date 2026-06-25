from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from vibeframe.cache import Cache
from vibeframe.config import Settings
from vibeframe.display.base import DisplayDriver
from vibeframe.library import ImageLibrary
from vibeframe.progress import RenderTracker
from vibeframe.scheduler import Scheduler


@dataclass
class AppState:
    settings: Settings
    library: ImageLibrary
    cache: Cache
    scheduler: Scheduler
    driver: DisplayDriver
    engine: object
    preview_tracker: RenderTracker


def get_state(request: Request) -> AppState:
    return request.app.state.app_state  # type: ignore[no-any-return]


def require_token(request: Request, x_vibeframe_token: str | None = Header(default=None)) -> None:
    state: AppState = request.app.state.app_state
    expected = state.settings.web_token
    if not expected:
        return
    if x_vibeframe_token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing token")
