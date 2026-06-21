from __future__ import annotations

import numpy as np

from vibeframe.processor.palette import RGB, SPECTRA6, palette_lab, rgb_to_lab


def _nearest_index(pixel_rgb: np.ndarray, pal_lab: np.ndarray) -> int:
    lab = rgb_to_lab(pixel_rgb.reshape(1, 3))[0]
    diff = pal_lab - lab
    return int(np.argmin(np.einsum("kc,kc->k", diff, diff)))


def _error_diffuse(
    src: np.ndarray,
    palette: tuple[RGB, ...],
    weights: list[tuple[int, int, float]],
    divisor: float,
) -> np.ndarray:
    """Generic error-diffusion dither. `weights` is list of (dx, dy, w)."""
    h, w, _ = src.shape
    buf = src.astype(np.float32).copy()
    pal_arr = np.array(palette, dtype=np.float32)
    pal_l = palette_lab(palette)
    out = np.zeros((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            old = np.clip(buf[y, x], 0, 255)
            idx = _nearest_index(old.astype(np.uint8), pal_l)
            out[y, x] = idx
            err = old - pal_arr[idx]
            for dx, dy, weight in weights:
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    buf[ny, nx] += err * (weight / divisor)
    return out


def floyd_steinberg(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    weights = [(1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)]
    return _error_diffuse(src, palette, weights, 16.0)


def atkinson(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    weights = [(1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)]
    return _error_diffuse(src, palette, weights, 8.0)


_BAYER_8 = (
    np.array(
        [
            [0, 32, 8, 40, 2, 34, 10, 42],
            [48, 16, 56, 24, 50, 18, 58, 26],
            [12, 44, 4, 36, 14, 46, 6, 38],
            [60, 28, 52, 20, 62, 30, 54, 22],
            [3, 35, 11, 43, 1, 33, 9, 41],
            [51, 19, 59, 27, 49, 17, 57, 25],
            [15, 47, 7, 39, 13, 45, 5, 37],
            [63, 31, 55, 23, 61, 29, 53, 21],
        ],
        dtype=np.float32,
    )
    / 64.0
    - 0.5
)


def bayer(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6, strength: float = 48.0) -> np.ndarray:
    h, w, _ = src.shape
    tile = np.tile(_BAYER_8, ((h + 7) // 8, (w + 7) // 8))[:h, :w, None]
    biased = np.clip(src.astype(np.float32) + tile * strength, 0, 255).astype(np.uint8)
    pal_l = palette_lab(palette)
    flat_lab = rgb_to_lab(biased.reshape(-1, 3))
    diff = flat_lab[:, None, :] - pal_l[None, :, :]
    d2 = np.einsum("nkc,nkc->nk", diff, diff)
    return np.argmin(d2, axis=-1).reshape(h, w).astype(np.uint8)


def quantize_only(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    h, w, _ = src.shape
    pal_l = palette_lab(palette)
    flat_lab = rgb_to_lab(src.reshape(-1, 3))
    diff = flat_lab[:, None, :] - pal_l[None, :, :]
    d2 = np.einsum("nkc,nkc->nk", diff, diff)
    return np.argmin(d2, axis=-1).reshape(h, w).astype(np.uint8)


def dither(
    src: np.ndarray,
    algorithm: str,
    palette: tuple[RGB, ...] = SPECTRA6,
) -> np.ndarray:
    """Dispatch by algorithm name. Returns a (H, W) uint8 array of palette indices."""
    if algorithm == "floyd-steinberg":
        return floyd_steinberg(src, palette)
    if algorithm == "atkinson":
        return atkinson(src, palette)
    if algorithm == "bayer":
        return bayer(src, palette)
    if algorithm == "none":
        return quantize_only(src, palette)
    raise ValueError(f"unknown dither algorithm: {algorithm}")
