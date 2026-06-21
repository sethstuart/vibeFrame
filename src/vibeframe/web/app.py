from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from vibeframe.web.deps import AppState
from vibeframe.web.routes import favorites, images, system
from vibeframe.web.routes import settings as settings_routes

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="vibeFrame", version="0.1.0")
    app.state.app_state = state
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(images.router)
    app.include_router(favorites.router)
    app.include_router(settings_routes.router)
    app.include_router(system.router)

    return app
