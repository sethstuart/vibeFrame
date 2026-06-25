from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from vibeframe.cache import Cache
from vibeframe.config import Settings
from vibeframe.db import record_show
from vibeframe.display.base import DisplayDriver
from vibeframe.library import ImageLibrary
from vibeframe.processor.pipeline import process
from vibeframe.timing import record, timed

log = logging.getLogger(__name__)


def is_quiet(now: datetime, start: time, end: time) -> bool:
    """True if `now` falls within the quiet window (handles wrap-around midnight)."""
    t = now.time()
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def _pick_next(
    library: ImageLibrary,
    mode: str,
    last_path: str | None,
) -> int | None:
    if mode == "favorites":
        ids = library.all_ids(favorites_only=True)
        if not ids:
            ids = library.all_ids()
    elif mode == "recent":
        ids = library.recent_ids(limit=50)
    else:
        ids = library.all_ids()
    if not ids:
        return None

    if mode == "sequential":
        sorted_imgs = library.list(limit=10_000)
        if not sorted_imgs:
            return None
        ordered = sorted(sorted_imgs, key=lambda i: i.path)
        paths = [i.path for i in ordered]
        idx = (paths.index(last_path) + 1) % len(paths) if last_path in paths else 0
        return ordered[idx].id

    return random.choice(ids)


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        library: ImageLibrary,
        cache: Cache,
        driver: DisplayDriver,
        engine,
    ) -> None:
        self.settings = settings
        self.library = library
        self.cache = cache
        self.driver = driver
        self.engine = engine
        self.kick = asyncio.Event()
        self._last_path: str | None = None
        self._last_shown_at: datetime | None = None
        self._stop = asyncio.Event()
        # One-shot override consulted by _pick_next. Cleared after use.
        self._next_override: int | None = None
        self._busy = False
        self._next_due_at: datetime | None = None

    async def run(self) -> None:
        log.info(
            "scheduler running: every %ss, mode=%s, quiet=%s..%s %s",
            self.settings.refresh_seconds,
            self.settings.selection_mode,
            self.settings.quiet_start,
            self.settings.quiet_end,
            self.settings.tz,
        )
        while not self._stop.is_set():
            await self._step()
            self._next_due_at = datetime.now(UTC) + _td_seconds(self.settings.refresh_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.kick.wait(), timeout=self.settings.refresh_seconds)
            self.kick.clear()

    async def _step(self) -> None:
        from time import perf_counter

        step_start = perf_counter()
        tz: ZoneInfo = self.settings.zoneinfo
        now_local = datetime.now(tz=tz)
        if is_quiet(now_local, self.settings.quiet_start, self.settings.quiet_end):
            log.debug("inside quiet hours; skipping refresh")
            return

        with timed("scheduler.pick_next"):
            if self._next_override is not None:
                image_id = self._next_override
                self._next_override = None
            else:
                image_id = _pick_next(self.library, self.settings.selection_mode, self._last_path)
        if image_id is None:
            log.info("no images available to display")
            return
        img = self.library.get(image_id)
        if img is None:
            return

        self._busy = True
        try:
            loop = asyncio.get_running_loop()
            processed = await loop.run_in_executor(
                None, process, Path(img.path), self.settings, self.cache, img.sha256
            )
            await loop.run_in_executor(None, self.driver.show, processed.image)
        except Exception:
            log.exception("failed to render/show image %s", img.path)
            self._busy = False
            return
        self._busy = False

        record_show(self.engine, image_id)
        self._last_path = img.path
        self._last_shown_at = datetime.now(UTC)
        record("scheduler.step.total", perf_counter() - step_start)
        log.info("displayed %s", img.path)

    def stop(self) -> None:
        self._stop.set()
        self.kick.set()

    def show_now(self, image_id: int) -> None:
        """Queue a specific image to be the next refresh and kick the loop."""
        self._next_override = image_id
        self.kick.set()

    @property
    def last_path(self) -> str | None:
        return self._last_path

    @property
    def last_shown_at(self) -> datetime | None:
        return self._last_shown_at

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def next_due_at(self) -> datetime | None:
        return self._next_due_at


def _td_seconds(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)
