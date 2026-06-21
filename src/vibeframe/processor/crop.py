from __future__ import annotations

import numpy as np
from PIL import Image

from vibeframe.processor.palette import RGB, SPECTRA6, palette_lab, rgb_to_lab


def _target_box(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int]:
    """Return left/top/right/bottom for a centered crop matching dst aspect."""
    src_aspect = src_w / src_h
    dst_aspect = dst_w / dst_h
    if src_aspect > dst_aspect:
        new_w = int(round(src_h * dst_aspect))
        x0 = (src_w - new_w) // 2
        return (x0, 0, x0 + new_w, src_h)
    new_h = int(round(src_w / dst_aspect))
    y0 = (src_h - new_h) // 2
    return (0, y0, src_w, y0 + new_h)


def center_crop(image: Image.Image, dst_w: int, dst_h: int) -> Image.Image:
    box = _target_box(image.width, image.height, dst_w, dst_h)
    return image.crop(box)


def smart_crop(image: Image.Image, dst_w: int, dst_h: int) -> Image.Image:
    """Saliency-aware crop. Falls back to center crop if OpenCV is unavailable
    or saliency computation fails."""
    try:
        import cv2  # type: ignore
    except ImportError:
        return center_crop(image, dst_w, dst_h)

    src = np.array(image.convert("RGB"))
    bgr = cv2.cvtColor(src, cv2.COLOR_RGB2BGR)
    try:
        saliency = cv2.saliency.StaticSaliencySpectralResidual_create()  # type: ignore[attr-defined]
        ok, sal_map = saliency.computeSaliency(bgr)
        if not ok:
            raise RuntimeError("saliency failed")
    except Exception:
        return center_crop(image, dst_w, dst_h)

    sal_map = (sal_map * 255).astype(np.uint8)
    src_h, src_w = sal_map.shape
    target = _target_box(src_w, src_h, dst_w, dst_h)
    crop_w = target[2] - target[0]
    crop_h = target[3] - target[1]

    if crop_w == src_w:
        # Vertical pan: integrate row sums and slide a crop_h window.
        row_sum = sal_map.sum(axis=1).astype(np.int64)
        cum = np.concatenate(([0], np.cumsum(row_sum)))
        window_sums = cum[crop_h:] - cum[: len(cum) - crop_h]
        y0 = int(np.argmax(window_sums))
        return image.crop((0, y0, src_w, y0 + crop_h))

    col_sum = sal_map.sum(axis=0).astype(np.int64)
    cum = np.concatenate(([0], np.cumsum(col_sum)))
    window_sums = cum[crop_w:] - cum[: len(cum) - crop_w]
    x0 = int(np.argmax(window_sums))
    return image.crop((x0, 0, x0 + crop_w, src_h))


def fit_letterbox(
    image: Image.Image,
    dst_w: int,
    dst_h: int,
    palette: tuple[RGB, ...] = SPECTRA6,
) -> Image.Image:
    """Resize preserving aspect, then pad with the palette-nearest border color
    (typically black or white) to fill the canvas."""
    src = image.convert("RGB")
    scale = min(dst_w / src.width, dst_h / src.height)
    new_w = max(1, int(round(src.width * scale)))
    new_h = max(1, int(round(src.height * scale)))
    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)

    arr = np.array(resized).reshape(-1, 3)
    avg = arr.mean(axis=0, dtype=np.float64)
    avg_lab = rgb_to_lab(avg.astype(np.uint8).reshape(1, 3))
    pal_l = palette_lab(palette)
    diff = pal_l - avg_lab
    idx = int(np.argmin(np.einsum("kc,kc->k", diff, diff)))
    border = palette[idx]

    canvas = Image.new("RGB", (dst_w, dst_h), border)
    canvas.paste(resized, ((dst_w - new_w) // 2, (dst_h - new_h) // 2))
    return canvas


def crop_to(image: Image.Image, dst_w: int, dst_h: int, mode: str) -> Image.Image:
    if mode == "smart":
        cropped = smart_crop(image, dst_w, dst_h)
    elif mode == "center":
        cropped = center_crop(image, dst_w, dst_h)
    elif mode == "fit":
        return fit_letterbox(image, dst_w, dst_h)
    else:
        raise ValueError(f"unknown crop mode: {mode}")
    return cropped.resize((dst_w, dst_h), Image.Resampling.LANCZOS)
