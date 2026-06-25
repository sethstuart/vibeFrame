from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import uvicorn

from vibeframe.cache import Cache
from vibeframe.config import get_settings
from vibeframe.db import build_engine
from vibeframe.display import build_driver
from vibeframe.library import ImageLibrary
from vibeframe.progress import RenderTracker
from vibeframe.scheduler import Scheduler
from vibeframe.thumb_warmer import ThumbWarmer
from vibeframe.watcher import LibraryWatcher
from vibeframe.web.app import create_app
from vibeframe.web.deps import AppState

log = logging.getLogger("vibeframe")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _serve() -> None:
    settings = get_settings()
    _setup_logging(settings.log_level)
    settings.ensure_dirs()

    engine = build_engine(settings.db_path)
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
