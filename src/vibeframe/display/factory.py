from __future__ import annotations

import logging

from vibeframe.config import Settings
from vibeframe.display.base import DisplayDriver
from vibeframe.display.mock_driver import MockDriver

log = logging.getLogger(__name__)


def build_driver(settings: Settings) -> DisplayDriver:
    target = settings.driver
    if target == "mock":
        return MockDriver(settings.mock_dir, orientation=settings.orientation)

    if target in ("auto", "inky"):
        try:
            from vibeframe.display.inky_driver import InkyDriver

            return InkyDriver(orientation=settings.orientation)
        except Exception as exc:
            if target == "inky":
                raise
            log.warning("Inky driver unavailable (%s); falling back to mock", exc)
            return MockDriver(settings.mock_dir, orientation=settings.orientation)

    raise ValueError(f"unknown driver: {target}")
