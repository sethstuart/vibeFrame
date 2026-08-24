from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from sqlmodel import Session, select

from vibeframe.db import (
    Collection,
    CollectionInUseError,
    CollectionMember,
    Favorite,
    Setting,
    build_engine,
    create_collection,
    delete_collection,
    get_setting,
    list_collections,
    update_collection,
)
from vibeframe.library import ImageLibrary


def _library(tmp_path: Path, count: int = 3) -> tuple[ImageLibrary, object]:
    root = tmp_path / "photos"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        Image.new("RGB", (16, 16), (i * 30, 40, 60)).save(root / f"p{i}.jpg", "JPEG")
    engine = build_engine(tmp_path / "test.db")
    lib = ImageLibrary(root, engine)
    lib.scan()
    return lib, engine


def test_fresh_install_has_only_the_favorites_collection(tmp_path: Path):
    _, engine = _library(tmp_path, count=1)
    cols = list_collections(engine)
    assert [c.name for c in cols] == ["Favorites"]
    assert cols[0].is_default is True


def test_legacy_favorites_migrate_into_the_collection_once(tmp_path: Path):
    """Upgrade path: rows in the old `favorite` table become membership in the
    built-in collection. Crucially it must not re-run — a user who deliberately
    unfavourites everything would otherwise have it all resurrected on the next
    boot."""
    db = tmp_path / "test.db"
    lib, engine = _library(tmp_path, count=3)
    ids = sorted(i.id for i in lib.list(limit=10))

    # Simulate an install that predates collections: legacy rows present, no
    # membership, and the migration marker cleared.
    with Session(engine) as session:
        for image_id in ids[:2]:
            session.add(Favorite(image_id=image_id))
        for member in session.exec(select(CollectionMember)).all():
            session.delete(member)
        for row in session.exec(
            select(Setting).where(Setting.key == "migrated.favorites_to_collection")
        ).all():
            session.delete(row)
        session.commit()
    engine.dispose()

    engine2 = build_engine(db)
    lib2 = ImageLibrary(tmp_path / "photos", engine2)
    assert sorted(lib2.all_ids(favorites_only=True)) == ids[:2]
    assert get_setting(engine2, "migrated.favorites_to_collection") == "1"

    # Unfavourite one, reboot, and it must stay unfavourited.
    lib2.toggle_favorite(ids[0])
    assert sorted(lib2.all_ids(favorites_only=True)) == [ids[1]]
    engine2.dispose()

    engine3 = build_engine(db)
    lib3 = ImageLibrary(tmp_path / "photos", engine3)
    assert sorted(lib3.all_ids(favorites_only=True)) == [ids[1]], "migration re-ran"


def test_star_and_collection_membership_are_the_same_thing(tmp_path: Path):
    """favorites_only is an alias for the default collection, not a parallel
    store — toggling the star has to be visible through the collection API and
    vice versa."""
    lib, engine = _library(tmp_path, count=2)
    img = lib.list(limit=1)[0]
    favorites = list_collections(engine)[0]

    assert lib.toggle_favorite(img.id) is True
    assert lib.collections_for(img.id) == [favorites.id]
    assert lib.count(favorites_only=True) == 1
    assert lib.count(collection_id=favorites.id) == 1

    # Remove it through the collection API; the star must agree.
    lib.set_collection(favorites.id, img.id, False)
    assert lib.is_favorite(img.id) is False
    assert lib.count(favorites_only=True) == 0


def test_an_image_can_belong_to_several_collections(tmp_path: Path):
    lib, engine = _library(tmp_path, count=2)
    img = lib.list(limit=1)[0]
    xmas = create_collection(engine, "Christmas", start_month=12, start_day=1,
                             end_month=12, end_day=25)
    trip = create_collection(engine, "Iceland")

    lib.toggle_favorite(img.id)
    lib.set_collection(xmas.id, img.id, True)
    lib.set_collection(trip.id, img.id, True)

    assert len(lib.collections_for(img.id)) == 3
    assert lib.collection_counts()[xmas.id] == 1
    assert [i.id for i in lib.list(limit=10, collection_id=xmas.id)] == [img.id]


def test_favorites_collection_is_protected(tmp_path: Path):
    _, engine = _library(tmp_path, count=1)
    favorites = list_collections(engine)[0]
    with pytest.raises(CollectionInUseError):
        delete_collection(engine, favorites.id)
    with pytest.raises(CollectionInUseError):
        update_collection(engine, favorites.id, name="Starred")
    # Everything else about it is editable.
    updated = update_collection(engine, favorites.id, weight=2.5)
    assert updated.weight == 2.5


def test_duplicate_names_are_refused(tmp_path: Path):
    _, engine = _library(tmp_path, count=1)
    create_collection(engine, "Halloween")
    with pytest.raises(ValueError):
        create_collection(engine, "Halloween")
    with pytest.raises(ValueError):
        create_collection(engine, "   ")


def test_deleting_a_collection_keeps_the_images(tmp_path: Path):
    """A collection is a label, not a container."""
    lib, engine = _library(tmp_path, count=2)
    img = lib.list(limit=1)[0]
    trip = create_collection(engine, "Iceland")
    lib.set_collection(trip.id, img.id, True)

    assert delete_collection(engine, trip.id) is True
    assert lib.count() == 2
    assert lib.collections_for(img.id) == []
    with Session(engine) as session:
        assert session.exec(select(CollectionMember)).all() == []


def test_deleting_an_image_clears_its_memberships(tmp_path: Path):
    lib, engine = _library(tmp_path, count=2)
    img = lib.list(limit=1)[0]
    trip = create_collection(engine, "Iceland")
    lib.set_collection(trip.id, img.id, True)
    lib.toggle_favorite(img.id)

    lib.remove_path(Path(img.path))

    with Session(engine) as session:
        assert session.exec(select(CollectionMember)).all() == []
        assert session.exec(select(Collection)).all() != [], "collections themselves survive"


def test_bulk_favorite_still_targets_the_default_collection(tmp_path: Path):
    lib, engine = _library(tmp_path, count=3)
    ids = [i.id for i in lib.list(limit=10)]
    favorites = list_collections(engine)[0]

    assert lib.bulk_favorite(ids, True) == 3
    assert lib.collection_counts()[favorites.id] == 3
    # Idempotent: re-adding changes nothing, so the count reported is honest.
    assert lib.bulk_favorite(ids, True) == 0
    assert lib.bulk_favorite(ids, False) == 3
