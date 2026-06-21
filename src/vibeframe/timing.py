"""Lightweight in-process timing metrics.

Records the duration of named code stages into bounded ring buffers so the
/metrics endpoint can show p50/p95/max for every hot path. No external deps,
no I/O, sub-microsecond overhead per measurement.

Usage:

    from vibeframe.timing import timed, record

    with timed("pipeline.crop"):
        do_crop()

    @timed("library.scan")
    def scan(): ...

    record("driver.show", elapsed_seconds)
"""

from __future__ import annotations

import functools
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

RING_SIZE = 256

_samples: dict[str, deque[float]] = {}
_totals: dict[str, tuple[int, float]] = {}  # (count, total_seconds) — survives ring eviction
_lock = threading.Lock()


def record(name: str, seconds: float) -> None:
    if seconds < 0:
        return
    with _lock:
        buf = _samples.get(name)
        if buf is None:
            buf = deque(maxlen=RING_SIZE)
            _samples[name] = buf
        buf.append(seconds)
        count, total = _totals.get(name, (0, 0.0))
        _totals[name] = (count + 1, total + seconds)


@contextmanager
def timed(name: str):
    """Context manager — records elapsed wall time under `name`."""
    start = time.perf_counter()
    try:
        yield
    finally:
        record(name, time.perf_counter() - start)


def timed_call(name: str, fn: Callable[..., Any], *args, **kwargs):
    """Time a single function call inline. Returns the function's result."""
    start = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        record(name, time.perf_counter() - start)


def timed_decorator(name: str):
    """Decorator form of timed(). `name` is the metric name."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with timed(name):
                return fn(*args, **kwargs)

        return inner

    return wrap


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Nearest-rank percentile, fine for our use case (debugging, not SLO grading).
    k = max(0, min(len(sorted_values) - 1, math.ceil(pct / 100 * len(sorted_values)) - 1))
    return sorted_values[k]


def snapshot() -> dict[str, dict[str, float | int]]:
    """Return a per-stage summary suitable for JSON serialization."""
    with _lock:
        names = list(_samples.keys())
        out: dict[str, dict[str, float | int]] = {}
        for name in names:
            window = list(_samples[name])
            count, total = _totals.get(name, (len(window), sum(window)))
            sorted_w = sorted(window)
            out[name] = {
                "count": count,
                "total_seconds": total,
                "window_n": len(window),
                "mean_ms": (sum(window) / len(window) * 1000.0) if window else 0.0,
                "p50_ms": _percentile(sorted_w, 50) * 1000.0,
                "p95_ms": _percentile(sorted_w, 95) * 1000.0,
                "max_ms": (max(window) if window else 0.0) * 1000.0,
                "last_ms": (window[-1] if window else 0.0) * 1000.0,
            }
        return out


def clear() -> None:
    """Reset all recorded metrics. For tests."""
    with _lock:
        _samples.clear()
        _totals.clear()
