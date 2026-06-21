from __future__ import annotations

from PIL import Image, ImageEnhance


def apply(image: Image.Image, saturation: float, contrast: float) -> Image.Image:
    """Pre-quantization tone shaping. Saturation tends to help compensate for
    the limited gamut of e-paper, contrast for the muted blacks/whites."""
    out = image
    if abs(saturation - 1.0) > 1e-3:
        out = ImageEnhance.Color(out).enhance(saturation)
    if abs(contrast - 1.0) > 1e-3:
        out = ImageEnhance.Contrast(out).enhance(contrast)
    return out
