from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from PIL import Image

from vibeframe.cache import Cache
from vibeframe.db import build_engine
from vibeframe.display.mock_driver import MockDriver
from vibeframe.library import ImageLibrary
from vibeframe.progress import RenderTracker
from vibeframe.scheduler import Scheduler
from vibeframe.web.app import create_app
from vibeframe.web.deps import AppState


def _setup(tmp_settings):
    tmp_settings.photos_dir.mkdir(parents=True, exist_ok=True)
    engine = build_engine(tmp_settings.db_path)
    cache = Cache(tmp_settings.cache_dir, tmp_settings.cache_max_bytes)
    library = ImageLibrary(tmp_settings.photos_dir, engine, cache=cache)
    library.scan()
    driver = MockDriver(tmp_settings.mock_dir, orientation=tmp_settings.orientation)
    scheduler = Scheduler(tmp_settings, library, cache, driver, engine)
    state = AppState(
        settings=tmp_settings,
        library=library,
        cache=cache,
        scheduler=scheduler,
        driver=driver,
        engine=engine,
        preview_tracker=RenderTracker(),
    )
    return create_app(state), library


async def _upload(client: httpx.AsyncClient, *paths: Path, hx: bool = False) -> httpx.Response:
    files = []
    handles = []
    try:
        for p in paths:
            h = p.open("rb")
            handles.append(h)
            files.append(("files", (p.name, h, "image/jpeg")))
        headers = {"HX-Request": "true"} if hx else {}
        return await client.post("/images/upload", files=files, headers=headers)
    finally:
        for h in handles:
            h.close()


def test_upload_multi_file_writes_all(tmp_path: Path, tmp_settings):
    tmp_settings.ensure_dirs()
    app, library = _setup(tmp_settings)

    fixtures = []
    for i in range(3):
        p = tmp_path / f"src{i}.jpg"
        Image.new("RGB", (64, 64), (50 + i * 50, 50, 200)).save(p, "JPEG")
        fixtures.append(p)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await _upload(client, *fixtures)
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["saved"]) == 3
            assert body["errors"] == []
            listing = library.list(limit=10)
            saved_names = {Path(p).name for p in body["saved"]}
            on_disk = {Path(img.path).name for img in listing}
            for name in saved_names:
                assert any(name in n for n in on_disk)

    asyncio.run(run())


def test_upload_hx_request_returns_html_fragment(tmp_path: Path, tmp_settings):
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    fixture = tmp_path / "hx.jpg"
    Image.new("RGB", (32, 32), (10, 20, 30)).save(fixture, "JPEG")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await _upload(client, fixture, hx=True)
            assert r.status_code == 200, r.text
            assert "text/html" in r.headers["content-type"]
            assert "toast-ok" in r.text
            assert "Uploaded 1 file" in r.text

    asyncio.run(run())


def test_upload_rejects_unsupported_extension(tmp_path: Path, tmp_settings):
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    bad = tmp_path / "not-an-image.txt"
    bad.write_bytes(b"hello")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await _upload(client, bad)
            assert r.status_code == 200  # endpoint returns per-file errors
            body = r.json()
            assert body["saved"] == []
            assert len(body["errors"]) == 1
            assert "unsupported" in body["errors"][0]

    asyncio.run(run())


def test_test_pattern_endpoint(tmp_settings):
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/system/test-pattern.png")
            assert r.status_code == 200
            assert r.headers["content-type"] == "image/png"

    asyncio.run(run())


def test_html_pages_render(tmp_settings):
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/", "/images", "/settings"):
                r = await client.get(path)
                assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
                assert "text/html" in r.headers["content-type"]

    asyncio.run(run())
