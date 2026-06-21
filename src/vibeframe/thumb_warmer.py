from __future__ import annotations

import hashlib
import io
import logging
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from vibeframe.config import Settings
from vibeframe.library import ImageLibrary
from vibeframe.timing import record, timed

log = logging.getLogger(__name__)

THUMB_MAX_SIDE = 320
THUMB_QUALITY = 80


def thumb_cache_path(settings: Settings, src: Path) -> Path:
    stat = src.stat()
    key = hashlib.sha256(f"{src}|{stat.st_mtime_ns}|{stat.st_size}".encode()).hexdigest()
    return settings.cache_dir / "thumbs" / f"{key}.jpg"


def generate_thumb(src: Path) -> bytes:
    with timed("thumb.generate"):
        with Image.open(src) as raw:
            oriented = ImageOps.exif_transpose(raw).convert("RGB")
            oriented.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            oriented.save(buf, format="JPEG", quality=THUMB_QUALITY)
            return buf.getvalue()


class ThumbWarmer:
    """Single-threaded background worker that pre-generates missing thumbnails.

    Single thread is deliberate: hammering an NFS share with parallel JPEG
    decodes is slower than serialising. The web UI's on-demand thumb route
    uses the same cache key so any miss it generates ends up here too.
    """

    def __init__(self, settings: Settings, library: ImageLibrary) -> None:
        self.settings = settings
        self.library = library
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="thumb-warmer", daemon=True)
        self._thread.start()
        self.kick()

    def kick(self) -> None:
        self._kick.set()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._kick.wait()
            self._kick.clear()
            if self._stop.is_set():
                return
            try:
                self._warm_once()
            except Exception:
                log.exception("thumb warm pass failed")

    def _warm_once(self) -> None:
        images = self.library.list(limit=10_000)
        generated = 0
        skipped = 0
        started = time.monotonic()
        for img in images:
            if self._stop.is_set():
                return
            src = Path(img.path)
            try:
                cached = thumb_cache_path(self.settings, src)
            except FileNotFoundError:
                continue
            if cached.is_file():
                skipped += 1
                continue
            try:
                data = generate_thumb(src)
            except Exception as e:
                log.warning("failed to thumb %s: %s", src, e)
                continue
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(data)
            except OSError as e:
                log.warning("failed to write thumb cache for %s: %s", src, e)
                continue
            generated += 1
        elapsed = time.monotonic() - started
        record("thumb.warm_pass.seconds", elapsed)
        record("thumb.warm_pass.generated", float(generated))
        if generated or skipped:
            log.info(
                "thumb warm pass: generated=%d, cached=%d, elapsed=%.1fs",
                generated,
                skipped,
                elapsed,
            )
