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


def _slide_window(weight_axis: np.ndarray, window: int) -> int:
    """Given a 1D non-negative weight array, return the start index whose
    `window`-length sum is greatest. Cumsum + diff is O(n)."""
    cum = np.concatenate(([0], np.cumsum(weight_axis.astype(np.int64))))
    sums = cum[window:] - cum[: len(cum) - window]
    return int(np.argmax(sums))


def _center_window(centre: int, window: int, src_len: int) -> int:
    """Place a window of length `window` centred on `centre`, clamped to
    [0, src_len - window]."""
    start = centre - window // 2
    return max(0, min(src_len - window, start))


def _faces_to_crop(
    image: Image.Image,
    faces: list[tuple[int, int, int, int]],
    src_w: int,
    src_h: int,
    crop_w: int,
    crop_h: int,
) -> Image.Image:
    """Center the crop window on the bounding box of detected faces, falling
    back to a clamp at image edges. Multi-face shots use the union bbox's
    centroid so groups stay framed together."""
    fx0 = min(f[0] for f in faces)
    fy0 = min(f[1] for f in faces)
    fx1 = max(f[0] + f[2] for f in faces)
    fy1 = max(f[1] + f[3] for f in faces)
    face_cx = (fx0 + fx1) // 2
    face_cy = (fy0 + fy1) // 2
    if crop_w == src_w:
        y0 = _center_window(face_cy, crop_h, src_h)
        return image.crop((0, y0, src_w, y0 + crop_h))
    x0 = _center_window(face_cx, crop_w, src_w)
    return image.crop((x0, 0, x0 + crop_w, src_h))


def _ensemble_importance(bgr: np.ndarray) -> np.ndarray | None:
    """Combine spectral-residual saliency with a dominant-colour rejection
    map. Each is normalised to uint8; their per-pixel product is what we
    slide the crop window over. Returns None if OpenCV bits fail.

    The motivation: spectral-residual saliency often latches onto vibrant
    backgrounds (skies, foliage). Multiplying by a "not the dominant colour"
    map suppresses those, so the crop chases foreground subject regions.
    """
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        saliency = cv2.saliency.StaticSaliencySpectralResidual_create()  # type: ignore[attr-defined]
        ok, sal_map = saliency.computeSaliency(bgr)
        if not ok:
            return None
    except Exception:
        return None
    sal_u8 = (sal_map * 255).astype(np.uint8)

    # Dominant-colour rejection: smooth the image, build a tiny 4x4 H,S
    # histogram (so the modal colour quadrants are very chunky), backproject
    # onto the smoothed pixels to get a "looks like background" probability,
    # then invert so high = unlike background.
    try:
        smoothed = cv2.pyrMeanShiftFiltering(bgr, sp=8, sr=20)
        hsv = cv2.cvtColor(smoothed, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [4, 4], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 255, cv2.NORM_MINMAX)
        bg_prob = cv2.calcBackProject([hsv], [0, 1], hist, [0, 180, 0, 256], 1)
        not_bg = 255 - bg_prob
    except Exception:
        return sal_u8

    return ((sal_u8.astype(np.uint16) * not_bg.astype(np.uint16)) >> 8).astype(np.uint8)


def smart_crop(image: Image.Image, dst_w: int, dst_h: int) -> Image.Image:
    """Saliency-aware crop. Order of precedence:

    1. Face detection (YuNet) — if any face is found, the crop is centred
       on the cluster bbox so people stay in frame.
    2. Ensemble saliency: spectral-residual * dominant-colour rejection,
       slid as a window along the panning axis. The product map suppresses
       false-positives from vibrant backgrounds.
    3. Centre crop fallback if OpenCV is missing or both stages fail.
    """
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError:
        return center_crop(image, dst_w, dst_h)

    src = np.array(image.convert("RGB"))
    src_h, src_w = src.shape[:2]
    target = _target_box(src_w, src_h, dst_w, dst_h)
    crop_w = target[2] - target[0]
    crop_h = target[3] - target[1]

    # 1. Face detection.
    try:
        from vibeframe.processor.faces import detect_faces

        faces = detect_faces(src)
    except Exception:
        faces = []
    if faces:
        return _faces_to_crop(image, faces, src_w, src_h, crop_w, crop_h)

    # 2. Ensemble saliency on the panning axis.
    bgr = cv2.cvtColor(src, cv2.COLOR_RGB2BGR)
    importance = _ensemble_importance(bgr)
    if importance is None:
        return center_crop(image, dst_w, dst_h)

    if crop_w == src_w:
        y0 = _slide_window(importance.sum(axis=1), crop_h)
        return image.crop((0, y0, src_w, y0 + crop_h))
    x0 = _slide_window(importance.sum(axis=0), crop_w)
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
