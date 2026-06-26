"""Palette-quantizing dithering algorithms.

Floyd-Steinberg and Atkinson are inherently sequential — each pixel's error
diffuses into pixels not yet processed — so we can't vectorise the inner
loop. Instead we move the loop out of CPython:

  - If `numba` is installed (the optional `dither` extra), we JIT-compile
    specialised inner loops to native code. On a Pi 4 this drops a 480x800
    Floyd-Steinberg pass from ~20 s to ~300 ms, and Atkinson from ~50 s to
    ~700 ms.
  - If numba isn't available we fall back to the original numpy + Python
    loop. Still works, just slow.

In both cases palette mapping uses a precomputed 64^3 RGB->index LUT built
once per palette from LAB-distance, so colour matching stays perceptual
without paying a LAB conversion per pixel.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from vibeframe.processor.palette import RGB, SPECTRA6, palette_lab, rgb_to_lab

log = logging.getLogger(__name__)

# Try to import numba. Module-level fallback so callers don't branch.
try:  # pragma: no cover - environment-dependent
    from numba import njit  # type: ignore[import-not-found]

    _NUMBA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NUMBA_AVAILABLE = False

    def njit(*_args, **_kwargs):  # type: ignore[misc]
        def _decorator(fn):
            return fn

        # Allow both @njit and @njit(...).
        if len(_args) == 1 and callable(_args[0]) and not _kwargs:
            return _args[0]
        return _decorator

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


# ───────────────────────────────────────────── numba JIT'd inner loops ─

# These functions are decorated with @njit (real or no-op fallback). When
# numba is installed they compile to native code on first call, then cache
# the result to NUMBA_CACHE_DIR for subsequent process starts.

@njit(cache=True, nogil=True, fastmath=True)
def _fs_njit(buf: np.ndarray, lut: np.ndarray, pal: np.ndarray) -> np.ndarray:
    """Floyd-Steinberg error diffusion. `buf` is the (H, W, 3) float32 scratch
    buffer (mutated in place). Returns (H, W) uint8 palette indices."""
    h, w, _ = buf.shape
    out = np.empty((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            r = buf[y, x, 0]
            g = buf[y, x, 1]
            b = buf[y, x, 2]
            ri = max(0, min(255, int(r))) >> 2
            gi = max(0, min(255, int(g))) >> 2
            bi = max(0, min(255, int(b))) >> 2
            idx = lut[ri, gi, bi]
            out[y, x] = idx
            er = r - pal[idx, 0]
            eg = g - pal[idx, 1]
            eb = b - pal[idx, 2]
            if x + 1 < w:
                buf[y, x + 1, 0] += er * 0.4375  # 7/16
                buf[y, x + 1, 1] += eg * 0.4375
                buf[y, x + 1, 2] += eb * 0.4375
            if y + 1 < h:
                if x > 0:
                    buf[y + 1, x - 1, 0] += er * 0.1875  # 3/16
                    buf[y + 1, x - 1, 1] += eg * 0.1875
                    buf[y + 1, x - 1, 2] += eb * 0.1875
                buf[y + 1, x, 0] += er * 0.3125  # 5/16
                buf[y + 1, x, 1] += eg * 0.3125
                buf[y + 1, x, 2] += eb * 0.3125
                if x + 1 < w:
                    buf[y + 1, x + 1, 0] += er * 0.0625  # 1/16
                    buf[y + 1, x + 1, 1] += eg * 0.0625
                    buf[y + 1, x + 1, 2] += eb * 0.0625
    return out


@njit(cache=True, nogil=True, fastmath=True)
def _atkinson_njit(buf: np.ndarray, lut: np.ndarray, pal: np.ndarray) -> np.ndarray:
    """Atkinson dither: 6 neighbours, weight 1/8 each (12.5% of error is
    discarded — that's why Atkinson images look slightly more contrasty)."""
    h, w, _ = buf.shape
    out = np.empty((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            r = buf[y, x, 0]
            g = buf[y, x, 1]
            b = buf[y, x, 2]
            ri = max(0, min(255, int(r))) >> 2
            gi = max(0, min(255, int(g))) >> 2
            bi = max(0, min(255, int(b))) >> 2
            idx = lut[ri, gi, bi]
            out[y, x] = idx
            er = (r - pal[idx, 0]) * 0.125
            eg = (g - pal[idx, 1]) * 0.125
            eb = (b - pal[idx, 2]) * 0.125
            if x + 1 < w:
                buf[y, x + 1, 0] += er
                buf[y, x + 1, 1] += eg
                buf[y, x + 1, 2] += eb
            if x + 2 < w:
                buf[y, x + 2, 0] += er
                buf[y, x + 2, 1] += eg
                buf[y, x + 2, 2] += eb
            if y + 1 < h:
                if x > 0:
                    buf[y + 1, x - 1, 0] += er
                    buf[y + 1, x - 1, 1] += eg
                    buf[y + 1, x - 1, 2] += eb
                buf[y + 1, x, 0] += er
                buf[y + 1, x, 1] += eg
                buf[y + 1, x, 2] += eb
                if x + 1 < w:
                    buf[y + 1, x + 1, 0] += er
                    buf[y + 1, x + 1, 1] += eg
                    buf[y + 1, x + 1, 2] += eb
            if y + 2 < h:
                buf[y + 2, x, 0] += er
                buf[y + 2, x, 1] += eg
                buf[y + 2, x, 2] += eb
    return out


_FS_HORIZONTAL = ((1, 7.0 / 16.0),)
_FS_NEXT_ROW = ((-1, 3.0 / 16.0), (0, 5.0 / 16.0), (1, 1.0 / 16.0))


def floyd_steinberg(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    if _NUMBA_AVAILABLE:
        buf = src.astype(np.float32, copy=True)
        return _fs_njit(buf, _get_lut(palette), np.asarray(palette, dtype=np.float32))
    return _error_diffuse(src, palette, _FS_HORIZONTAL, _FS_NEXT_ROW)


_ATKINSON_HORIZONTAL = ((1, 1.0 / 8.0), (2, 1.0 / 8.0))
_ATKINSON_NEXT_ROW = ((-1, 1.0 / 8.0), (0, 1.0 / 8.0), (1, 1.0 / 8.0))
_ATKINSON_NEXT_NEXT_ROW = ((0, 1.0 / 8.0),)


def atkinson(src: np.ndarray, palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    if _NUMBA_AVAILABLE:
        buf = src.astype(np.float32, copy=True)
        return _atkinson_njit(buf, _get_lut(palette), np.asarray(palette, dtype=np.float32))
    return _error_diffuse(
        src, palette, _ATKINSON_HORIZONTAL, _ATKINSON_NEXT_ROW, _ATKINSON_NEXT_NEXT_ROW
    )


def prewarm() -> None:
    """Force numba to JIT-compile the dither inner loops at startup so the
    first user-facing refresh doesn't pay the ~3s compile latency. No-op when
    numba isn't installed."""
    if not _NUMBA_AVAILABLE:
        return
    dummy = np.zeros((4, 4, 3), dtype=np.uint8)
    try:
        floyd_steinberg(dummy, SPECTRA6)
        atkinson(dummy, SPECTRA6)
        log.info("dither numba prewarm complete")
    except Exception as e:  # pragma: no cover
        log.warning("dither prewarm failed: %s", e)


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
