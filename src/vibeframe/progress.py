"""Render progress tracking — drives the web UI's live progress indicators
and the early image swap on the home page.

The tracker is updated synchronously from the pipeline executor thread as
each stage runs. The polling /system/render-status endpoint reads its
snapshot. One tracker per render context:

  - `Scheduler.refresh_tracker` — the recurring panel refresh.
  - `AppState.preview_tracker` — the settings live-preview render-with.

Both are independent so a user adjusting settings doesn't fight a
scheduler refresh.

The web UI swaps in the rendered preview as soon as `rendered_at` is set
(i.e. the cache PNG is on disk), not waiting for the ~38s panel push.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

# Stage name -> (start_pct, end_pct). The first pct is set on entering the
# stage; transitions between stages snap to the next start_pct.
# Numbers are approximate proportions of total wall-time on the Pi 4:
#   pick is trivial; decode is NFS-bound (~variable); crop is fast; dither
#   dominates (~70% of pre-show wall time post-vectorisation); cache_write
#   is small; show is the panel refresh (~38s, after `rendered_at`).
STAGE_WEIGHTS: dict[str, tuple[float, float]] = {
    "idle": (0.0, 0.0),
    "pick": (0.0, 2.0),
    "cache_lookup": (2.0, 3.0),
    "decode": (3.0, 12.0),
    "exif": (12.0, 14.0),
    "crop": (14.0, 28.0),
    "tonemap": (28.0, 32.0),
    "ndarray": (32.0, 34.0),
    "dither": (34.0, 88.0),
    "quantize": (88.0, 90.0),
    "cache_write": (90.0, 95.0),
    "show": (95.0, 99.5),
    "done": (100.0, 100.0),
    "failed": (100.0, 100.0),
}


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


class RenderTracker:
    """Thread-safe live state of a single render-in-progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.image_id: int | None = None
        self.image_path: str | None = None
        self.stage: str = "idle"
        self.progress_pct: float = 0.0
        self.started_at: datetime | None = None
        self.rendered_at: datetime | None = None
        self.shown_at: datetime | None = None
        self.error: str | None = None

    def start(self, image_id: int | None, image_path: str | None) -> None:
        with self._lock:
            self.image_id = image_id
            self.image_path = image_path
            self.stage = "pick"
            self.progress_pct = STAGE_WEIGHTS["pick"][0]
            self.started_at = datetime.now(UTC)
            self.rendered_at = None
            self.shown_at = None
            self.error = None

    def set_stage(self, name: str, pct: float | None = None) -> None:
        with self._lock:
            self.stage = name
            if pct is None:
                pct = STAGE_WEIGHTS.get(name, (self.progress_pct, self.progress_pct))[0]
            self.progress_pct = pct

    def mark_rendered(self) -> None:
        with self._lock:
            self.rendered_at = datetime.now(UTC)
            # Don't drop progress_pct — caller may not have advanced beyond
            # cache_write yet, but the rendered file IS available now.

    def mark_done(self) -> None:
        with self._lock:
            self.stage = "done"
            self.progress_pct = 100.0
            self.shown_at = datetime.now(UTC)
            if self.rendered_at is None:
                self.rendered_at = self.shown_at

    def mark_failed(self, error: str) -> None:
        with self._lock:
            self.stage = "failed"
            self.error = error

    def snapshot(self) -> dict:
        with self._lock:
            active = self.started_at is not None and self.stage not in ("done", "failed", "idle")
            return {
                "active": active,
                "stage": self.stage,
                "progress_pct": round(self.progress_pct, 1),
                "image_id": self.image_id,
                "image_path": self.image_path,
                "rendered": self.rendered_at is not None,
                "done": self.shown_at is not None or self.stage == "done",
                "failed": self.stage == "failed",
                "error": self.error,
                "started_at": _iso(self.started_at),
                "rendered_at": _iso(self.rendered_at),
                "shown_at": _iso(self.shown_at),
            }
