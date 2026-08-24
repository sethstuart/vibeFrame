from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from vibeframe.config import Settings, collection_mode_id, is_valid_selection_mode
from vibeframe.db import (
    Collection,
    build_engine,
    create_collection,
    effective_weight,
    in_season,
    list_collections,
    record_show,
)
from vibeframe.library import ImageLibrary
from vibeframe.scheduler import _pick_least_shown, _pick_next, _pick_weighted


def _library(tmp_path: Path, count: int = 4):
    root = tmp_path / "photos"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        Image.new("RGB", (16, 16), (i * 20, 30, 40)).save(root / f"p{i}.jpg", "JPEG")
    engine = build_engine(tmp_path / "test.db")
    lib = ImageLibrary(root, engine)
    lib.scan()
    return lib, engine


def _season(**kw) -> Collection:
    return Collection(name="x", **kw)


# ── seasons ──

def test_season_window_is_annual_and_inclusive():
    xmas = _season(start_month=12, start_day=1, end_month=12, end_day=25)
    assert in_season(xmas, date(2026, 12, 1)) is True     # first day
    assert in_season(xmas, date(2026, 12, 25)) is True    # last day
    assert in_season(xmas, date(2026, 12, 26)) is False
    assert in_season(xmas, date(2026, 11, 30)) is False
    # Same window, a different year — the point of month/day only.
    assert in_season(xmas, date(2031, 12, 10)) is True


def test_season_window_can_wrap_the_new_year():
    """Dec 15 - Jan 5 has to mean "after the start OR before the end", the same
    way quiet hours wrap midnight."""
    winter = _season(start_month=12, start_day=15, end_month=1, end_day=5)
    assert in_season(winter, date(2026, 12, 20)) is True
    assert in_season(winter, date(2027, 1, 3)) is True
    assert in_season(winter, date(2026, 12, 14)) is False
    assert in_season(winter, date(2027, 1, 6)) is False
    assert in_season(winter, date(2027, 7, 1)) is False


def test_no_season_is_never_in_season():
    assert in_season(_season(), date(2026, 6, 1)) is False
    # Half a window can't happen through the UI, but must not throw here.
    assert in_season(_season(start_month=6, start_day=1), date(2026, 6, 2)) is False


def test_effective_weight_applies_the_boost_only_in_season():
    xmas = _season(weight=2.0, boost=3.0,
                   start_month=12, start_day=1, end_month=12, end_day=25)
    assert effective_weight(xmas, date(2026, 12, 10)) == 6.0
    assert effective_weight(xmas, date(2026, 6, 10)) == 2.0
    # Weight 0 excludes the collection outright, boost or not.
    off = _season(weight=0.0, boost=5.0,
                  start_month=12, start_day=1, end_month=12, end_day=25)
    assert effective_weight(off, date(2026, 12, 10)) == 0.0


# ── mode parsing ──

def test_collection_mode_parsing():
    assert collection_mode_id("collection:7") == 7
    assert collection_mode_id("shuffle") is None
    assert collection_mode_id("collection:nope") is None
    assert is_valid_selection_mode("collection:7") is True
    assert is_valid_selection_mode("weighted") is True
    assert is_valid_selection_mode("nonsense") is False


def test_settings_rejects_an_unknown_selection_mode():
    """SelectionMode stopped being a Literal so "collection:<id>" would fit,
    which means a typo is no longer caught by the type alone."""
    with pytest.raises(ValidationError):
        Settings(selection_mode="shufle")
    assert Settings(selection_mode="collection:3").selection_mode == "collection:3"


# ── picking ──

def test_collection_mode_picks_only_from_that_collection(tmp_path: Path):
    lib, engine = _library(tmp_path, count=4)
    ids = sorted(i.id for i in lib.list(limit=10))
    trip = create_collection(engine, "Iceland")
    lib.set_collection(trip.id, ids[0], True)
    lib.set_collection(trip.id, ids[1], True)

    picks = {
        _pick_next(lib, f"collection:{trip.id}", None, engine=engine, today=date(2026, 6, 1))
        for _ in range(40)
    }
    assert picks <= {ids[0], ids[1]}
    assert len(picks) == 2, "both members should turn up over 40 draws"


def test_empty_or_missing_collection_falls_back_to_the_library(tmp_path: Path):
    """Otherwise the frame freezes on whatever is already on the panel."""
    lib, engine = _library(tmp_path, count=3)
    empty = create_collection(engine, "Empty")

    assert _pick_next(lib, f"collection:{empty.id}", None, engine=engine) is not None
    assert _pick_next(lib, "collection:99999", None, engine=engine) is not None


def test_least_shown_prefers_the_never_shown_then_the_oldest(tmp_path: Path):
    lib, engine = _library(tmp_path, count=3)
    ids = sorted(i.id for i in lib.list(limit=10))

    # Show two; the third has never been shown and must win outright.
    record_show(engine, ids[0])
    record_show(engine, ids[1])
    assert _pick_least_shown(engine) == ids[2]

    # Once everything has been shown, the earliest one is next.
    record_show(engine, ids[2])
    assert _pick_least_shown(engine) == ids[0]
    record_show(engine, ids[0])
    assert _pick_least_shown(engine) == ids[1]


def test_least_shown_eventually_covers_the_whole_library(tmp_path: Path):
    """The reason this mode exists: no image is starved."""
    lib, engine = _library(tmp_path, count=5)
    all_ids = {i.id for i in lib.list(limit=10)}

    seen = set()
    for _ in range(5):
        image_id = _pick_least_shown(engine)
        seen.add(image_id)
        record_show(engine, image_id)
    assert seen == all_ids


def test_weighted_favours_an_in_season_collection(tmp_path: Path):
    """A Christmas collection in December should dominate without excluding
    everything else — that is the whole point of boost over a hard filter."""
    lib, engine = _library(tmp_path, count=4)
    ids = sorted(i.id for i in lib.list(limit=10))
    xmas = create_collection(
        engine, "Christmas", weight=5.0, boost=10.0,
        start_month=12, start_day=1, end_month=12, end_day=25,
    )
    lib.set_collection(xmas.id, ids[0], True)

    random.seed(1234)
    december = [_pick_weighted(lib, engine, date(2026, 12, 10)) for _ in range(400)]
    june = [_pick_weighted(lib, engine, date(2026, 6, 10)) for _ in range(400)]

    in_dec = december.count(ids[0]) / len(december)
    in_jun = june.count(ids[0]) / len(june)
    # Dec: baseline pool weight 1 vs collection 50 -> ~98% of draws.
    assert in_dec > 0.8, in_dec
    # Jun: baseline 1 vs collection 5 -> far lower, but never zero.
    assert 0.1 < in_jun < in_dec, in_jun
    # Other images still appear in December.
    assert set(december) - {ids[0]}


def test_weighted_degrades_to_shuffle_with_no_collected_images(tmp_path: Path):
    """A fresh install has an empty Favorites; weighted must not return None."""
    lib, engine = _library(tmp_path, count=3)
    assert list_collections(engine)[0].is_default is True
    picks = {_pick_weighted(lib, engine, date(2026, 6, 1)) for _ in range(30)}
    assert None not in picks
    assert len(picks) > 1
