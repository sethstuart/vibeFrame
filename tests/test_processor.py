from __future__ import annotations

import numpy as np

from vibeframe.cache import Cache
from vibeframe.processor import dither, palette
from vibeframe.processor.pipeline import process


def test_palette_has_six_distinct_colors():
    assert len(palette.SPECTRA6) == 6
    assert len(set(palette.SPECTRA6)) == 6


def test_quantize_only_produces_palette_indices():
    src = np.full((10, 10, 3), 200, dtype=np.uint8)
    out = dither.quantize_only(src, palette.SPECTRA6)
    assert out.shape == (10, 10)
    assert int(out.max()) < len(palette.SPECTRA6)


def test_bayer_uses_palette_only():
    rng = np.random.default_rng(0)
    src = rng.integers(0, 256, size=(40, 40, 3), dtype=np.uint8)
    out = dither.bayer(src, palette.SPECTRA6)
    assert set(np.unique(out).tolist()).issubset(set(range(len(palette.SPECTRA6))))


def test_pipeline_caches_output(tmp_settings, sample_jpeg, tmp_path):
    cache = Cache(tmp_settings.cache_dir, tmp_settings.cache_max_bytes)
    first = process(sample_jpeg, tmp_settings, cache)
    assert first.image.mode == "P"
    # Output covers the rotated/un-rotated target dims
    expected_w, expected_h = 800, 480
    if tmp_settings.orientation in (90, 270):
        expected_w, expected_h = 480, 800
    assert first.image.size == (expected_w, expected_h)

    cached_files_after_first = list(cache.root.rglob("*.png"))
    assert cached_files_after_first, "first run should populate cache"

    second = process(sample_jpeg, tmp_settings, cache)
    assert second.source_sha256 == first.source_sha256
    cached_files_after_second = list(cache.root.rglob("*.png"))
    assert len(cached_files_after_first) == len(cached_files_after_second)


def test_pipeline_uses_dither_setting(tmp_settings, sample_jpeg):
    tmp_settings.dither = "bayer"
    out = process(sample_jpeg, tmp_settings, None)
    assert out.image.mode == "P"
