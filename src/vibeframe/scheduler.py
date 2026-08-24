from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from vibeframe.cache import Cache
from vibeframe.config import Settings, collection_mode_id
from vibeframe.db import effective_weight, least_recently_shown, list_collections, record_show
from vibeframe.display.base import DisplayDriver
from vibeframe.library import ImageLibrary
from vibeframe.processor.pipeline import process
from vibeframe.progress import RenderTracker
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


def _pick_least_shown(engine) -> int | None:
    """The image that has gone longest without being displayed.

    This is the mode that guarantees you eventually see the whole library
    instead of the same handful. Ties -- which is every image on a fresh
    install, where nothing has been shown -- are broken at random so the first
    pass through is not just scan order.
    """
    rows = least_recently_shown(engine)
    if not rows:
        return None
    oldest = rows[0][1]
    tied = [image_id for image_id, last in rows if last == oldest]
    return random.choice(tied) if tied else rows[0][0]


def _pick_weighted(library: ImageLibrary, engine, today: date) -> int | None:
    """Weighted shuffle across collections.

    Each collection is a pool whose weight is its base weight times its boost
    while its season window is open -- that is the whole "prefer the Christmas
    collection in December" behaviour, with no per-collection mode needed.

    The full library is always in the draw as a baseline pool of weight 1, so
    uncollected photos still appear and the mode degrades to a plain shuffle
    when nothing is collected yet. An image sitting in several collections gets
    proportionally more chances, which is the intuitive reading of putting it
    in several.
    """
    pools: list[tuple[float, list[int]]] = []
    everything = library.all_ids()
    if not everything:
        return None
    pools.append((1.0, everything))
    for collection in list_collections(engine):
        weight = effective_weight(collection, today)
        if weight <= 0:
            continue
        ids = library.all_ids(collection_id=collection.id)
        if ids:
            pools.append((weight, ids))

    total = sum(weight for weight, _ in pools)
    if total <= 0:
        return random.choice(everything)
    draw = random.uniform(0, total)
    for weight, ids in pools:
        draw -= weight
        if draw <= 0:
            return random.choice(ids)
    return random.choice(pools[-1][1])


def _pick_next(
    library: ImageLibrary,
    mode: str,
    last_path: str | None,
    engine=None,
    today: date | None = None,
) -> int | None:
    collection_id = collection_mode_id(mode)
    if collection_id is not None:
        ids = library.all_ids(collection_id=collection_id)
        # An emptied or deleted collection would otherwise freeze the frame on
        # whatever is already up. Showing something beats showing nothing.
        if not ids:
            log.warning("collection %s is empty or gone; falling back to the whole library",
                        collection_id)
            ids = library.all_ids()
    elif mode == "least-shown" and engine is not None:
        return _pick_least_shown(engine)
    elif mode == "weighted" and engine is not None:
        return _pick_weighted(library, engine, today or datetime.now(UTC).date())
    elif mode == "favorites":
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
        # One-shot flag set by manual triggers ("show next now", library
        # "show now"). Bypasses quiet hours for that single refresh.
        self._force_next = False
        self._busy = False
        self._next_due_at: datetime | None = None
        # Live render progress for the web UI's home page (circular spinner,
        # early image swap on cache write).
        self.refresh_tracker = RenderTracker()

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
        # Manual triggers force a refresh even inside quiet hours; the periodic
        # tick honours both the enable toggle and the window.
        force = self._force_next
        self._force_next = False
        if (
            not force
            and self.settings.quiet_hours_enabled
            and is_quiet(now_local, self.settings.quiet_start, self.settings.quiet_end)
        ):
            log.debug("inside quiet hours; skipping refresh")
            return

        with timed("scheduler.pick_next"):
            if self._next_override is not None:
                image_id = self._next_override
                self._next_override = None
            else:
                image_id = _pick_next(
                    self.library,
                    self.settings.selection_mode,
                    self._last_path,
                    engine=self.engine,
                    today=now_local.date(),
                )
        if image_id is None:
            log.info("no images available to display")
            return
        img = self.library.get(image_id)
        if img is None:
            return

        self._busy = True
        self.refresh_tracker.start(image_id, img.path)
        try:
            loop = asyncio.get_running_loop()
            processed = await loop.run_in_executor(
                None,
                _process_with_tracker,
                Path(img.path),
                self.settings,
                self.cache,
                img.sha256,
                self.refresh_tracker,
            )
            self.refresh_tracker.set_stage("show", 95.0)
            await loop.run_in_executor(None, self.driver.show, processed.image)
        except Exception as e:
            log.exception("failed to render/show image %s", img.path)
            self.refresh_tracker.mark_failed(repr(e))
            self._busy = False
            return
        self._busy = False
        self.refresh_tracker.mark_done()

        record_show(self.engine, image_id)
        self._last_path = img.path
        self._last_shown_at = datetime.now(UTC)
        record("scheduler.step.total", perf_counter() - step_start)
        log.info("displayed %s", img.path)

    def stop(self) -> None:
        self._stop.set()
        self.kick.set()

    def show_now(self, image_id: int) -> None:
        """Queue a specific image to be the next refresh and kick the loop.
        A manual "show now" overrides quiet hours."""
        self._next_override = image_id
        self._force_next = True
        self.kick.set()

    def force_next(self) -> None:
        """Advance to the next image now, bypassing quiet hours ("show next
        now")."""
        self._force_next = True
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


def _process_with_tracker(
    path: Path,
    settings: Settings,
    cache: Cache,
    sha256: str,
    tracker: RenderTracker,
):
    """Trampoline for run_in_executor — keyword args aren't directly forwarded."""
    return process(path, settings, cache, sha256, tracker=tracker)
