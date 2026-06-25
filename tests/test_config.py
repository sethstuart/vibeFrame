from __future__ import annotations

import pytest

from vibeframe.config import Settings


def test_orientation_accepts_string_env(monkeypatch):
    """Env vars arrive as strings; Literal[int, ...] needs coercion before validation."""
    monkeypatch.setenv("VIBEFRAME_ORIENTATION", "270")
    monkeypatch.setenv("VIBEFRAME_PHOTOS_DIR", "/tmp/photos")
    s = Settings()
    assert s.orientation == 270


@pytest.mark.parametrize("value", ["0", "90", "180", "270"])
def test_orientation_all_valid_string_values(monkeypatch, value):
    monkeypatch.setenv("VIBEFRAME_ORIENTATION", value)
    s = Settings()
    assert s.orientation == int(value)


def test_orientation_rejects_invalid_string(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("VIBEFRAME_ORIENTATION", "45")
    with pytest.raises(ValidationError):
        Settings()
