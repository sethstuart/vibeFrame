from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from vibeframe.config import Settings, reset_settings_for_tests


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("VIBEFRAME_"):
            monkeypatch.delenv(key, raising=False)
    reset_settings_for_tests()
    yield
    reset_settings_for_tests()


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        photos_dir=tmp_path / "photos",
        upload_subdir="_uploads",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        driver="mock",
        refresh_seconds=10,
    )


@pytest.fixture
def sample_jpeg(tmp_path: Path) -> Path:
    """Synthetic 1200x800 RGB JPEG with a clear bright square (for saliency tests)."""
    img = Image.new("RGB", (1200, 800), (40, 40, 40))
    # Bright high-contrast block in the right third — saliency should latch onto it.
    for y in range(200, 600):
        for x in range(800, 1100):
            img.putpixel((x, y), (250, 220, 30))
    p = tmp_path / "fixture.jpg"
    img.save(p, "JPEG", quality=85)
    return p
