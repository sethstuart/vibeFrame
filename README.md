# vibeFrame

NFS-backed digital photo frame for the Pimoroni **Inky Impression 7.3" (Spectra 6)** e-paper display.

vibeFrame watches a directory on an NFS share, runs each image through a smart-crop and dithering pipeline tuned for the panel's 6-color palette, and ships a small local web UI for uploads, favorites, and settings. Runs as a single Docker container on a Raspberry Pi.

---

## Features

**Image pipeline**

- **Face-aware smart cropping.** Detects faces (YuNet) and centres the crop on
  them so people stay in frame. With no faces, falls back to an ensemble of
  spectral-residual saliency × dominant-colour rejection so the crop chases the
  subject instead of a vibrant sky. `center` and `fit` modes are available too.
- **LAB-accurate dithering** — Floyd-Steinberg, Atkinson, Bayer, or none —
  mapped through a perceptual LAB lookup table for cleaner color than naive RGB
  quantization. The error-diffusion loops are **numba-JIT-accelerated** when the
  `dither` extra is installed (~sub-second per image on a Pi 4 vs ~20–50s pure
  Python).
- **Two-layer on-disk cache.** A "prepared" cache holds the decoded + cropped
  image so a settings tweak that only changes dither/saturation/contrast skips
  the NFS read and crop; the final dithered PNG is cached keyed by image hash +
  pipeline params, so re-shows are instant.

**Operation**

- File watcher (with a periodic NFS-rescan fallback, since NFS often misses
  inotify) picks up new photos automatically and pre-warms thumbnails.
- Quiet hours skip refreshes overnight.
- Settings changed in the web UI **persist across restarts** (SQLite-backed,
  applied over the env defaults on boot).
- HEIC/HEIF support is available via an optional `heif` extra.

**Web UI** (FastAPI + HTMX + Alpine, "e-ink editorial" design)

- **Now showing** — full-quality cropped hero of the current image, a live
  determinate progress spinner driven by the *real* render pipeline, drag-and-
  drop or browse upload to NFS with toast feedback, and a horizontally-scrolling
  "recently shown" strip.
- **Library / Favorites** — paginated grid, filename search, sort, multi-select
  with bulk favorite/delete, click-to-zoom lightbox, and per-image favorite /
  show-now / delete actions.
- **Settings** — live before/after compare slider that re-renders on change,
  a "push to frame?" prompt on save, and quiet-hours / schedule / tone controls.
- **Metrics** — live-updating per-stage timing table plus status tiles
  (avg processing time, NFS reachability + r/w latency, image count).

- Hardware-mocked dev mode — run the whole stack on macOS/Windows and inspect
  rendered frames as PNGs.

---

## Hardware

- Raspberry Pi (tested on Pi 4 / Pi 5) running Raspberry Pi OS Bookworm.
- Pimoroni Inky Impression 7.3" Spectra 6 (800×480, 6 colors: black, white, red, green, blue, yellow).
- SPI and I2C enabled (`sudo raspi-config` → Interface Options).

If you have the older 7-color Inky Impression 7.3", change `SPECTRA6` in `src/vibeframe/processor/palette.py` to the 7-color palette (black, white, red, green, blue, yellow, orange).

---

## Quickstart (Raspberry Pi + Docker)

### 1. Mount your NFS share on the Pi host

The container does **not** mount NFS itself — it bind-mounts a host path. Add to `/etc/fstab`:

```
nas.local:/photos  /mnt/nas/photos  nfs  ro,vers=4.1,bg,soft,timeo=100,_netdev  0  0
```

Then `sudo systemctl daemon-reload && sudo mount -a`. Use `ro` if you only want to display; `rw` if you want the web UI's upload feature to write back.

### 2. Configure

```sh
cp .env.example .env
# edit .env: orientation, refresh interval, quiet hours, etc.
```

### 3. Deploy

```sh
docker compose up -d --build
```

The web UI lands on `http://<pi-host>` (host port 80 → container port 8080).

#### Rebuilding on the Pi

The Dockerfile installs dependencies in a layer keyed only on
`pyproject.toml`, so **rebuilds after editing app code/templates/CSS reuse
the cached dependency layer** — they take seconds and barely touch the SD
card. Only a change to `pyproject.toml` (i.e. dependencies) triggers the
full ~5-minute reinstall of numba/opencv/etc.

For any rebuild, stop the running container first so the build and the live
process don't compete for the SD card's limited write throughput (the real
bottleneck on a Pi 4 — you'll see high I/O wait, not memory exhaustion):

```sh
docker compose down && docker compose up -d --build
```

> If builds on the Pi are painful, the nicest option is to build the arm64
> image on a faster machine (or CI) and `docker pull` it on the Pi so the
> Pi never compiles anything.

### Build-time options

Pass these as `--build-arg` (e.g. `docker compose build --build-arg INSTALL_DITHER=0`):

| Build arg | Default | Effect |
|---|---|---|
| `INSTALL_RPI` | `1` | Install the real Inky/SPI/GPIO driver (`inky[rpi]`, `gpiod`). Set `0` for dev images that only use the mock driver. |
| `INSTALL_DITHER` | `1` | Install `numba` for JIT-accelerated dithering. `0` keeps a pure-NumPy fallback (correct, but ~20–50s/image on a Pi). |
| `INSTALL_PROFILE` | `0` | Install `py-spy` for deep profiling (see [Profiling](#profiling)). |

HEIC/HEIF photo support is a separate Python extra (`pip install ".[heif]"`);
it's not enabled in the Docker image by default since it's a heavy native
build most libraries don't need.

---

## Development (no Pi required)

The mock driver writes rendered frames as PNGs instead of pushing to a real display:

```sh
mkdir -p dev/photos dev/state dev/cache
cp some/photos/*.jpg dev/photos/
docker compose -f docker-compose.dev.yml up --build
open dev/state/mock/current.png
```

Or run natively:

```sh
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
VIBEFRAME_DRIVER=mock \
VIBEFRAME_PHOTOS_DIR=./dev/photos \
VIBEFRAME_STATE_DIR=./dev/state \
VIBEFRAME_CACHE_DIR=./dev/cache \
VIBEFRAME_REFRESH_SECONDS=15 \
python -m vibeframe
```

Run the tests:

```sh
pytest
```

---

## Configuration reference

Environment variables prefixed `VIBEFRAME_` set the **defaults**. See `.env.example` for the canonical list. Note: the display/tone/schedule settings (orientation, refresh interval, selection mode, dither, crop mode, saturation, contrast, quiet hours) can also be changed at runtime in the web UI's Settings page — those are persisted to SQLite and applied *over* the env defaults on the next boot, so a saved setting survives container restarts. Highlights:

| Variable | Default | Notes |
|---|---|---|
| `VIBEFRAME_PHOTOS_DIR` | `/photos` | Where the container looks for images (bind-mount your NFS share here). |
| `VIBEFRAME_UPLOAD_SUBDIR` | `_uploads` | Subdir of `PHOTOS_DIR` for uploads from the web UI. |
| `VIBEFRAME_ORIENTATION` | `270` | `0`, `90`, `180`, `270`. |
| `VIBEFRAME_REFRESH_SECONDS` | `1800` | Slideshow interval. |
| `VIBEFRAME_SELECTION_MODE` | `shuffle` | `shuffle`, `sequential`, `favorites`, `recent`. |
| `VIBEFRAME_DITHER` | `floyd-steinberg` | `floyd-steinberg`, `atkinson`, `bayer`, `none`. |
| `VIBEFRAME_CROP_MODE` | `smart` | `smart`, `center`, `fit`. |
| `VIBEFRAME_QUIET_HOURS_ENABLED` | `true` | Master on/off for quiet hours. When off, the frame refreshes around the clock. |
| `VIBEFRAME_QUIET_START` / `_END` | `22:00` / `07:00` | Skip refreshes during this window. "Show next" / "Show now" always override it. |
| `VIBEFRAME_TZ` | `UTC` | IANA timezone for quiet hours. |
| `VIBEFRAME_DRIVER` | `auto` | `auto` (real Inky if available, fall back to mock), `mock`. |
| `VIBEFRAME_METRICS_REFRESH_SECONDS` | `10` | How often the Metrics page auto-refreshes. |
| `VIBEFRAME_WEB_TOKEN` | _(unset)_ | If set, write endpoints require `X-Vibeframe-Token: <value>`. |

---

## Web UI endpoints

Pages:

| Path | Purpose |
|---|---|
| `GET /` | Now showing — current image, live progress, recent strip, upload. |
| `GET /images` | Paginated library grid (`?q=`, `?sort=`, `?favorites_only=`, `?offset=`, `?limit=`). |
| `GET /settings` | Settings form + live before/after preview. |
| `GET /metrics` | Live per-stage timing table + status tiles. |

Images:

| Path | Purpose |
|---|---|
| `POST /images/upload` | Multipart (multi-file) upload → `PHOTOS_DIR/UPLOAD_SUBDIR/`. |
| `DELETE /images/{id}` | Delete from NFS. |
| `POST /images/{id}/show` | Push this specific image to the panel now. |
| `POST /images/bulk/favorite` / `POST /images/bulk/delete` | Bulk ops (`{ids:[…]}`). |
| `GET /images/{id}/thumb.png` | Cached 320px JPEG thumbnail. |
| `GET /images/{id}/full.jpg` | Full-aspect (uncropped) downscaled JPEG — the library lightbox view. |
| `GET /images/{id}/source-cropped.jpg` | Full-quality source cropped to the panel composition (the hero image). |
| `GET /images/{id}/preview.png` | Dithered render of exactly what the panel shows. |
| `GET /images/{id}/render-with.png` | Preview render with ad-hoc `dither`/`crop_mode`/`saturation`/`contrast`/`orientation` query params (drives the Settings live preview). |
| `POST /favorites/{id}` / `DELETE /favorites/{id}` | Toggle favorite. |

System:

| Path | Purpose |
|---|---|
| `POST /settings` | Persist settings (redirects to `/settings?saved=1`). |
| `POST /system/next` | Trigger an immediate refresh of the next image. |
| `GET /system/now-showing` | HTML fragment of the current-image hero (polled/swapped by the UI). |
| `GET /system/status` / `GET /system/status-chip` | Scheduler status JSON / header chip fragment. |
| `GET /system/render-status` | Live render progress JSON (drives the home spinner + early image swap). |
| `GET /system/recent` | Recently-shown images JSON (drives the recent strip). |
| `GET /system/test-pattern.png` / `POST /system/test-pattern` | Render / push the 6-color palette bars. |
| `GET /metrics.json` / `GET /metrics/fragment` / `POST /metrics/clear` | Metrics JSON / live table fragment / reset. |
| `GET /healthz` | Liveness check (used by the Docker healthcheck). |

Write endpoints honor `VIBEFRAME_WEB_TOKEN` (via the `X-Vibeframe-Token` header) when it's set.

---

## Architecture

```
            ┌─────────────────────── vibeFrame container ───────────────────────┐
            │                                                                   │
NFS ───────▶│  ImageLibrary ◀── watcher (watchdog + rescan) ── /photos/         │
bind        │       │                └─▶ ThumbWarmer (bg thumbs + prepared cache)│
            │       ▼                                                           │
            │  Processor: face/saliency crop → tonemap → numba dither            │
            │       │            └─ prepared cache ──┐   └─ dithered cache ──┐   │
            │       ▼                                ▼                       ▼   │
            │  Scheduler (asyncio) ──▶ DisplayDriver ──▶ Inky SPI (or Mock PNG)  │
            │       │   └─ RenderTracker (live progress) ──┐                     │
            │       ▼                                       ▼                     │
            │  FastAPI Web UI ◀── SQLite (settings/favorites/history)            │
            │       └─ timing ring buffers ──▶ /metrics                          │
            └───────────────────────────────────────────────────────────────────┘
```

One Python process, one asyncio loop. The Inky SPI driver is not thread-safe, so the scheduler owns all writes; heavy work (image decode + dither) runs in a thread executor. A `RenderTracker` exposes per-stage progress so the web UI can show a real progress spinner and swap in the new image the instant it's rendered, without waiting for the ~38s panel push to finish.

---

## Profiling

vibeFrame has built-in lightweight timing for every hot path and a synthetic
load harness for capturing clean numbers without waiting for real traffic.

> **Where the time goes.** With the `dither` extra (numba) installed — the
> default for the Pi image — error-diffusion dithering is JIT-compiled and
> runs in tens of milliseconds, so the dominant cost of a refresh is the
> physical panel write (`driver.inky.show`, ~38 s on the Spectra 6 — that's
> hardware, not us). **Without** numba the pure-NumPy fallback is ~20 s
> (Floyd-Steinberg) to ~50 s (Atkinson) per 480×800 image. Either way the
> dithered output is cached after the first render, so re-shows are instant,
> and the post-EXIF + post-crop intermediate is cached separately so a
> settings tweak that only changes dither/saturation/contrast skips the NFS
> read and crop. `bayer` (~10 ms, ordered) is the fastest dither if you ever
> need to avoid the diffusion cost entirely.

### Live metrics

Every request and every background stage records into in-memory ring buffers
(last 256 samples each, lifetime counters). View them at:

- `http://<host>/metrics` — sortable table, slow rows highlighted
- `GET /metrics.json` — JSON for scripting
- `POST /metrics/clear` — reset (useful before reproducing a slow case)

Stage names you'll see:

| Prefix | What it measures |
|---|---|
| `http.GET./...`, `http.POST./...` | Per-route HTTP duration (matched route, not URL) |
| `pipeline.process.hit` / `.miss` | End-to-end image processing, split by cache result |
| `pipeline.image.open`, `pipeline.crop.<mode>`, `pipeline.dither.<algo>`, `pipeline.cache.write`, ... | Individual pipeline stages |
| `pipeline.prepared.lookup` / `.load` / `.write` | The decoded+cropped intermediate cache |
| `library.scan` and `library.scan.*` | Scan total + walk/stat/hash/db sub-stages |
| `thumb.generate`, `thumb.warm_pass.seconds`, `nfs.write`, `source_cropped` | Thumbnails, NFS upload writes, hero crop |
| `driver.inky.prepare` / `.set_image` / `.show` | Display driver phases (Spectra 6 physical refresh is `driver.inky.show`) |
| `scheduler.step.total`, `scheduler.pick_next` | Scheduler timings |

### Synthetic bench

```sh
docker exec -it vibeframe python -m vibeframe.bench --from /vibeFrame --pick 10 --runs 3
```

Or against synthetic photos (works in any environment):

```sh
python -m vibeframe.bench --photos 50 --pick 10 --runs 3
```

Times each stage cold (empty cache) and warm (cache hits), reports a markdown
table. Add `--metrics-url http://localhost:8080/metrics.json` to also dump the live
container's accumulated metrics.

### Deep profiling with py-spy

For looking inside opaque native calls (PIL, OpenCV, libgpiod) where the
in-app metrics see only a single stage.

1. Rebuild with the profile extra:
   ```sh
   docker compose build --build-arg INSTALL_PROFILE=1
   ```
2. Add `SYS_PTRACE` to `docker-compose.yml` (commented line included) and
   `docker compose up -d`.
3. Attach:
   ```sh
   # Live top view, ctrl-C to exit
   docker exec -it vibeframe py-spy top --pid 1

   # 60s flame-graph-style record
   docker exec vibeframe py-spy record -o /tmp/profile.svg --pid 1 --duration 60
   docker cp vibeframe:/tmp/profile.svg ./profile.svg

   # One-shot stack dump of every thread (great for "why is it hung")
   docker exec vibeframe py-spy dump --pid 1
   ```

Drop `SYS_PTRACE` when you're done — it lets the container read other
processes' memory, which you don't want in normal operation.

## Roadmap / not yet implemented

- Hardware button support (Inky's A/B/C/D) via `gpiozero`.
- Per-image sidecar JSON overrides (crop window, orientation).
- Cloud photo sources (Immich, Google Photos).
- Calibration UI for tuning the 6-color palette per individual panel.

---

## License

MIT.
