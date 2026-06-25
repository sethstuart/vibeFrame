"""Synthetic load harness — exercises the hot paths to surface bottlenecks.

Usage (inside the container or any environment with vibeframe installed):

    python -m vibeframe.bench                       # default: 20 synthetic photos, 1 pass
    python -m vibeframe.bench --photos 50 --runs 3  # bigger sample, average 3 runs
    python -m vibeframe.bench --from /vibeFrame     # use real NFS-backed photos
    python -m vibeframe.bench --from /vibeFrame --pick 10   # only 10 of them

Reports per-stage timings as a markdown table to stdout. With --metrics-url it
also fetches /metrics.json from a running container and includes those numbers.
"""

from __future__ import annotations

import argparse
import random
import shutil
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from PIL import Image

from vibeframe import timing
from vibeframe.cache import Cache
from vibeframe.config import Settings
from vibeframe.db import build_engine
from vibeframe.display.mock_driver import MockDriver
from vibeframe.library import ImageLibrary
from vibeframe.processor.pipeline import process
from vibeframe.thumb_warmer import generate_thumb


def _make_synthetic(dir_: Path, n: int, seed: int = 42) -> list[Path]:
    rng = random.Random(seed)
    dir_.mkdir(parents=True, exist_ok=True)
    paths = []
    sizes = [(1200, 800), (2400, 1600), (4000, 3000), (1600, 1200)]
    for i in range(n):
        w, h = sizes[i % len(sizes)]
        img = Image.new("RGB", (w, h), (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
        # Add noise so JPEG compression isn't trivially small.
        px = img.load()
        for _ in range(min(20_000, w * h // 50)):
            x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        p = dir_ / f"synth_{i:04d}.jpg"
        img.save(p, "JPEG", quality=85)
        paths.append(p)
    return paths


def _stage(label: str, fn) -> tuple[str, float]:
    t0 = perf_counter()
    fn()
    return (label, perf_counter() - t0)


def _bench(photos_dir: Path, pick: int, settings: Settings) -> list[tuple[str, float]]:
    results: list[tuple[str, float]] = []
    engine = build_engine(settings.db_path)
    cache = Cache(settings.cache_dir, max_bytes=settings.cache_max_bytes)
    library = ImageLibrary(photos_dir, engine, recursive=True, cache=cache)
    driver = MockDriver(settings.mock_dir, orientation=settings.orientation)

    # 1: cold scan (empty DB)
    results.append(_stage("library.scan (cold)", library.scan))
    # 2: warm scan (everything indexed, mtimes match → 0 rehashes)
    results.append(_stage("library.scan (warm)", library.scan))

    images = library.list(limit=pick)
    if not images:
        return results

    # 3: pipeline cold (no cache)
    def pipe_cold():
        for img in images:
            process(Path(img.path), settings, cache, img.sha256)

    results.append(_stage(f"pipeline.process x{len(images)} (cold)", pipe_cold))

    # 4: pipeline warm (cache hits)
    def pipe_warm():
        for img in images:
            process(Path(img.path), settings, cache, img.sha256)

    results.append(_stage(f"pipeline.process x{len(images)} (warm)", pipe_warm))

    # 5: thumb cold — generate without cache check (warmer would write the file)
    def thumb_cold():
        for img in images:
            generate_thumb(Path(img.path))

    results.append(_stage(f"thumb.generate x{len(images)} (cold)", thumb_cold))

    # 6: driver.show (mock — file write, not real e-paper)
    last = process(Path(images[0].path), settings, cache, images[0].sha256)
    results.append(_stage("driver.show (mock)", lambda: driver.show(last.image)))

    return results


def _format_markdown(runs: list[list[tuple[str, float]]]) -> str:
    labels = [lbl for lbl, _ in runs[0]]
    out = ["| Stage | " + " | ".join(f"Run {i+1} (s)" for i in range(len(runs))) + " | Mean (s) |"]
    out.append("|" + "---|" * (len(runs) + 2))
    for i, label in enumerate(labels):
        values = [r[i][1] for r in runs]
        cells = " | ".join(f"{v:.3f}" for v in values)
        mean = statistics.mean(values)
        out.append(f"| {label} | {cells} | {mean:.3f} |")
    return "\n".join(out)


def _format_metrics_snapshot(snap: dict) -> str:
    rows = sorted(snap.items(), key=lambda kv: kv[1].get("p95_ms", 0), reverse=True)
    if not rows:
        return "_(no timing metrics recorded)_"
    out = ["| Stage | Count | Mean (ms) | p50 (ms) | p95 (ms) | Max (ms) | Total (s) |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for name, s in rows:
        out.append(
            f"| {name} | {s['count']} | {s['mean_ms']:.2f} | {s['p50_ms']:.2f} | "
            f"{s['p95_ms']:.2f} | {s['max_ms']:.2f} | {s['total_seconds']:.2f} |"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="vibeFrame perf bench")
    p.add_argument("--photos", type=int, default=20, help="number of synthetic photos to generate")
    p.add_argument("--from", dest="from_dir", help="use real photos under this dir instead of synthetics")
    p.add_argument("--pick", type=int, default=10, help="how many photos to push through the pipeline")
    p.add_argument("--runs", type=int, default=1, help="repeat the whole bench N times")
    p.add_argument("--metrics-url", help="fetch /metrics.json from a running instance and include in report")
    args = p.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="vibeframe-bench-") as work:
        work_dir = Path(work)
        if args.from_dir:
            photos_dir = Path(args.from_dir)
            print(f"# vibeFrame bench (using existing dir: {photos_dir})", flush=True)
        else:
            photos_dir = work_dir / "photos"
            print(f"# vibeFrame bench (generating {args.photos} synthetic photos)", flush=True)
            _make_synthetic(photos_dir, args.photos)

        settings = Settings(
            photos_dir=photos_dir,
            cache_dir=work_dir / "cache",
            state_dir=work_dir / "state",
            driver="mock",
            refresh_seconds=10,
        )
        settings.ensure_dirs()

        all_runs: list[list[tuple[str, float]]] = []
        for r in range(args.runs):
            timing.clear()
            # Use a fresh DB and cache each run for cleanliness.
            shutil.rmtree(settings.state_dir, ignore_errors=True)
            shutil.rmtree(settings.cache_dir, ignore_errors=True)
            settings.ensure_dirs()
            print(f"\n## Run {r + 1}/{args.runs}", flush=True)
            run_results = _bench(photos_dir, args.pick, settings)
            for label, secs in run_results:
                print(f"  {label}: {secs:.3f}s")
            all_runs.append(run_results)
            print("\n### Per-stage metrics from this run")
            print(_format_metrics_snapshot(timing.snapshot()))

        print("\n## Summary")
        print(_format_markdown(all_runs))

        if args.metrics_url:
            import httpx

            print("\n## Live container metrics")
            try:
                snap = httpx.get(args.metrics_url, timeout=10).json()
                print(_format_metrics_snapshot(snap))
            except Exception as e:
                print(f"_(failed to fetch metrics: {e})_")

    return 0


if __name__ == "__main__":
    sys.exit(main())
