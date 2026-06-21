from __future__ import annotations

from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from vibeframe.timing import record
from vibeframe.web.deps import AppState
from vibeframe.web.routes import favorites, images, metrics, system
from vibeframe.web.routes import settings as settings_routes

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="vibeFrame", version="0.1.0")
    app.state.app_state = state
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.middleware("http")
    async def timing_middleware(request: Request, call_next):
        start = perf_counter()
        response = await call_next(request)
        # Use the matched route path so {image_id} doesn't blow up cardinality.
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path) if route else request.url.path
        record(f"http.{request.method}.{path}", perf_counter() - start)
        return response

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(images.router)
    app.include_router(favorites.router)
    app.include_router(settings_routes.router)
    app.include_router(system.router)
    app.include_router(metrics.router)

    return app
