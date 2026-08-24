from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

import uvicorn

from vibeframe.cache import Cache
from vibeframe.config import Settings, get_settings
from vibeframe.db import build_engine, get_setting
from vibeframe.display import build_driver
from vibeframe.library import ImageLibrary
from vibeframe.processor import dither as dither_mod
from vibeframe.processor import pipeline as pipeline_mod
from vibeframe.progress import RenderTracker
from vibeframe.scheduler import Scheduler
from vibeframe.thumb_warmer import ThumbWarmer
from vibeframe.watcher import LibraryWatcher
from vibeframe.web.app import create_app
from vibeframe.web.deps import AppState

log = logging.getLogger("vibeframe")


def _restore_persisted_settings(settings: Settings, engine) -> None:
    """Overlay DB-persisted setting values onto the env-driven defaults
    so changes made via the web UI survive a container restart."""
    from datetime import time as _time

    def _parse_time(value: str) -> _time:
        hh, mm = value.split(":", 1)
        return _time(int(hh), int(mm))

    def _parse_bool(value: str) -> bool:
        return value.strip().lower() in ("1", "true", "yes", "on")

    fields = [
        ("orientation", int),
        ("refresh_seconds", int),
        ("selection_mode", str),
        ("dither", str),
        ("crop_mode", str),
        ("saturation", float),
        ("contrast", float),
        ("quiet_hours_enabled", _parse_bool),
        ("quiet_start", _parse_time),
        ("quiet_end", _parse_time),
        ("metrics_refresh_seconds", int),
        ("cache_max_bytes", int),
        ("backup_keep", int),
    ]
    for name, parse in fields:
        raw = get_setting(engine, name)
        if raw is None:
            continue
        try:
            setattr(settings, name, parse(raw))
        except Exception as e:
            log.warning("ignoring bad persisted setting %s=%r: %s", name, raw, e)


def _setup_logging(level: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )


async def _serve() -> None:
    settings = get_settings()
    _setup_logging(settings.log_level, settings.log_path)
    settings.ensure_dirs()

    engine = build_engine(settings.db_path)
    _restore_persisted_settings(settings, engine)
    # Reject decompression-bomb images before any decode (scan, thumb warm, or
    # a render) can exhaust memory on the 4 GB Pi.
    pipeline_mod.configure_pillow(settings.max_image_pixels)
    # Pre-compile numba-JIT'd dither inner loops while the rest of the app is
    # still booting. Subsequent process starts hit the on-disk cache (~10 ms).
    dither_mod.prewarm()
    cache = Cache(settings.cache_dir, settings.cache_max_bytes)
    library = ImageLibrary(settings.photos_dir, engine, recursive=settings.recursive, cache=cache)
    library.scan()

    warmer = ThumbWarmer(settings, library)
    warmer.start()

    watcher = LibraryWatcher(library, on_change=warmer.kick)
    watcher.start()

    driver = build_driver(settings)
    scheduler = Scheduler(settings, library, cache, driver, engine)
    state = AppState(
        settings=settings,
        library=library,
        cache=cache,
        scheduler=scheduler,
        driver=driver,
        engine=engine,
        preview_tracker=RenderTracker(),
    )
    app = create_app(state)

    config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()

    def _handle_signal():
        log.info("shutdown requested")
        scheduler.stop()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal)

    scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
    try:
        await server.serve()
    finally:
        scheduler.stop()
        await asyncio.gather(scheduler_task, return_exceptions=True)
        watcher.stop()
        warmer.stop()


def main() -> int:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
