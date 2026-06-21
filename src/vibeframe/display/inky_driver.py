from __future__ import annotations

import logging

from PIL import Image

log = logging.getLogger(__name__)


class InkyDriver:
    name = "inky"

    def __init__(self, orientation: int = 0) -> None:
        from inky.auto import auto  # type: ignore[import-not-found]

        self._inky = auto(ask_user=False, verbose=False)
        self.width = int(self._inky.width)
        self.height = int(self._inky.height)
        self.orientation = orientation
        log.info(
            "inky display detected: %sx%s, orientation=%s",
            self.width,
            self.height,
            orientation,
        )

    def show(self, image: Image.Image) -> None:
        framed = image
        if self.orientation:
            framed = framed.rotate(self.orientation, expand=True)
        if framed.size != (self.width, self.height):
            framed = framed.resize((self.width, self.height), Image.Resampling.LANCZOS)
        self._inky.set_image(framed.convert("RGB"))
        self._inky.show()
