"""Palette-quantizing dithering algorithms.

Floyd-Steinberg and Atkinson use a per-pixel column loop in Python (sequential
error propagation can't be fully vectorised), but each iteration does only a
single 3D-LUT lookup + a few vectorised adds — no per-pixel LAB conversion or
argmin. The LUT is built once per palette using LAB-distance for perceptual
accuracy, then runtime lookup is pure RGB-space O(1).

Empirically: rewriting from naive per-pixel LAB+argmin to LUT cuts Pi-4 cost
from ~120 seconds per image to sub-second.
"""

from __future__ import annotations

import threading

import numpy as np

from vibeframe.processor.palette import RGB, SPECTRA6, palette_lab, rgb_to_lab

# 64-level (step=4) RGB->palette LUT: 64^3 = 262 144 entries x 1 byte = ~256 kB.
# Plenty fine-grained for visually correct nearest-palette mapping at the
# bin-centre RGB resolution we sample.
_LUT_STEP = 4
_LUT_SIZE = 256 // _LUT_STEP  # 64
_LUT_SHIFT = 2  # log2(_LUT_STEP)

_lut_cache: dict[tuple[RGB, ...], np.ndarray] = {}
_lut_lock = threading.Lock()


def _build_palette_lut(palette: tuple[RGB, ...]) -> np.ndarray:
    """64^3 LUT mapping (R>>2, G>>2, B>>2) → nearest-palette index (uint8)."""
    coords = np.arange(_LUT_SIZE, dtype=np.int32) * _LUT_STEP + _LUT_STEP // 2
    grid = np.stack(np.meshgrid(coords, coords, coords, indexing="ij"), axis=-1)
    flat_rgb = grid.reshape(-1, 3).astype(np.uint8)
    flat_lab = rgb_to_lab(flat_rgb)
    pal_lab = palette_lab(palette)
    d2 = np.sum((flat_lab[:, None, :] - pal_lab[None, :, :]) ** 2, axis=-1)
    return np.argmin(d2, axis=-1).reshape(_LUT_SIZE, _LUT_SIZE, _LUT_SIZE).astype(np.uint8)


def _get_lut(palette: tuple[RGB, ...]) -> np.ndarray:
    lut = _lut_cache.get(palette)
    if lut is not None:
        return lut
    with _lut_lock:
        lut = _lut_cache.get(palette)
        if lut is None:
            lut = _build_palette_lut(palette)
            _lut_cache[palette] = lut
        return lut


def _lookup_palette_indices(rgb_float: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Vectorised LUT lookup for an (..., 3) float array of RGB values."""
    quantized = np.clip(rgb_float, 0, 255).astype(np.int32) >> _LUT_SHIFT
    return lut[quantized[..., 0], quantized[..., 1], quantized[..., 2]]


def _error_diffuse(
    src: np.ndarray,
    palette: tuple[RGB, ...],
    horizontal: tuple[tuple[int, float], ...],
    next_row: tuple[tuple[int, float], ...],
    next_next_row: tuple[tuple[int, float], ...] = (),
) -> np.ndarray:
    """Generic error-diffusion dither parameterised by error weights.

    `horizontal` weights write to the current row at offsets > 0.
    `next_row` writes to row y+1 at relative x offsets (can be negative).
    `next_next_row` writes to row y+2 (only used by Atkinson).
    All weights are already divided by their algorithm's divisor.
    """
    h, w, _ = src.shape
    lut = _get_lut(palette)
    pal_arr = np.array(palette, dtype=np.float32)

    buf = src.astype(np.float32, copy=True)
    out = np.empty((h, w), dtype=np.uint8)

    # Pre-allocate accumulators for downward-diffused error so we don't write
    # into buf one element at a time per pixel.
    row_plus1 = np.zeros((w + 4, 3), dtype=np.float32)
    row_plus2 = np.zeros((w + 4, 3), dtype=np.float32) if next_next_row else None
    # Padding columns let us write to x-1 and x+2 without bounds checks.
    PAD = 2

    for y in range(h):
        # Fold pending errors from prior rows into buf for this row.
        buf[y] += row_plus1[PAD : PAD + w]
        if row_plus2 is not None:
            row_plus1[:] = row_plus2
            row_plus2.fill(0.0)
        else:
            row_plus1.fill(0.0)

        row = buf[y]
        for x in range(w):
            pix = row[x]
            r = int(pix[0])
            g = int(pix[1])
            b = int(pix[2])
            r = 0 if r < 0 else (255 if r > 255 else r)
            g = 0 if g < 0 else (255 if g > 255 else g)
            b = 0 if b < 0 else (255 if b > 255 else b)
            idx = lut[r >> _LUT_SHIFT, g >> _LUT_SHIFT, b >> _LUT_SHIFT]
            out[y, x] = idx
            err = pix - pal_arr[idx]
            for dx, weight in horizontal:
                nx = x + dx
                if nx < w:
                    row[nx] += err * weight
            for dx, weight in next_row:
                row_plus1[PAD + x + dx] += err * weight
            if row_plus2 is not None:
                for dx, weight in next_next_row:
                    row_plus2[PAD + x + dx] += err * weight

    return out


_FS_HORIZONTAL = ((1, 7.0 / 16.0),)
_FS_NEXT_ROW = ((-1, 3.0 / 16.0), (0, 5.0 / 16.0), (1, 1.0 / 16.0))


def floyd_steinberg(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    return _error_diffuse(src, palette, _FS_HORIZONTAL, _FS_NEXT_ROW)


_ATKINSON_HORIZONTAL = ((1, 1.0 / 8.0), (2, 1.0 / 8.0))
_ATKINSON_NEXT_ROW = ((-1, 1.0 / 8.0), (0, 1.0 / 8.0), (1, 1.0 / 8.0))
_ATKINSON_NEXT_NEXT_ROW = ((0, 1.0 / 8.0),)


def atkinson(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    return _error_diffuse(
        src, palette, _ATKINSON_HORIZONTAL, _ATKINSON_NEXT_ROW, _ATKINSON_NEXT_NEXT_ROW
    )


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
    biased = src.astype(np.float32) + tile * strength
    return _lookup_palette_indices(biased, _get_lut(palette))


def quantize_only(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    return _lookup_palette_indices(src.astype(np.float32), _get_lut(palette))


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


def clear_lut_cache_for_tests() -> None:
    with _lut_lock:
        _lut_cache.clear()
