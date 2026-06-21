from __future__ import annotations

from typing import Protocol

from PIL import Image


class DisplayDriver(Protocol):
    width: int
    height: int
    name: str

    def show(self, image: Image.Image) -> None: ...
