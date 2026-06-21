from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from PIL import Image

from vibeframe.cache import Cache
from vibeframe.db import build_engine
from vibeframe.display.mock_driver import MockDriver
from vibeframe.library import ImageLibrary
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
    )
    return create_app(state), library


async def _upload(client: httpx.AsyncClient, path: Path) -> httpx.Response:
    with path.open("rb") as f:
        return await client.post(
            "/images/upload",
            files={"file": (path.name, f, "image/jpeg")},
        )


def test_upload_writes_to_uploads_dir(tmp_path: Path, tmp_settings):
    tmp_settings.ensure_dirs()
    app, library = _setup(tmp_settings)

    fixture = tmp_path / "src.jpg"
    Image.new("RGB", (64, 64), (200, 50, 30)).save(fixture, "JPEG")

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await _upload(client, fixture)
            assert r.status_code == 200, r.text
            uploaded = Path(r.json()["path"])
            assert uploaded.parent == tmp_settings.upload_dir
            assert uploaded.exists()

            health = await client.get("/healthz")
            assert health.status_code == 200

            listing = library.list(limit=10)
            assert any(uploaded.name in img.path for img in listing)
            preview = await client.get(f"/images/{listing[0].id}/preview.png")
            assert preview.status_code == 200
            assert preview.headers["content-type"] == "image/png"

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
