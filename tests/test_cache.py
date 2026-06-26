from __future__ import annotations

from vibeframe.cache import Cache, CacheKey


def _put(cache: Cache, source: str, params: str = "p", data: bytes = b"x" * 100):
    return cache.put_bytes(CacheKey(source_sha256=source, params_hash=params), data)


def test_usage_counts_all_png(tmp_path):
    cache = Cache(tmp_path, max_bytes=10 * 1024 * 1024)
    _put(cache, "aabb", data=b"x" * 100)
    _put(cache, "ccdd", data=b"y" * 200)
    u = cache.usage()
    assert u["count"] == 2
    assert u["bytes"] == 300


def test_clear_keeps_both_layers_of_listed_source(tmp_path):
    cache = Cache(tmp_path, max_bytes=10 * 1024 * 1024)
    # Dithered + prepared entries for two images.
    _put(cache, "aabbcc", params="d1", data=b"x" * 100)
    _put(cache, "prepared-aabbcc", params="pr", data=b"x" * 100)
    _put(cache, "ddeeff", params="d1", data=b"y" * 100)
    _put(cache, "prepared-ddeeff", params="pr", data=b"y" * 100)
    assert cache.usage()["count"] == 4

    result = cache.clear(keep_sources={"aabbcc"})
    assert result["removed"] == 2
    assert result["kept"] == 2
    # Both layers of the kept image survive; the other image is fully gone.
    names = sorted(p.name for p in tmp_path.rglob("*.png"))
    assert all("aabbcc" in n for n in names)
    assert cache.usage()["count"] == 2


def test_clear_all_when_no_keep(tmp_path):
    cache = Cache(tmp_path, max_bytes=10 * 1024 * 1024)
    _put(cache, "aabb", data=b"x" * 100)
    _put(cache, "prepared-aabb", data=b"x" * 100)
    result = cache.clear()
    assert result["removed"] == 2
    assert result["kept"] == 0
    assert cache.usage()["count"] == 0


def test_keep_prefix_does_not_match_similar_sha(tmp_path):
    """The trailing '-' in the keep prefix must prevent 'aabbcc' from also
    sparing 'aabbccff'."""
    cache = Cache(tmp_path, max_bytes=10 * 1024 * 1024)
    _put(cache, "aabbcc", params="d1")
    _put(cache, "aabbccff", params="d1")
    cache.clear(keep_sources={"aabbcc"})
    names = [p.name for p in tmp_path.rglob("*.png")]
    assert names == ["aabbcc-d1.png"]
