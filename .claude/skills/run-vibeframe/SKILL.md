---
name: run-vibeframe
description: Build, run, and drive vibeFrame with no e-paper hardware. Use when asked to start vibeFrame, launch the photo frame app, run its tests, take a screenshot of its web UI, check what the panel would display, or interact with the running app.
---

vibeFrame is a FastAPI + HTMX photo-frame server whose real output is an 800×480
Spectra-6 e-paper panel. With `VIBEFRAME_DRIVER=mock` it writes the exact frame it
*would* push to the panel as a PNG, so the whole app is drivable headlessly with no
Raspberry Pi and no Inky attached.

Drive it with **`.claude/skills/run-vibeframe/driver.py`** — it seeds a photo library,
boots the app, exercises the HTTP surface, asserts the panel frame is a real Spectra-6
image, and drives the web UI over the Chrome DevTools Protocol. Paths below are relative
to the repo root.

Verified on Windows 11, Python 3.13.3, Chrome 141. The driver resolves `.venv/Scripts`
vs `.venv/bin` itself, so it is portable, but the commands below are the Windows ones
that were actually run.

## Prerequisites

- **Python 3.11+** (`requires-python = ">=3.11"`).
- **Chrome or Chromium** — only for `shots`/`smoke`. Auto-detected at the standard
  install paths; override with `VIBEFRAME_CHROME=/path/to/chrome`. On a bare Ubuntu box
  this is the missing piece (`apt-get install -y chromium`); everything else is pure pip
  wheels, no system libraries needed (opencv is `opencv-python-headless`).

No hardware, no `xvfb`, no NFS mount, no Docker.

## Setup

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install --upgrade pip
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

`numba` is deliberately **not** installed — the dither module falls back to pure NumPy.
Both paths must keep working; add it with `pip install -e ".[dev,dither]"` to exercise
the JIT path.

## Run (agent path)

One command does everything and exits non-zero on failure:

```bash
./.venv/Scripts/python.exe .claude/skills/run-vibeframe/driver.py smoke --allow-js-errors
```

Expected tail:

```
[driver] panel OK: (800, 480), 6/6 inks used, 2 frames written -> ...\dev\state\mock\current.png
[driver] clicked 'Show next now': caption 'shown 17:06:04 UTC' -> 'shown 17:06:20 UTC'
[driver] SMOKE PASSED
```

Selection is `shuffle`, so the ink count varies run to run (`5/6` is normal) and the
image named in `rendered+shown:` changes. The assertion is that every colour is *in* the
palette, not that all six appear.

Drop `--allow-js-errors` to enforce a zero-JS-error page; see Gotchas for the one
known pre-existing failure that makes strict mode exit 1 today.

| command | what it does |
|---|---|
| `smoke` | seed → up → api → panel → shots → down. The one to run. `--keep` leaves it running |
| `up` / `down` | boot and leave serving on :8099 / stop it. `up` refuses if the port is already serving; `down` refuses to kill a pid whose cmdline isn't `-m vibeframe` |
| `seed` | write 4 synthetic photos (landscape/portrait/square/wide) into `dev/photos` |
| `api` | assert `/healthz`, all 4 pages, then force a render and require `shown_at` to *advance* |
| `panel` | assert `dev/state/mock/current.png` is exactly 800×480, uses only Spectra-6 inks, and post-dates this run |
| `shots` | screenshot all 4 pages, assert Alpine initialised, click "Show next now" and require the caption to change, collect JS errors |
| `render <img>` | run one image through the pipeline — no server, no DB, no scheduler |

Every assertion is written to fail rather than pass vacuously: the render check
baselines `shown_at` *before* posting (the boot render would otherwise satisfy
it), and `panel` compares the PNG's mtime against the run's start (`dev/` is
gitignored and never auto-cleaned, so a week-old frame would otherwise pass).
Run `panel` standalone with nothing tracked and it warns that it cannot verify
freshness rather than quietly asserting on a stale file.

Artifacts (all under the gitignored `dev/`):

- Panel frame → `dev/state/mock/current.png` (plus `frame-<ts>.png` per show)
- Screenshots → `dev/shots/{home,images,settings,metrics,home-after-next}.png`
- App log → `dev/app.log`

**Direct invocation** — the fast path for changes under `processor/` (crop, tonemap,
dither, palette). No boot, ~250 ms:

```bash
./.venv/Scripts/python.exe .claude/skills/run-vibeframe/driver.py render dev/photos/landscape.jpg
# [driver] landscape.jpg: (480, 800) mode=P in 244 ms -> ...\dev\shots\render.png
```

Note the result is `(480, 800)` mode `P` — the pipeline returns the image
*pre-rotation* at panel resolution; the display driver applies `orientation` and
converts to RGB. The first call in a process takes ~2 s (smart crop loads the YuNet
face model); subsequent ones are ~250 ms. OpenCV prints a
`Targets are not supported by the new graph engine` warning on load — benign.

## Run (human path)

```bash
VIBEFRAME_DRIVER=mock VIBEFRAME_PHOTOS_DIR=dev/photos VIBEFRAME_STATE_DIR=dev/state \
VIBEFRAME_CACHE_DIR=dev/cache VIBEFRAME_REFRESH_SECONDS=20 \
VIBEFRAME_QUIET_HOURS_ENABLED=false VIBEFRAME_WEB_PORT=8099 \
./.venv/Scripts/python.exe -m vibeframe
```

Serves <http://127.0.0.1:8099>. Ctrl-C to stop. A one-off screenshot without the driver:

```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --disable-gpu \
  --hide-scrollbars --virtual-time-budget=6000 --window-size=1280,1400 \
  --screenshot='C:\Users\Seth\Documents\GitHub\vibeFrame\dev\shots\home.png' \
  http://127.0.0.1:8099/
```

## Test

```bash
./.venv/Scripts/python.exe -m pytest -q          # 41 passed in 4.10s
./.venv/Scripts/python.exe -m ruff check .       # All checks passed!
```

CI runs only these two. Note `CLAUDE.md` says "36 tests" — it is stale; the suite is 41.

## Gotchas

- **Quiet hours will silently eat your run.** They default to on, 22:00–07:00, and
  inside that window the scheduler skips *every* refresh — no frames, no errors, looks
  like a hang. The driver always sets `VIBEFRAME_QUIET_HOURS_ENABLED=false` and asserts
  `in_quiet == false` in `/system/status`. Set it yourself for any manual run.
- **Known JS error: `Uncaught ReferenceError: $dispatch is not defined`** (pre-existing,
  not caused by the driver). `home.html:37,41` use `hx-on::before-request="$dispatch(...)"`,
  but `hx-on` is evaluated by **HTMX** as plain JS while `$dispatch` is an **Alpine**
  magic that only exists inside Alpine expressions. Compounding it, the listener is on
  the sibling `.hero` section (`home.html:118`), which a bubbling event would never reach
  anyway. Effect: the processing spinner never appears during a refresh — ~38 s of no
  feedback on real hardware. `smoke` without `--allow-js-errors` fails on exactly this.
- **The panel frame is the real assertion.** `dev/state/mock/current.png` must be exactly
  800×480 and contain *only* the 6 Spectra-6 inks. A regression in `palette.py` or
  `dither.py` shows up here and nowhere in the test suite.
- **`pipeline.process()` returns a `P`-mode image at `(480, 800)`** for the default
  orientation 270 — width/height are swapped pre-rotation. Don't assert `(800, 480)` on
  the pipeline result; assert it on the mock driver's output.
- **`Settings()` reads `.env` from the CWD.** A `.env` left in the repo silently overrides
  defaults. Env vars still win over it, but `driver.py render` passes `_env_file=None` to
  ignore it entirely. The driver also forces `VIBEFRAME_WEB_TOKEN=""` for the child,
  because a token in that `.env` would 401 the driver's own `POST /system/next`
  (`require_token` returns early only when the token is falsy).
- **Env vars are not the last word on settings.** `__main__._restore_persisted_settings`
  overlays the DB's `setting` table on top of the env at boot, so anyone who has hit Save
  on the settings page against this dev root can silently re-enable quiet hours or change
  the dither — and no env var fixes it. `up()` clears that table in its own scratch DB
  before launching; if you boot the app by hand, delete `dev/state/vibeframe.db` instead.
- **The driver binds the app to `127.0.0.1`.** The app's own default is `0.0.0.0` with no
  token, which would expose `POST /settings` and `DELETE /images/{id}` to the whole LAN
  for the lifetime of a `smoke --keep`.
- **Windows has no `SIGTERM` for this process.** `__main__` installs handlers via
  `loop.add_signal_handler`, which raises `NotImplementedError` on Windows and is
  suppressed — so the app never sees a graceful stop signal. `driver.py down` uses
  `taskkill /F /T` there and `terminate()` elsewhere.
- **`hx-on` vs Alpine generally** — this repo mixes HTMX and Alpine on the same elements.
  Alpine magics (`$dispatch`, `$refs`, `$el`) are **not** in scope inside `hx-on::*`
  handlers. That is the bug above, and it is an easy one to reintroduce.

## Troubleshooting

- **`app exited immediately (rc=1)`** with `error while attempting to bind on address`:
  port 8099 is taken. Free it — `Get-NetTCPConnection -LocalPort 8099 -State Listen` then
  `Stop-Process -Id <pid> -Force` — or pass `--port 8123`.
- **`no panel output at dev/state/mock/current.png`**: the scheduler never completed a
  show. Almost always quiet hours (see Gotchas) or an empty library — run `seed` first.
- **`window.Alpine is undefined`**: `static/app.js` must load *before* the Alpine CDN
  script in `base.html`. Alpine's CDN build fires `alpine:init` synchronously on execute,
  so a registration script loaded after it never runs.
- **`no Chrome/Chromium found`**: set `VIBEFRAME_CHROME` to the binary. Only `shots` and
  `smoke` need it; `api`, `panel`, and `render` do not.
- **`library scan complete: 0 images`**: `VIBEFRAME_PHOTOS_DIR` is wrong or empty. The
  driver's paths are relative to the repo root, not to the skill directory.
