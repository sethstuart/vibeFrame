from __future__ import annotations

from pathlib import Path

from PIL import Image

from vibeframe.cache import Cache, CacheKey
from vibeframe.db import build_engine
from vibeframe.library import ImageLibrary


def _make_jpeg(p: Path, color=(20, 80, 200)) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(p, "JPEG")


def test_scan_indexes_images(tmp_path: Path):
    root = tmp_path / "photos"
    root.mkdir()
    _make_jpeg(root / "a.jpg")
    _make_jpeg(root / "sub" / "b.jpg")
    engine = build_engine(tmp_path / "test.db")
    lib = ImageLibrary(root, engine, recursive=True)
    assert lib.scan() == 2
    paths = {Path(i.path).name for i in lib.list(limit=10)}
    assert paths == {"a.jpg", "b.jpg"}


def test_remove_path_invalidates_cache(tmp_path: Path):
    root = tmp_path / "photos"
    root.mkdir()
    img_path = root / "x.jpg"
    _make_jpeg(img_path)
    engine = build_engine(tmp_path / "test.db")
    cache = Cache(tmp_path / "cache", max_bytes=10_000_000)
    lib = ImageLibrary(root, engine, recursive=False, cache=cache)
    lib.scan()
    img = lib.list(limit=1)[0]
    cache.put_bytes(CacheKey(img.sha256, "abc123"), b"x" * 10)
    assert cache.has(CacheKey(img.sha256, "abc123"))
    lib.remove_path(Path(img.path))
    assert not cache.has(CacheKey(img.sha256, "abc123"))


def test_toggle_favorite_round_trip(tmp_path: Path):
    root = tmp_path / "photos"
    root.mkdir()
    _make_jpeg(root / "f.jpg")
    engine = build_engine(tmp_path / "test.db")
    lib = ImageLibrary(root, engine, recursive=False)
    lib.scan()
    img = lib.list(limit=1)[0]
    assert not lib.is_favorite(img.id)
    assert lib.toggle_favorite(img.id) is True
    assert lib.is_favorite(img.id)
    assert lib.toggle_favorite(img.id) is False
