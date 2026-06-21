from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from vibeframe.scheduler import is_quiet


def test_quiet_window_does_not_wrap():
    tz = ZoneInfo("UTC")
    assert is_quiet(datetime(2026, 1, 1, 12, 0, tzinfo=tz), time(9, 0), time(17, 0))
    assert not is_quiet(datetime(2026, 1, 1, 8, 0, tzinfo=tz), time(9, 0), time(17, 0))
    assert not is_quiet(datetime(2026, 1, 1, 17, 0, tzinfo=tz), time(9, 0), time(17, 0))


def test_quiet_window_wraps_midnight():
    tz = ZoneInfo("UTC")
    assert is_quiet(datetime(2026, 1, 1, 23, 30, tzinfo=tz), time(22, 0), time(7, 0))
    assert is_quiet(datetime(2026, 1, 1, 3, 0, tzinfo=tz), time(22, 0), time(7, 0))
    assert not is_quiet(datetime(2026, 1, 1, 12, 0, tzinfo=tz), time(22, 0), time(7, 0))


def test_quiet_window_disabled_when_equal():
    tz = ZoneInfo("UTC")
    assert not is_quiet(datetime(2026, 1, 1, 5, 0, tzinfo=tz), time(7, 0), time(7, 0))
