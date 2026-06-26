# CLAUDE.md

Guidance for working in this repo. vibeFrame is a self-hosted, NFS-backed digital
photo frame for the Pimoroni **Inky Impression 7.3" (Spectra 6)** e-paper panel,
running as one Docker container on a Raspberry Pi 4. Python + FastAPI + HTMX +
Alpine; pillow/numpy/opencv image pipeline.

## Layout

- `src/vibeframe/__main__.py` — entrypoint: builds engine/cache/library, restores
  persisted settings, starts scheduler + watcher + thumb warmer + uvicorn.
- `src/vibeframe/config.py` — `Settings` (pydantic-settings, `VIBEFRAME_*` env).
- `src/vibeframe/scheduler.py` — asyncio loop that picks → renders → shows; owns
  the `RenderTracker` and the one-shot "show this image now" override.
- `src/vibeframe/processor/` — `pipeline.process()` (crop → tonemap → dither →
  palette, with two cache layers), `crop.py` (face/saliency smart crop),
  `faces.py` (YuNet), `dither.py` (numba-accelerated error diffusion + LUT),
  `palette.py` (Spectra 6, LAB), `tonemap.py`.
- `src/vibeframe/display/` — `inky_driver.py` (real), `mock_driver.py` (PNG),
  `factory.py` (`auto` picks real, falls back to mock).
- `src/vibeframe/library.py`, `db.py` — SQLite (SQLModel): Image/Favorite/History/
  Setting. `cache.py` (LRU disk cache), `thumb_warmer.py`, `watcher.py`,
  `progress.py` (RenderTracker), `timing.py` (metrics ring buffers), `bench.py`.
- `src/vibeframe/web/` — `app.py` (FastAPI + middleware), `routes/*.py`,
  `templates/*.html` (Jinja, `_*.html` are fragments/macros),
  `static/` (`app.css`, `app.js`, `tokens/`, `icons.svg`, `face_yunet.onnx`).

## Dev workflow

```sh
pip install -e ".[dev]"          # add ,dither for numba locally
pytest                            # 36 tests; must stay green
ruff check .                      # must be clean (CI runs both, nothing else)
```

Run natively against the mock driver with `VIBEFRAME_DRIVER=mock` and
`VIBEFRAME_PHOTOS_DIR`/`STATE_DIR`/`CACHE_DIR` pointed at local dirs (see README).
CI (`.github/workflows/ci.yml`) runs **only ruff + pytest** — it does **not** build
the Docker image, so a Dockerfile change is validated on the Pi (or locally with
`docker build`), not by CI.

## Gotchas (these cost real time — don't relearn them)

- **Alpine script order**: `static/app.js` MUST load *before* the Alpine CDN
  script in `base.html`. Alpine's CDN build dispatches `alpine:init`
  synchronously when it executes, so a registration script loaded after it never
  runs (`"<component> is not defined"`). Inline `x-data="foo()"` components
  defined in page `<script>` blocks are fine.
- **HTML attribute quoting**: never put `{{ x|tojson }}` inside a double-quoted
  Alpine attribute — `tojson` emits double quotes and breaks the attribute. Pass
  values via Jinja-autoescaped `data-*` attributes and read `$el.dataset.*`.
- **numba**: `NUMBA_CACHE_DIR` is set to a writable dir in the Dockerfile (the
  package installs root-owned, the app runs as the `vibeframe` user). `dither.prewarm()`
  is called at boot to JIT-compile before the first refresh. The pure-NumPy
  fallback runs when numba isn't installed — keep both paths working.
- **gpiodevice/Inky in a container**: `inky_driver.py` monkeypatches
  `gpiodevice.platform.get_gpiochip_labels` because `/proc/device-tree` isn't
  readable inside the container. Compose passes only `/dev/spidev0.0` (not 0.1),
  `/dev/i2c-1`, `/dev/gpiochip0`, and SPI/GPIO/I2C GIDs numerically (group names
  don't resolve in the container).
- **Docker build caching**: deps install in a layer keyed only on `pyproject.toml`
  (via a throwaway stub package), then `COPY src` + `pip install --no-deps
  --force-reinstall .`. Editing app code is a seconds-long rebuild; changing
  `pyproject.toml` triggers the full ~5-min reinstall. Don't reorder `COPY src`
  above the dependency install.
- **The panel write is the bottleneck**, not the pipeline: `driver.inky.show` is
  ~38 s of hardware refresh. The web UI swaps in the rendered image as soon as
  the cache is written (`RenderTracker.rendered`), without waiting for the push.
- **Settings persistence**: `POST /settings` writes the `Setting` table;
  `__main__._restore_persisted_settings` applies them over env defaults on boot.
- **Settings live preview**: `render-with.png` writes to the pipeline cache so the
  post-save `preview.png` is a cache hit (the "before" updates without re-rendering).
- **Pi reality**: 4 GB RAM, slow SD card. Apparent "low free memory" is page
  cache (reclaimable) — the real constraint is SD write I/O. `docker compose down`
  before `up -d --build`.

## Conventions

- UI: "e-ink editorial" design system — warm paper, hairline borders, three fonts
  (Space Grotesk / Hanken Grotesk / JetBrains Mono via CDN), Spectra 6 palette for
  data/accents only, **sentence case**, **no emoji** (★ ☆ ← → glyphs are OK).
- Wrap new hot paths in `vibeframe.timing.timed(...)` so they appear on `/metrics`.
- Web UI is host port **80** → container 8080. Browser hits `http://<host>`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  and a `Claude-Session:` line.
