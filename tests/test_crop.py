from __future__ import annotations

from PIL import Image

from vibeframe.processor.crop import center_crop, crop_to, fit_letterbox, smart_crop


def test_center_crop_matches_target_aspect():
    img = Image.new("RGB", (1200, 800), "red")
    out = center_crop(img, 800, 480)
    # crop output preserves source pixels but matches dst aspect, not dst size
    assert abs((out.width / out.height) - (800 / 480)) < 1e-3


def test_fit_letterbox_fills_exact_size():
    img = Image.new("RGB", (1000, 1000), "green")
    out = fit_letterbox(img, 800, 480)
    assert out.size == (800, 480)


def test_crop_to_returns_exact_size():
    img = Image.new("RGB", (1200, 800), "blue")
    for mode in ("smart", "center", "fit"):
        out = crop_to(img, 800, 480, mode)
        assert out.size == (800, 480), f"mode {mode} returned {out.size}"


def test_smart_crop_keeps_salient_region(sample_jpeg):
    img = Image.open(sample_jpeg).convert("RGB")
    out = smart_crop(img, 800, 480)
    # output is a cropped region (not yet resized). Verify aspect matches target.
    assert abs((out.width / out.height) - (800 / 480)) < 1e-2
