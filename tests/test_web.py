from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
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


def test_settings_push_prompt_only_on_render_change(tmp_settings):
    """The 'push to frame?' prompt (pushable=1) must appear only when a
    render-affecting setting changed — not for schedule/UI-only saves like the
    metrics refresh interval."""
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    # All current (default) values; we vary one field per request.
    base = {
        "orientation": 270,
        "refresh_minutes": 30,
        "selection_mode": "shuffle",
        "dither": "floyd-steinberg",
        "crop_mode": "smart",
        "saturation": 1.15,
        "contrast": 1.05,
        "quiet_hours_enabled": "true",
        "quiet_start": "22:00",
        "quiet_end": "07:00",
        "metrics_refresh_seconds": 10,
        "cache_max_mb": 500,
        "backup_keep": 5,
    }

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            # Non-render change (metrics refresh) → no push prompt.
            r = await client.post("/settings", data={**base, "metrics_refresh_seconds": 20})
            assert r.status_code == 303, r.text
            assert "pushable=1" not in r.headers["location"]

            # Render-affecting change (saturation) → push prompt.
            r = await client.post("/settings", data={**base, "saturation": 2.0})
            assert r.status_code == 303, r.text
            assert "pushable=1" in r.headers["location"]

    asyncio.run(run())


def test_backup_keep_persists_under_its_documented_key(tmp_settings):
    """scripts/vibeframe-backup.sh reads this value straight out of the DB with
    `select value from setting where key = 'backup_keep'`, so the key name and
    the string encoding are a contract with a shell script that no other test
    would catch breaking."""
    from vibeframe.db import get_setting

    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)
    engine = app.state.app_state.engine

    base = {
        "orientation": 270,
        "refresh_minutes": 30,
        "selection_mode": "shuffle",
        "dither": "floyd-steinberg",
        "crop_mode": "smart",
        "saturation": 1.15,
        "contrast": 1.05,
        "quiet_hours_enabled": "true",
        "quiet_start": "22:00",
        "quiet_end": "07:00",
        "metrics_refresh_seconds": 10,
        "cache_max_mb": 500,
        "backup_keep": 5,
    }

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            r = await client.post("/settings", data={**base, "backup_keep": 9})
            assert r.status_code == 303, r.text
            assert get_setting(engine, "backup_keep") == "9"
            assert tmp_settings.backup_keep == 9

            # Rotation with 0 would delete every snapshot including the new one,
            # so the floor is enforced server-side, not just in the shell script.
            r = await client.post("/settings", data={**base, "backup_keep": 0})
            assert r.status_code == 422, r.text
            assert get_setting(engine, "backup_keep") == "9"

    asyncio.run(run())


def _collections(app):
    from vibeframe.db import list_collections

    return list_collections(app.state.app_state.engine)


def test_collection_crud_round_trip(tmp_path: Path, tmp_settings):
    """Create, edit weight + season, then delete — the editor's whole loop."""
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            r = await client.post("/collections", data={"name": "Christmas"})
            assert r.status_code == 303, r.text
            names = [c.name for c in _collections(app)]
            assert names == ["Favorites", "Christmas"], "default sorts first"

            xmas = next(c for c in _collections(app) if c.name == "Christmas")
            r = await client.post(
                f"/collections/{xmas.id}",
                data={
                    "name": "Christmas", "weight": "2", "boost": "4",
                    "start_month": "12", "start_day": "1",
                    "end_month": "12", "end_day": "25",
                },
            )
            assert r.status_code == 303, r.text
            xmas = next(c for c in _collections(app) if c.id == xmas.id)
            assert (xmas.weight, xmas.boost) == (2.0, 4.0)
            assert (xmas.start_month, xmas.start_day) == (12, 1)
            assert (xmas.end_month, xmas.end_day) == (12, 25)

            # Clearing every season field is how "all year" is expressed.
            r = await client.post(
                f"/collections/{xmas.id}",
                data={"name": "Christmas", "weight": "2", "boost": "4",
                      "start_month": "", "start_day": "", "end_month": "", "end_day": ""},
            )
            assert r.status_code == 303, r.text
            assert next(c for c in _collections(app) if c.id == xmas.id).start_month is None

            r = await client.post(f"/collections/{xmas.id}/delete")
            assert r.status_code == 303, r.text
            assert [c.name for c in _collections(app)] == ["Favorites"]

    asyncio.run(run())


def test_collection_input_is_validated(tmp_path: Path, tmp_settings):
    """A half-filled season would silently never match, and a duplicate name
    would put two identical rows in the picker."""
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            await client.post("/collections", data={"name": "Halloween"})
            r = await client.post("/collections", data={"name": "Halloween"})
            assert r.status_code == 422, r.text

            hw = next(c for c in _collections(app) if c.name == "Halloween")
            base = {"name": "Halloween", "weight": "1", "boost": "3"}

            # Start set, end blank.
            r = await client.post(
                f"/collections/{hw.id}",
                data={**base, "start_month": "10", "start_day": "1",
                      "end_month": "", "end_day": ""},
            )
            assert r.status_code == 422, r.text

            # Feb 30 does not exist in any year.
            r = await client.post(
                f"/collections/{hw.id}",
                data={**base, "start_month": "2", "start_day": "30",
                      "end_month": "3", "end_day": "1"},
            )
            assert r.status_code == 422, r.text

            r = await client.post(f"/collections/{hw.id}", data={**base, "weight": "-1"})
            assert r.status_code == 422, r.text

    asyncio.run(run())


def test_favorites_collection_cannot_be_removed_through_the_api(tmp_settings):
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)
    favorites = _collections(app)[0]

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            r = await client.post(f"/collections/{favorites.id}/delete")
            assert r.status_code == 409, r.text
            r = await client.post(
                f"/collections/{favorites.id}",
                data={"name": "Starred", "weight": "1", "boost": "3"},
            )
            assert r.status_code == 409, r.text
            # Its other fields stay editable.
            r = await client.post(
                f"/collections/{favorites.id}",
                data={"name": favorites.name, "weight": "5", "boost": "3"},
            )
            assert r.status_code == 303, r.text

    asyncio.run(run())


def test_library_filters_by_collection_and_survives_a_deleted_one(tmp_path: Path, tmp_settings):
    """?collection_id=N narrows the grid; a stale id falls back to the whole
    library rather than rendering an unexplained empty grid."""
    tmp_settings.ensure_dirs()
    photos = tmp_settings.photos_dir
    photos.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        Image.new("RGB", (8, 8), (i * 20, 30, 40)).save(photos / f"p{i}.jpg", "JPEG")
    app, library = _setup(tmp_settings)
    ids = sorted(i.id for i in library.list(limit=10))

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/collections", data={"name": "Iceland"})
            trip = next(c for c in _collections(app) if c.name == "Iceland")

            r = await client.post(f"/collections/{trip.id}/images/{ids[0]}")
            assert r.status_code == 200, r.text

            r = await client.get("/images", params={"collection_id": trip.id})
            assert r.text.count('class="photo-card"') == 1
            assert "Iceland" in r.text

            r = await client.delete(f"/collections/{trip.id}/images/{ids[0]}")
            assert r.status_code == 200, r.text
            r = await client.get("/images", params={"collection_id": trip.id})
            assert r.text.count('class="photo-card"') == 0

            # Stale id → whole library, not an empty grid.
            r = await client.get("/images", params={"collection_id": 99999})
            assert r.text.count('class="photo-card"') == 3

    asyncio.run(run())


def test_next_partial_url_encodes_filters():
    """The sentinel URL goes through urlencode, unlike the pager's Jinja macro:
    a search term containing & has to survive the round trip."""
    from vibeframe.web.routes.images import _next_partial_url

    url = _next_partial_url(offset=0, limit=24, favorites_only=True, q="a&b", sort="name")
    assert url.startswith("/images?")
    assert "offset=24" in url
    assert "limit=24" in url
    assert "partial=true" in url
    assert "favorites_only=true" in url
    assert "q=a%26b" in url
    assert "sort=name" in url

    # Defaults stay out of the URL so it reads like the ones a human writes.
    plain = _next_partial_url(offset=0, limit=24, favorites_only=False, q=None, sort="newest")
    assert "favorites_only" not in plain
    assert "q=" not in plain
    assert "sort" not in plain


def test_library_paginates_on_desktop_and_streams_on_mobile(tmp_path: Path, tmp_settings):
    """The first page carries both affordances — a pager CSS hides on mobile and
    a sentinel whose htmx trigger filter holds it inert on desktop — and
    ?partial=true returns bare cards for the sentinel to append."""
    tmp_settings.ensure_dirs()
    photos = tmp_settings.photos_dir
    photos.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        Image.new("RGB", (8, 8), (i * 10, i * 10, i * 10)).save(photos / f"p{i}.jpg", "JPEG")

    app, library = _setup(tmp_settings)
    assert library.count() == 3

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/images", params={"limit": 2})
            assert first.status_code == 200
            assert first.text.count('class="photo-card"') == 2
            assert 'class="pager library-pager"' in first.text
            assert 'class="scroll-sentinel"' in first.text
            # Mobile-only gate. If this filter ever disappears, desktop silently
            # turns into an infinite scroll.
            assert "matchMedia('(max-width: 720px)')" in first.text

            last = await client.get("/images", params={"limit": 2, "offset": 2, "partial": "true"})
            assert last.status_code == 200
            # Bare cards: no layout chrome to nest inside the existing page.
            assert "<html" not in last.text
            assert "app-header" not in last.text
            assert last.text.count('class="photo-card"') == 1
            # Last page — the chain has to stop, or the sentinel loops forever.
            assert 'class="scroll-sentinel"' not in last.text

    asyncio.run(run())


def test_html_pages_render(tmp_settings):
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in (
                "/", "/images", "/collections", "/settings", "/metrics", "/metrics/fragment"
            ):
                r = await client.get(path)
                assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"
                assert "text/html" in r.headers["content-type"]

    asyncio.run(run())


def test_upload_rejects_oversized_file(tmp_path: Path, tmp_settings):
    """POST /images/upload must enforce the max_upload_mb cap and leave no
    partial file behind."""
    tmp_settings.ensure_dirs()
    app, _ = _setup(tmp_settings)
    tmp_settings.max_upload_mb = 1

    big = tmp_path / "big.jpg"
    big.write_bytes(os.urandom(2 * 1024 * 1024))

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await _upload(client, big)
            assert r.status_code == 200
            body = r.json()
            assert body["saved"] == []
            assert any("exceeds" in e for e in body["errors"])

    asyncio.run(run())
    leftover = list(tmp_settings.upload_dir.glob("*")) if tmp_settings.upload_dir.exists() else []
    assert leftover == []


def test_symlink_escape_is_never_indexed_or_deletable(tmp_path: Path, tmp_settings):
    """Two layers, both required.

    A symlink pointing out of photos_dir must not be indexed at all — indexing
    one produces a row every route then refuses, i.e. a broken tile that can't
    be cleared. And a row that predates that filter (or was hand-written) must
    still not be usable to delete the file it points at."""
    from vibeframe.db import upsert_images

    tmp_settings.ensure_dirs()
    photos = tmp_settings.photos_dir
    photos.mkdir(parents=True, exist_ok=True)

    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(outside, "JPEG")
    escape = photos / "escape.jpg"
    try:
        os.symlink(outside, escape)
    except OSError as e:
        pytest.skip(f"symlinks unavailable here: {e}")

    app, library = _setup(tmp_settings)
    assert library.count() == 0, "escaping symlink must not enter the library"

    stat = escape.stat()
    upsert_images(
        library.engine,
        [{"path": str(escape), "sha256": "0" * 64, "mtime": stat.st_mtime, "size": stat.st_size}],
    )
    image_id = library.list(limit=1)[0].id

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.delete(f"/images/{image_id}")
            return r

    r = asyncio.run(run())
    assert r.status_code == 404
    assert outside.exists()
    assert library.count() == 1


def test_thumb_route_shares_the_warmers_cache_key(tmp_path: Path, tmp_settings):
    """The thumb route and ThumbWarmer must derive the same cache path.

    thumb_cache_path hashes the path *string*, and the warmer feeds it the
    stored DB path. If the route resolved the path first, a photos_dir reached
    through a symlink would key differently and every warmed thumbnail would be
    missed and regenerated per request."""
    from vibeframe.thumb_warmer import generate_thumb, thumb_cache_path

    real = tmp_path / "real-photos"
    real.mkdir()
    link = tmp_path / "photos-link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"symlinks unavailable here: {e}")
    tmp_settings.photos_dir = link
    tmp_settings.ensure_dirs()

    Image.new("RGB", (64, 64), (10, 20, 30)).save(real / "thumbme.jpg", "JPEG")
    app, library = _setup(tmp_settings)
    img = library.list(limit=1)[0]

    # Warm exactly as ThumbWarmer._warm_once does: from the stored path.
    warmed = thumb_cache_path(tmp_settings, Path(img.path))
    warmed.parent.mkdir(parents=True, exist_ok=True)
    warmed.write_bytes(generate_thumb(Path(img.path)))
    before = sorted(p.name for p in warmed.parent.iterdir())

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(f"/images/{img.id}/thumb.png")
            return r

    r = asyncio.run(run())
    assert r.status_code == 200
    assert r.content == warmed.read_bytes()
    assert sorted(p.name for p in warmed.parent.iterdir()) == before, (
        "route wrote a second cache entry — it is not using the warmer's key"
    )


def test_render_with_rejects_out_of_range_params(tmp_path: Path, tmp_settings):
    """Out-of-range slider values on render-with.png are client input errors
    (422), not server faults (500)."""
    tmp_settings.ensure_dirs()
    app, library = _setup(tmp_settings)

    fixture = tmp_settings.photos_dir / "src.jpg"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(fixture, "JPEG")
    library.scan()
    image_id = library.list(limit=1)[0].id

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                f"/images/{image_id}/render-with.png", params={"saturation": 9}
            )
            return r

    r = asyncio.run(run())
    assert r.status_code == 422
