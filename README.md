# vibeFrame

NFS-backed digital photo frame for the Pimoroni **Inky Impression 7.3" (Spectra 6)** e-paper display.

vibeFrame watches a directory on an NFS share, runs each image through a smart-crop and dithering pipeline tuned for the panel's 6-color palette, and ships a small local web UI for uploads, favorites, and settings. Runs as a single Docker container on a Raspberry Pi.

---

## Features

- Smart, saliency-aware cropping (with center/fit fallbacks) so portrait photos don't get awkwardly centered.
- LAB-aware dithering — Floyd-Steinberg, Atkinson, Bayer, or none — for cleaner color reproduction than naive RGB quantization.
- On-disk cache keyed by image hash + pipeline params, so re-shows are free.
- File watcher (with periodic NFS rescan fallback) picks up new photos automatically.
- Quiet hours skip refreshes overnight.
- Local web UI: current image, library, favorites, upload-to-NFS, per-image preview of what the panel will actually show, test pattern, manual "next now" trigger.
- Hardware-mocked dev mode — run the whole stack on macOS/Windows and inspect rendered frames as PNGs.

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
docker compose up -d
```

The web UI lands on `http://<pi-host>:8080`.

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

All settings come from environment variables prefixed `VIBEFRAME_`. See `.env.example` for the canonical list. Highlights:

| Variable | Default | Notes |
|---|---|---|
| `VIBEFRAME_PHOTOS_DIR` | `/photos` | Where the container looks for images (bind-mount your NFS share here). |
| `VIBEFRAME_UPLOAD_SUBDIR` | `_uploads` | Subdir of `PHOTOS_DIR` for uploads from the web UI. |
| `VIBEFRAME_ORIENTATION` | `270` | `0`, `90`, `180`, `270`. |
| `VIBEFRAME_REFRESH_SECONDS` | `1800` | Slideshow interval. |
| `VIBEFRAME_SELECTION_MODE` | `shuffle` | `shuffle`, `sequential`, `favorites`, `recent`. |
| `VIBEFRAME_DITHER` | `floyd-steinberg` | `floyd-steinberg`, `atkinson`, `bayer`, `none`. |
| `VIBEFRAME_CROP_MODE` | `smart` | `smart`, `center`, `fit`. |
| `VIBEFRAME_QUIET_START` / `_END` | `22:00` / `07:00` | Skip refreshes during this window. |
| `VIBEFRAME_TZ` | `UTC` | IANA timezone for quiet hours. |
| `VIBEFRAME_DRIVER` | `auto` | `auto` (real Inky if available, fall back to mock), `mock`. |
| `VIBEFRAME_WEB_TOKEN` | _(unset)_ | If set, write endpoints require `X-Vibeframe-Token: <value>`. |

---

## Web UI endpoints

| Path | Purpose |
|---|---|
| `GET /` | Current image, manual "next now", quiet-hours status. |
| `GET /images` | Paginated library with thumbnails. |
| `POST /images/upload` | Multipart upload → written to `PHOTOS_DIR/UPLOAD_SUBDIR/`. |
| `DELETE /images/{id}` | Delete from NFS. |
| `GET /images/{id}/preview.png` | Server-rendered preview of what the panel will show. |
| `POST /favorites/{id}` / `DELETE /favorites/{id}` | Toggle favorite. |
| `GET /settings` / `POST /settings` | Runtime-tunable settings. |
| `POST /system/next` | Trigger an immediate refresh. |
| `GET /system/test-pattern.png` | Render the 6-color palette as bars (preview). |
| `POST /system/test-pattern` | Push the palette bars to the actual display. |
| `GET /healthz` | Liveness check (used by Docker healthcheck). |

---

## Architecture

```
            ┌─────────────────────── vibeFrame container ───────────────────────┐
            │                                                                   │
NFS ───────▶│  ImageLibrary  ◀── watcher (watchdog) ── /photos/                  │
bind        │       │                                                           │
            │       ▼                                                           │
            │  Processor (PIL + NumPy: crop → tonemap → dither) ──▶ Cache       │
            │       │                                                           │
            │       ▼                                                           │
            │  Scheduler (asyncio) ──▶ DisplayDriver ──▶ Inky SPI (or Mock PNG) │
            │       ▲                                                           │
            │       │                                                           │
            │  FastAPI Web UI ◀────── SQLite (settings/favorites/history)       │
            └───────────────────────────────────────────────────────────────────┘
```

One Python process, one asyncio loop. The Inky SPI driver is not thread-safe, so the scheduler owns all writes; heavy work (image decode + dither) runs in a thread executor.

---

## Roadmap / not yet implemented

- Hardware button support (Inky's A/B/C/D) via `gpiozero`.
- Per-image sidecar JSON overrides (crop window, orientation).
- Cloud photo sources (Immich, Google Photos).
- Calibration UI for tuning the 6-color palette per individual panel.

---

## License

MIT.
