from __future__ import annotations

import numpy as np

RGB = tuple[int, int, int]

SPECTRA6: tuple[RGB, ...] = (
    (0, 0, 0),        # black
    (255, 255, 255),  # white
    (255, 243, 56),   # yellow
    (191, 0, 0),      # red
    (100, 64, 255),   # blue
    (67, 138, 28),    # green
)


def palette_image(palette: tuple[RGB, ...] = SPECTRA6):
    """Return a PIL palette image (mode 'P') usable as a quantize target."""
    from PIL import Image

    pal_img = Image.new("P", (1, 1))
    flat = [c for rgb in palette for c in rgb]
    flat += [0] * (768 - len(flat))
    pal_img.putpalette(flat)
    return pal_img


def palette_array(palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    return np.array(palette, dtype=np.uint8)


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_xyz(rgb: np.ndarray) -> np.ndarray:
    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float64,
    )
    return rgb @ m.T


def _xyz_to_lab(xyz: np.ndarray) -> np.ndarray:
    white = np.array([0.95047, 1.00000, 1.08883])
    xyz = xyz / white
    delta = 6 / 29
    f = np.where(xyz > delta**3, np.cbrt(xyz), xyz / (3 * delta**2) + 4 / 29)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert uint8 sRGB array (..., 3) to CIELAB float64."""
    linear = _srgb_to_linear(rgb.astype(np.float64))
    return _xyz_to_lab(_linear_to_xyz(linear))


def palette_lab(palette: tuple[RGB, ...] = SPECTRA6) -> np.ndarray:
    return rgb_to_lab(palette_array(palette))


def nearest_palette_indices(
    pixels_lab: np.ndarray, pal_lab: np.ndarray
) -> np.ndarray:
    """Return indices of nearest palette entry for each pixel using LAB distance.

    pixels_lab: shape (..., 3) float
    pal_lab:    shape (K, 3) float
    """
    diff = pixels_lab[..., None, :] - pal_lab[None, ...]
    d2 = np.einsum("...kc,...kc->...k", diff, diff)
    return np.argmin(d2, axis=-1)
