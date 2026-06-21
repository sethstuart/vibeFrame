from __future__ import annotations

import logging
import threading

from PIL import Image

log = logging.getLogger(__name__)

# Known gpiochip labels for Pi 4 (BCM2711) and Pi 5 (RP1). gpiodevice tries
# to derive these from /proc/device-tree/compatible, which is unreadable
# inside the standard Docker sysfs sandbox even with bind-mount workarounds.
# libgpiod itself can still enumerate chips and match by label, so we patch
# gpiodevice's platform lookup to fall back to these when detection fails.
_FALLBACK_GPIOCHIP_LABELS = ["pinctrl-bcm2835", "pinctrl-bcm2711", "pinctrl-rp1"]


def _install_gpiodevice_fallback() -> None:
    try:
        import gpiodevice.platform as gp  # type: ignore[import-not-found]
    except ImportError:
        return

    original = gp.get_gpiochip_labels

    def patched():
        try:
            return original()
        except RuntimeError:
            log.warning(
                "gpiodevice platform detection failed (no /proc/device-tree "
                "access); falling back to known labels %s",
                _FALLBACK_GPIOCHIP_LABELS,
            )
            return _FALLBACK_GPIOCHIP_LABELS

    gp.get_gpiochip_labels = patched


_install_gpiodevice_fallback()


class InkyDriver:
    name = "inky"

    def __init__(self, orientation: int = 0) -> None:
        from inky.auto import auto  # type: ignore[import-not-found]

        self._inky = auto(ask_user=False, verbose=False)
        self.width = int(self._inky.width)
        self.height = int(self._inky.height)
        self.orientation = orientation
        self._lock = threading.Lock()
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
        framed = framed.convert("RGB")
        with self._lock:
            self._inky.set_image(framed)
            self._inky.show()
