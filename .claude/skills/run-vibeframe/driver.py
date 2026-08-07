#!/usr/bin/env python3
"""Launch and drive vibeFrame headlessly, with no display hardware.

vibeFrame's whole job is to push a dithered image onto an e-paper panel, so the
mock driver (VIBEFRAME_DRIVER=mock) is the interaction surface that matters: it
writes exactly what the panel would show to <root>/state/mock/current.png. This
driver seeds a photo library, boots the app, drives the HTTP surface, asserts the
panel output is a real Spectra-6 frame, and drives the web UI over the Chrome
DevTools Protocol so JS errors surface instead of failing silently.

Runs on the project venv only -- httpx is a direct dependency and websockets
arrives with uvicorn[standard]. Nothing to pip install.

  python .claude/skills/run-vibeframe/driver.py smoke     # everything, exit != 0 on failure
  python .claude/skills/run-vibeframe/driver.py up        # boot and leave running
  python .claude/skills/run-vibeframe/driver.py shots     # screenshot the 4 pages
  python .claude/skills/run-vibeframe/driver.py render X  # run one image through the pipeline
  python .claude/skills/run-vibeframe/driver.py down
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = REPO / "dev"
DEFAULT_PORT = 8099
STATE_FILE = DEFAULT_ROOT / ".driver.json"

# The panel is 800x480 and can only physically show these six inks.
PANEL_SIZE = (800, 480)
PAGES = ["/", "/images", "/settings", "/metrics"]


def log(msg: str) -> None:
    print(f"[driver] {msg}", flush=True)


def die(msg: str) -> NoReturn:
    print(f"[driver] FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def venv_python() -> Path:
    """The project venv's interpreter. Falls back to the current one."""
    for rel in ("Scripts/python.exe", "bin/python"):
        p = REPO / ".venv" / rel
        if p.exists():
            return p
    return Path(sys.executable)


def app_env(root: Path, port: int) -> dict:
    """Env for a hardware-free run.

    Quiet hours MUST be off: they default to 22:00-07:00, and inside that window
    the scheduler skips every refresh, so a run after 22:00 local would sit there
    producing no frames and look like a hang. Note the env var alone is not
    sufficient -- see clear_persisted_settings(), which the DB can otherwise
    override at boot.
    """
    env = dict(os.environ)
    env.update(
        VIBEFRAME_DRIVER="mock",
        VIBEFRAME_PHOTOS_DIR=str(root / "photos"),
        VIBEFRAME_STATE_DIR=str(root / "state"),
        VIBEFRAME_CACHE_DIR=str(root / "cache"),
        VIBEFRAME_REFRESH_SECONDS="20",
        VIBEFRAME_QUIET_HOURS_ENABLED="false",
        VIBEFRAME_WEB_PORT=str(port),
        VIBEFRAME_LOG_LEVEL="INFO",
        # Loopback only. The app's own default is 0.0.0.0 and web_token defaults
        # to None, so inheriting it would publish an unauthenticated admin
        # surface (POST /settings, DELETE /images/{id}) to whatever network the
        # machine is on. Every client in this file talks to 127.0.0.1 anyway.
        VIBEFRAME_WEB_HOST="127.0.0.1",
        # The child runs with cwd=REPO, where Settings reads .env. If that .env
        # carries a deployment token, every write endpoint the driver calls
        # would 401. Blank it: require_token() returns early on a falsy token.
        VIBEFRAME_WEB_TOKEN="",
        PYTHONUNBUFFERED="1",
    )
    return env


def clear_persisted_settings(root: Path) -> None:
    """Drop rows from the scratch DB's `setting` table before boot.

    __main__._restore_persisted_settings overlays this table on top of the
    env-derived Settings, so anyone who has ever hit Save on the settings page
    against this dev root would otherwise silently override the env above --
    including re-enabling quiet hours, which stalls the scheduler entirely.
    Only ever touches the driver's own scratch DB under `root`.
    """
    import sqlite3

    db = root / "state" / "vibeframe.db"
    if not db.exists():
        return
    try:
        with sqlite3.connect(db) as c:
            n = c.execute("delete from setting").rowcount
        if n:
            log(f"cleared {n} persisted setting(s) so env values win")
    except sqlite3.Error as e:
        log(f"could not clear persisted settings ({e}); env may be overridden")


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------


def seed(root: Path, count: int = 4) -> Path:
    """Write synthetic photos covering each aspect ratio the smart crop handles."""
    from PIL import Image, ImageDraw

    photos = root / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    specs = [
        ("landscape", 2400, 1600, (30, 90, 160)),
        ("portrait", 1200, 1800, (160, 60, 40)),
        ("square", 1500, 1500, (40, 140, 70)),
        ("wide", 3000, 1200, (150, 120, 30)),
    ][:count]

    for name, w, h, base in specs:
        im = Image.new("RGB", (w, h), base)
        dr = ImageDraw.Draw(im)
        # Hard edges + a bright blob: dithering artifacts and crop centering are
        # both obvious by eye in the resulting frame.
        for i in range(0, w, max(1, w // 12)):
            dr.rectangle([i, h // 3, i + w // 24, 2 * h // 3], fill=((i * 7) % 256, (i * 3) % 256, 200))
        dr.ellipse([w // 2 - w // 8, h // 2 - h // 8, w // 2 + w // 8, h // 2 + h // 8], fill=(250, 240, 220))
        dr.text((40, 40), name, fill=(255, 255, 255))
        im.save(photos / f"{name}.jpg", quality=88)

    log(f"seeded {len(specs)} photos into {photos}")
    return photos


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def wait_health(port: int, timeout: float = 90.0) -> None:
    import httpx

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=3)
            if r.status_code == 200:
                log(f"healthy on :{port} ({r.json()})")
                return
            last = f"HTTP {r.status_code}"
        except Exception as e:  # server not up yet
            last = type(e).__name__
        time.sleep(1)
    die(f"app never became healthy on :{port} (last: {last})")


def port_is_serving(port: int) -> bool:
    import httpx

    try:
        return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200
    except Exception:
        return False


def pid_cmdline(pid: int) -> str:
    """Best-effort command line for a pid, '' if unknown.

    Used to confirm a recorded pid is still *our* app before killing it. The OS
    recycles pids, and `taskkill /F /T` on a recycled pid would take down an
    unrelated process and its entire child tree.
    """
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20,
            )
            return (out.stdout or "").strip()
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ")
    except Exception:
        return ""


def up(root: Path, port: int, quiet: bool = False) -> subprocess.Popen:
    root.mkdir(parents=True, exist_ok=True)

    # Refuse to start on top of a live instance. Truncating its log and
    # overwriting its pid record would orphan it holding the port, with no
    # handle left to stop it.
    if port_is_serving(port):
        die(f"something is already serving :{port} -- run 'driver.py down', or use --port")

    clear_persisted_settings(root)

    logfile = root / "app.log"
    # Deliberately not a context manager: the handle must outlive this function
    # and stay open for the child's lifetime.
    fh = open(logfile, "wb")  # noqa: SIM115
    proc = subprocess.Popen(
        [str(venv_python()), "-m", "vibeframe"],
        cwd=str(REPO),
        env=app_env(root, port),
        stdout=fh,
        stderr=subprocess.STDOUT,
    )
    if not quiet:
        log(f"started pid={proc.pid}, log -> {logfile}")

    # A crash during boot (bad env, port in use) shows up here rather than as a
    # confusing health timeout.
    time.sleep(2)
    if proc.poll() is not None:
        tail = logfile.read_text(errors="replace")[-2000:]
        die(f"app exited immediately (rc={proc.returncode}):\n{tail}")

    wait_health(port)
    # Recorded only once the process is confirmed alive and serving, so a failed
    # boot can never replace the handle to a working one.
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"pid": proc.pid, "port": port, "started_at": time.time()}))
    return proc


def down(proc: subprocess.Popen | None = None) -> None:
    pid = None
    if proc is not None:
        pid = proc.pid
    elif STATE_FILE.exists():
        pid = json.loads(STATE_FILE.read_text()).get("pid")

    if pid is None:
        log("nothing to stop")
        return

    # A pid we did not spawn in this process must be proven to still be the app
    # before we force-kill it -- pids get recycled, and nothing clears the state
    # file across a reboot.
    if proc is None:
        cmd = pid_cmdline(pid)
        if not cmd:
            log(f"pid {pid} is not running; clearing stale state file")
            STATE_FILE.unlink(missing_ok=True)
            return
        # Match the module invocation, not a bare "vibeframe" substring: the
        # interpreter itself lives at <repo>/.venv/Scripts/python.exe, so the
        # repo name appears in the cmdline of *every* process started from this
        # venv and would wave through an unrelated one.
        if "-m vibeframe" not in " ".join(cmd.split()).lower():
            STATE_FILE.unlink(missing_ok=True)
            die(f"pid {pid} is not vibeframe (cmdline: {cmd[:120]!r}); refusing to kill it")

    # Windows has no SIGTERM for this process; __main__ installs signal handlers
    # via loop.add_signal_handler, which raises NotImplementedError there and is
    # suppressed. terminate()/taskkill is the portable stop.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        elif proc is not None:
            proc.terminate()
        else:
            os.kill(pid, 15)
        log(f"stopped pid={pid}")
    except Exception as e:
        log(f"stop failed ({e}); pid {pid} may already be gone")
    STATE_FILE.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# assertions
# --------------------------------------------------------------------------


def check_api(port: int) -> None:
    import httpx

    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base, timeout=30) as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}, f"/healthz -> {r.status_code} {r.text[:200]}"

        r = c.get("/system/status")
        body = r.json()
        assert r.status_code == 200 and "label" in body, f"/system/status -> {r.text[:200]}"
        assert body.get("in_quiet") is False, "quiet hours active -- the scheduler will not refresh"
        log(f"status: {body}")

        for page in PAGES:
            r = c.get(page)
            assert r.status_code == 200, f"{page} -> HTTP {r.status_code}"
            assert "<html" in r.text.lower(), f"{page} did not return an HTML document"
        log(f"pages OK: {', '.join(PAGES)}")

        # Baseline BEFORE triggering. __main__ starts scheduler.run() at boot and
        # its first _step() renders immediately, so a bare "done and shown_at is
        # set" poll is already satisfied by that boot render -- meaning a
        # /system/next that had regressed to a no-op would still pass.
        before = c.get("/system/render-status").json().get("refresh", {})
        baseline_shown = before.get("shown_at")

        assert c.post("/system/next").status_code == 200, "POST /system/next failed"
        deadline = time.time() + 90
        while time.time() < deadline:
            st = c.get("/system/render-status").json().get("refresh", {})
            if st.get("failed"):
                die(f"render failed: {st.get('error')}")
            if st.get("done") and st.get("shown_at") and st.get("shown_at") != baseline_shown:
                log(f"rendered+shown: {st.get('image_path')} (shown_at advanced)")
                break
            time.sleep(1)
        else:
            die(
                "POST /system/next did not produce a NEW show within 90s "
                f"(shown_at stuck at {baseline_shown!r}) -- the endpoint may be a no-op"
            )

        m = c.get("/metrics.json").json()
        assert "driver.mock.show" in m, "no panel-write metric recorded"
        log(f"panel writes: {m['driver.mock.show']['count']}, last {m['driver.mock.show']['last_ms']:.0f} ms")


def check_panel(root: Path, newer_than: float | None = None) -> None:
    """The frame the panel would physically display must be exactly 800x480 and
    contain only the six Spectra-6 inks. Anything else means the palette or
    dither stage regressed.

    `newer_than` is a unix timestamp the frame must post-date. Without it this
    check happily validates a PNG left in the gitignored dev/ tree by a run days
    ago -- which is the one way this assertion could report green while the
    palette it exists to guard is broken.
    """
    from PIL import Image

    from vibeframe.processor.palette import SPECTRA6

    cur = root / "state" / "mock" / "current.png"
    if not cur.exists():
        die(f"no panel output at {cur} -- the scheduler never completed a show")

    if newer_than is None and STATE_FILE.exists():
        # Standalone `panel` run: fall back to the tracked app's start time.
        with contextlib.suppress(Exception):
            newer_than = json.loads(STATE_FILE.read_text()).get("started_at")

    age_src = cur.stat().st_mtime
    if newer_than is None:
        log("WARNING: cannot verify frame freshness (no running app tracked); it may be stale")
    elif age_src < newer_than:
        die(
            f"panel frame is stale: {cur} was written {newer_than - age_src:.0f}s "
            "before this run started -- no new frame was produced"
        )

    im = Image.open(cur).convert("RGB")
    if im.size != PANEL_SIZE:
        die(f"panel frame is {im.size}, expected {PANEL_SIZE}")

    colors = {c for _, c in im.getcolors(maxcolors=1 << 24)}
    pal = {tuple(c) for c in SPECTRA6}
    extra = colors - pal
    if extra:
        die(f"frame contains {len(extra)} non-palette colors, e.g. {sorted(extra)[:5]}")

    frames = sorted((root / "state" / "mock").glob("frame-*.png"))
    log(f"panel OK: {im.size}, {len(colors)}/6 inks used, {len(frames)} frames written -> {cur}")


# --------------------------------------------------------------------------
# browser (Chrome DevTools Protocol)
# --------------------------------------------------------------------------


def find_chrome() -> str:
    if os.environ.get("VIBEFRAME_CHROME"):
        return os.environ["VIBEFRAME_CHROME"]
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    die("no Chrome/Chromium found; set VIBEFRAME_CHROME=/path/to/chrome")


class Browser:
    """Minimal CDP client: navigate, evaluate, click, screenshot, collect errors.

    Deliberately not Playwright -- this needs zero installs beyond the project
    venv, which matters because the only reason to run a browser here is to
    prove the page's JS actually initialised.
    """

    def __init__(self, port: int = 9333):
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self._id = 0
        self.errors: list[str] = []

    def launch(self, profile: Path) -> None:
        import httpx

        # A leftover Chrome from a crashed run still answers on this port. Without
        # this check we would "connect" to it, drive the wrong browser, and
        # screenshot a stale page while reporting success.
        port_busy = False
        with contextlib.suppress(Exception):
            httpx.get(f"http://127.0.0.1:{self.port}/json/version", timeout=2)
            port_busy = True
        if port_busy:
            die(
                f"a browser is already on debug port {self.port} (leaked from an earlier run); "
                "close it and retry"
            )

        # Absolute: Chrome silently fails to bring up the debug port when
        # --user-data-dir is relative.
        profile = profile.resolve()
        profile.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                find_chrome(),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/json/version", timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        die("Chrome did not open its debugging port")

    async def connect(self) -> None:
        import httpx
        import websockets

        targets = httpx.get(f"http://127.0.0.1:{self.port}/json/list", timeout=5).json()
        page = next((t for t in targets if t["type"] == "page"), None)
        if not page:
            die("no page target in Chrome")
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
        for method in ("Page.enable", "Runtime.enable", "Log.enable"):
            await self.send(method)

    async def send(self, method: str, **params) -> dict:
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            self._note(msg)
            if msg.get("id") == mid:
                if "error" in msg:
                    die(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def _note(self, msg: dict) -> None:
        """Record page-side JS failures. This is the point of using CDP at all:
        base.html loads static/app.js before the Alpine CDN, and if that order is
        ever flipped every x-data component dies with '<component> is not
        defined' while the HTML still renders and screenshots look fine."""
        m = msg.get("method")
        if m == "Runtime.exceptionThrown":
            d = msg["params"]["exceptionDetails"]
            self.errors.append(d.get("exception", {}).get("description") or d.get("text", "exception"))
        elif m == "Log.entryAdded":
            e = msg["params"]["entry"]
            if e.get("level") == "error":
                self.errors.append(f"{e.get('source')}: {e.get('text')}")
        elif m == "Runtime.consoleAPICalled":
            # htmx reports swap/target/status failures through console.error
            # rather than by throwing, so without this a "Show next now" that
            # started 500ing would leave the page looking clean.
            p = msg["params"]
            if p.get("type") in ("error", "assert"):
                parts = [
                    a.get("description") or json.dumps(a.get("value"), default=str)
                    for a in p.get("args", [])
                ]
                self.errors.append("console.error: " + " ".join(x for x in parts if x))

    async def goto(self, url: str, settle: float = 2.5) -> None:
        await self.send("Page.navigate", url=url)
        await asyncio.sleep(settle)  # let HTMX/Alpine finish their first pass
        # Drain any events that arrived after navigation settled.
        try:
            while True:
                self._note(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=0.2)))
        except Exception:
            pass

    async def eval(self, expr: str):
        r = await self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def shot(self, path: Path, width: int = 1280, height: int = 1400) -> Path:
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            width=width, height=height, deviceScaleFactor=1, mobile=False,
        )
        r = await self.send("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(r["data"]))
        return path

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
        if self.proc:
            self.proc.terminate()


async def _shots(root: Path, port: int) -> list[str]:
    b = Browser()
    out = root / "shots"
    # launch/connect live INSIDE the try: a failure in connect() (Chrome up but no
    # page target yet) would otherwise skip close() and leak a headless Chrome
    # holding the debug port and the profile lock.
    try:
        b.launch(root / "chrome-profile")
        await b.connect()
        for page in PAGES:
            await b.goto(f"http://127.0.0.1:{port}{page}")
            name = "home" if page == "/" else page.strip("/").replace("/", "-")
            p = await b.shot(out / f"{name}.png")
            log(f"shot {page} -> {p}")

        # Alpine is what powers the status chip and every settings control; if it
        # failed to register, this is undefined and the UI is inert.
        await b.goto(f"http://127.0.0.1:{port}/")
        alpine = await b.eval("typeof window.Alpine !== 'undefined'")
        assert alpine, "window.Alpine is undefined -- Alpine never initialised"

        # Click the real button rather than POSTing the endpoint, so the HTMX
        # wiring (hx-post + target swap) is exercised too.
        before = await b.eval("document.querySelector('.hero-caption .when')?.textContent || ''")
        clicked = await b.eval(
            "(() => { const b=[...document.querySelectorAll('button')]"
            ".find(x=>/show next/i.test(x.textContent)); if(!b) return false; b.click(); return true; })()"
        )
        assert clicked, "could not find the 'Show next now' button"

        # Poll for the caption to actually advance instead of sleeping a fixed
        # 8s and merely logging the result -- otherwise a click that silently
        # does nothing (bad hx-target, endpoint 500ing) still "passes".
        after = before
        for _ in range(40):
            await asyncio.sleep(1)
            after = await b.eval("document.querySelector('.hero-caption .when')?.textContent || ''")
            if after and after != before:
                break
        assert after != before, (
            f"clicking 'Show next now' did not change the caption (still {before!r}) "
            "-- the HTMX post or its swap is broken"
        )
        log(f"clicked 'Show next now': caption {before!r} -> {after!r}")
        await b.shot(out / "home-after-next.png")
        return b.errors
    finally:
        await b.close()


def shots(root: Path, port: int, allow_js_errors: bool = False) -> None:
    errors = asyncio.run(_shots(root, port))
    # Favicon 404s are noise in a local run; anything else is a real page error.
    real = [e for e in errors if "favicon" not in e.lower()]
    if real:
        for e in real:
            print(f"[driver]   JS ERROR: {e}", file=sys.stderr)
        if not allow_js_errors:
            die(f"{len(real)} JS error(s) on the page")
        log(f"{len(real)} JS error(s) tolerated (--allow-js-errors)")
    else:
        log("no JS errors on any page")


# --------------------------------------------------------------------------
# direct invocation
# --------------------------------------------------------------------------


def render_one(src: Path, out: Path) -> None:
    """Run one image through the pipeline with no server, no scheduler, no DB.

    This is the fast path for changes under processor/ -- crop, tonemap, dither,
    palette. Seconds instead of a full boot.
    """
    from vibeframe.config import Settings
    from vibeframe.processor import pipeline

    settings = Settings(_env_file=None)  # ignore any .env sitting in the repo
    pipeline.configure_pillow(settings.max_image_pixels)
    t0 = time.perf_counter()
    result = pipeline.process(src, settings)
    dt = (time.perf_counter() - t0) * 1000
    out.parent.mkdir(parents=True, exist_ok=True)
    result.image.convert("RGB").save(out)
    log(f"{src.name}: {result.image.size} mode={result.image.mode} in {dt:.0f} ms -> {out}")


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["smoke", "seed", "up", "down", "api", "panel", "shots", "render"])
    ap.add_argument("arg", nargs="?", help="render: source image path")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--keep", action="store_true", help="smoke: leave the app running")
    ap.add_argument(
        "--allow-js-errors",
        action="store_true",
        help="downgrade page JS errors to warnings (see Gotchas: known $dispatch bug)",
    )
    a = ap.parse_args()

    root: Path = a.root.resolve()

    if a.cmd == "seed":
        seed(root)
    elif a.cmd == "up":
        up(root, a.port)
        log(f"running -> http://127.0.0.1:{a.port}  (stop with: driver.py down)")
    elif a.cmd == "down":
        down()
    elif a.cmd == "api":
        check_api(a.port)
    elif a.cmd == "panel":
        check_panel(root)
    elif a.cmd == "shots":
        shots(root, a.port, a.allow_js_errors)
    elif a.cmd == "render":
        if not a.arg:
            die("render needs a source image path")
        render_one(Path(a.arg), root / "shots" / "render.png")
    elif a.cmd == "smoke":
        seed(root)
        started = time.time()
        proc = up(root, a.port)
        try:
            check_api(a.port)
            # Every frame this run produces must post-date `started`, so a PNG
            # left in dev/ by an earlier run cannot satisfy the palette check.
            check_panel(root, newer_than=started)
            shots(root, a.port, a.allow_js_errors)
        finally:
            if not a.keep:
                down(proc)
        log("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
