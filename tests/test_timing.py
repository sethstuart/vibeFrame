from __future__ import annotations

import time

import pytest

from vibeframe import timing


@pytest.fixture(autouse=True)
def _clean():
    timing.clear()
    yield
    timing.clear()


def test_record_creates_entry():
    timing.record("foo", 0.1)
    snap = timing.snapshot()
    assert "foo" in snap
    assert snap["foo"]["count"] == 1
    assert snap["foo"]["mean_ms"] == pytest.approx(100.0, rel=0.01)


def test_negative_durations_ignored():
    timing.record("bar", -1.0)
    assert "bar" not in timing.snapshot()


def test_timed_context_manager():
    with timing.timed("ctx"):
        time.sleep(0.005)
    snap = timing.snapshot()
    assert snap["ctx"]["count"] == 1
    assert snap["ctx"]["max_ms"] >= 5.0


def test_timed_decorator():
    @timing.timed_decorator("dec")
    def fn(x):
        return x * 2

    assert fn(3) == 6
    assert fn(4) == 8
    assert timing.snapshot()["dec"]["count"] == 2


def test_percentiles_basic():
    for i in range(1, 11):
        timing.record("p", i / 1000.0)  # 1ms..10ms
    snap = timing.snapshot()["p"]
    assert snap["count"] == 10
    assert snap["max_ms"] == pytest.approx(10.0, rel=0.01)
    assert snap["p50_ms"] == pytest.approx(5.0, abs=1.0)
    assert snap["p95_ms"] == pytest.approx(10.0, abs=1.0)


def test_ring_buffer_caps_at_256():
    for _ in range(300):
        timing.record("ring", 0.001)
    snap = timing.snapshot()["ring"]
    assert snap["count"] == 300  # lifetime count preserved
    assert snap["window_n"] == 256  # ring buffer capped


def test_clear_resets():
    timing.record("x", 0.01)
    assert "x" in timing.snapshot()
    timing.clear()
    assert timing.snapshot() == {}
