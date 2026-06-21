from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PIL import Image

from vibeframe.timing import timed

log = logging.getLogger(__name__)


class MockDriver:
    name = "mock"

    def __init__(self, output_dir: Path, orientation: int = 0, width: int = 800, height: int = 480) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.orientation = orientation
        self._lock = threading.Lock()
        log.info(
            "mock display active: writing PNGs to %s (size=%sx%s, orientation=%s)",
            self.output_dir,
            self.width,
            self.height,
            orientation,
        )

    def show(self, image: Image.Image) -> None:
        with timed("driver.mock.show"):
            framed = image
            if self.orientation:
                framed = framed.rotate(self.orientation, expand=True)
            if framed.size != (self.width, self.height):
                framed = framed.resize((self.width, self.height), Image.Resampling.LANCZOS)
            rgb = framed.convert("RGB")
            ts = int(time.time())
            with self._lock:
                rgb.save(self.output_dir / f"frame-{ts}.png")
                rgb.save(self.output_dir / "current.png")
