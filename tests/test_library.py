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


def test_rescan_skips_rehash_when_stat_unchanged(tmp_path: Path, monkeypatch):
    """Subsequent scans must NOT re-hash files whose mtime+size are unchanged."""
    from vibeframe import library as library_mod

    root = tmp_path / "photos"
    root.mkdir()
    _make_jpeg(root / "a.jpg")
    _make_jpeg(root / "b.jpg")
    engine = build_engine(tmp_path / "test.db")
    lib = ImageLibrary(root, engine, recursive=False)
    lib.scan()

    calls = {"n": 0}
    original = library_mod.file_sha256

    def counting(p):
        calls["n"] += 1
        return original(p)

    monkeypatch.setattr(library_mod, "file_sha256", counting)
    lib.scan()
    assert calls["n"] == 0, "unchanged files should not be re-hashed"


def test_pipeline_accepts_precomputed_sha(tmp_path: Path):
    """process() must not call file_sha256 when sha256 is supplied."""
    from vibeframe.cache import Cache
    from vibeframe.config import Settings
    from vibeframe.processor import pipeline

    src = tmp_path / "x.jpg"
    _make_jpeg(src)
    settings = Settings(
        photos_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        driver="mock",
    )
    settings.ensure_dirs()
    cache = Cache(settings.cache_dir, max_bytes=10_000_000)

    p1 = pipeline.process(src, settings, cache, sha256="deadbeef")
    assert p1.source_sha256 == "deadbeef"
    p2 = pipeline.process(src, settings, cache, sha256="deadbeef")
    assert p2.source_sha256 == "deadbeef"


def test_favorite_survives_periodic_rescan_with_missing_files(tmp_path: Path):
    """Periodic rescan (prune=False) must not delete favorites even if the
    files temporarily disappear (NFS hiccup)."""
    root = tmp_path / "photos"
    root.mkdir()
    img_path = root / "keep.jpg"
    _make_jpeg(img_path)
    engine = build_engine(tmp_path / "test.db")
    lib = ImageLibrary(root, engine, recursive=False)
    lib.scan()
    img = lib.list(limit=1)[0]
    assert lib.toggle_favorite(img.id) is True

    # Simulate NFS dropping every file off the share.
    img_path.unlink()

    # Periodic rescan must NOT prune.
    lib.scan(prune=False)
    assert lib.is_favorite(img.id), "favorite must survive periodic rescan"

    # Explicit scan with prune AND empty result also leaves things alone
    # (sanity guard for total NFS failure).
    lib.scan(prune=True)
    assert lib.is_favorite(img.id), "favorite must survive empty-result prune guard"


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
