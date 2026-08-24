from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlmodel import Field, Session, SQLModel, create_engine, select

log = logging.getLogger(__name__)


class Image(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    sha256: str = Field(index=True)
    width: int | None = None
    height: int | None = None
    mtime: float
    size: int | None = None
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Favorite(SQLModel, table=True):
    """Superseded by the built-in Favorites Collection.

    Nothing reads or writes this any more — `_seed_collections` copies it into
    `collectionmember` once and everything goes through collections after that.
    It is left in place for one release as a rollback path for that migration;
    delete the model and DROP the table once the collections work has run on
    real data for a while. Do not start reading it again: Favorites has exactly
    one source of truth now, and it is collection membership.
    """

    image_id: int = Field(primary_key=True, foreign_key="image.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Collection(SQLModel, table=True):
    """A named set of images. Favorites is one of these (`is_default`), not a
    separate concept — that is what lets `favorites_only` stay a thin alias for
    "member of the default collection" instead of a second source of truth.

    The season fields describe a *recurring annual* window: a Christmas
    collection is Dec 1 - Dec 25 every year, not one specific December. While
    the window contains today, weighted selection multiplies `weight` by
    `boost`; outside it, plain `weight` applies. All four month/day fields are
    null together, meaning "no season, always plain weight".
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    # The built-in Favorites collection. Cannot be deleted or renamed: the nav,
    # the star button, and `favorites_only` all resolve through it.
    is_default: bool = Field(default=False, index=True)
    # Relative likelihood under weighted selection. 0 excludes the collection.
    weight: float = 1.0
    # Multiplier applied to `weight` while the season window is open.
    boost: float = 3.0
    start_month: int | None = None
    start_day: int | None = None
    end_month: int | None = None
    end_day: int | None = None
    sort_order: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CollectionMember(SQLModel, table=True):
    """Many-to-many: one image can sit in any number of collections."""

    collection_id: int = Field(primary_key=True, foreign_key="collection.id")
    image_id: int = Field(primary_key=True, foreign_key="image.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class History(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    image_id: int = Field(index=True, foreign_key="image.id")
    shown_at: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


def build_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        # The engine is shared across the event loop, the scheduler executor,
        # the thumb-warmer, and the watcher threads. WAL lets readers proceed
        # during a write; busy_timeout makes a concurrent writer wait for the
        # lock instead of immediately raising 'database is locked'.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    SQLModel.metadata.create_all(engine)
    _apply_migrations(engine)
    _seed_collections(engine)
    return engine


def _apply_migrations(engine) -> None:
    """Lightweight schema migrations for SQLite. SQLModel only creates new
    tables, never alters existing ones, so columns added to models need an
    explicit ADD COLUMN here for upgraded installs."""
    inspector = inspect(engine)
    if "image" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("image")}
        if "size" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE image ADD COLUMN size INTEGER"))


DEFAULT_COLLECTION_NAME = "Favorites"
# Idempotency marker for the one-time favorite -> collectionmember copy. Without
# it, a user who deliberately empties Favorites would have every old favourite
# resurrected on the next boot.
_FAV_MIGRATION_KEY = "migrated.favorites_to_collection"


def _seed_collections(engine) -> None:
    """Guarantee the built-in Favorites collection exists, and fold the legacy
    `favorite` table into it exactly once."""
    with Session(engine) as session:
        default = session.exec(select(Collection).where(Collection.is_default)).first()
        if default is None:
            default = Collection(name=DEFAULT_COLLECTION_NAME, is_default=True)
            session.add(default)
            session.commit()
            session.refresh(default)
        default_id = default.id

    if get_setting(engine, _FAV_MIGRATION_KEY) or default_id is None:
        return

    copied = 0
    if "favorite" in inspect(engine).get_table_names():
        with Session(engine) as session:
            already = set(
                session.exec(
                    select(CollectionMember.image_id).where(
                        CollectionMember.collection_id == default_id
                    )
                ).all()
            )
            for fav in session.exec(select(Favorite)).all():
                if fav.image_id in already:
                    continue
                session.add(
                    CollectionMember(
                        collection_id=default_id,
                        image_id=fav.image_id,
                        created_at=fav.created_at,
                    )
                )
                copied += 1
            session.commit()
    if copied:
        log.info("migrated %d favorite(s) into the %s collection", copied, DEFAULT_COLLECTION_NAME)
    set_setting(engine, _FAV_MIGRATION_KEY, "1")


def default_collection_id(engine) -> int | None:
    with Session(engine) as session:
        row = session.exec(select(Collection).where(Collection.is_default)).first()
        return row.id if row else None


def _resolve_collection(engine, favorites_only: bool, collection_id: int | None) -> int | None:
    """`favorites_only=True` means "the default collection"; an explicit
    collection_id wins over it."""
    if collection_id is not None:
        return collection_id
    if favorites_only:
        return default_collection_id(engine)
    return None


def upsert_images(engine, rows: Iterable[dict]) -> None:
    with Session(engine) as session:
        for row in rows:
            existing = session.exec(select(Image).where(Image.path == row["path"])).first()
            if existing:
                existing.sha256 = row["sha256"]
                existing.width = row.get("width")
                existing.height = row.get("height")
                existing.mtime = row["mtime"]
                existing.size = row.get("size")
                session.add(existing)
            else:
                session.add(Image(**row))
        session.commit()


def get_existing_index(engine) -> dict[str, tuple[float, int | None, str]]:
    """Return {path: (mtime, size, sha256)} for every indexed image. Used by
    library.scan() to skip re-hashing files whose stat matches."""
    with Session(engine) as session:
        rows = session.exec(select(Image.path, Image.mtime, Image.size, Image.sha256)).all()
    return {path: (mtime, size, sha) for (path, mtime, size, sha) in rows}


def image_count(engine, favorites_only: bool = False, collection_id: int | None = None) -> int:
    from sqlalchemy import func

    cid = _resolve_collection(engine, favorites_only, collection_id)
    with Session(engine) as session:
        stmt = select(func.count(Image.id))
        if cid is not None:
            stmt = stmt.join(CollectionMember, CollectionMember.image_id == Image.id).where(
                CollectionMember.collection_id == cid
            )
        return int(session.exec(stmt).one())


def delete_image_by_path(engine, path: str) -> str | None:
    with Session(engine) as session:
        existing = session.exec(select(Image).where(Image.path == path)).first()
        if existing is None:
            return None
        sha = existing.sha256
        if existing.id is not None:
            for fav in session.exec(select(Favorite).where(Favorite.image_id == existing.id)):
                session.delete(fav)
            for member in session.exec(
                select(CollectionMember).where(CollectionMember.image_id == existing.id)
            ):
                session.delete(member)
            for hist in session.exec(select(History).where(History.image_id == existing.id)):
                session.delete(hist)
        session.delete(existing)
        session.commit()
        return sha


def in_season(collection: Collection, today: date) -> bool:
    """Is today inside this collection's recurring annual window?

    Month/day only, so it repeats every year. Handles a window that wraps the
    new year (Dec 15 - Jan 5) the same way quiet hours handle wrapping
    midnight: inside means "after the start OR before the end" rather than
    "between the two".
    """
    if collection.start_month is None or collection.end_month is None:
        return False
    if collection.start_day is None or collection.end_day is None:
        return False
    start = (collection.start_month, collection.start_day)
    end = (collection.end_month, collection.end_day)
    now = (today.month, today.day)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def effective_weight(collection: Collection, today: date) -> float:
    """Weight to use for weighted selection right now: the base weight, times
    the boost while the season window is open. This is what makes a Christmas
    collection surface in December without needing a mode of its own."""
    weight = max(0.0, collection.weight)
    if weight == 0.0:
        return 0.0
    return weight * max(0.0, collection.boost) if in_season(collection, today) else weight


class CollectionInUseError(Exception):
    """Raised when an operation is refused because it targets the built-in
    Favorites collection."""


def list_collections(engine) -> list[Collection]:
    """Default collection first, then by sort_order, then name — the order the
    Collections tab and the per-image picker both render in."""
    with Session(engine) as session:
        rows = session.exec(select(Collection)).all()
    return sorted(rows, key=lambda c: (not c.is_default, c.sort_order, c.name.lower()))


def get_collection(engine, collection_id: int) -> Collection | None:
    with Session(engine) as session:
        return session.get(Collection, collection_id)


def create_collection(engine, name: str, **fields) -> Collection:
    """Create a collection. Raises ValueError on a blank or duplicate name —
    `name` is unique so the picker never shows two identical rows."""
    name = name.strip()
    if not name:
        raise ValueError("collection name cannot be empty")
    with Session(engine) as session:
        clash = session.exec(select(Collection).where(Collection.name == name)).first()
        if clash is not None:
            raise ValueError(f"a collection named {name!r} already exists")
        row = Collection(name=name, **fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def update_collection(engine, collection_id: int, **fields) -> Collection | None:
    """Patch the given fields. The default collection's name is fixed; every
    other field on it (weight, season) is editable like any other."""
    with Session(engine) as session:
        row = session.get(Collection, collection_id)
        if row is None:
            return None
        if "name" in fields:
            name = (fields.pop("name") or "").strip()
            if row.is_default and name != row.name:
                raise CollectionInUseError("the Favorites collection cannot be renamed")
            if not name:
                raise ValueError("collection name cannot be empty")
            clash = session.exec(select(Collection).where(Collection.name == name)).first()
            if clash is not None and clash.id != collection_id:
                raise ValueError(f"a collection named {name!r} already exists")
            row.name = name
        for key, value in fields.items():
            setattr(row, key, value)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def delete_collection(engine, collection_id: int) -> bool:
    """Drop a collection and its membership rows. The images themselves are
    untouched — a collection is a label, not a container."""
    with Session(engine) as session:
        row = session.get(Collection, collection_id)
        if row is None:
            return False
        if row.is_default:
            raise CollectionInUseError("the Favorites collection cannot be deleted")
        for member in session.exec(
            select(CollectionMember).where(CollectionMember.collection_id == collection_id)
        ):
            session.delete(member)
        session.delete(row)
        session.commit()
        return True


def set_membership(engine, collection_id: int, image_id: int, member: bool) -> bool:
    """Add or remove one image from one collection. Returns the resulting
    membership state, so a caller can treat this as an idempotent set."""
    with Session(engine) as session:
        existing = session.get(CollectionMember, (collection_id, image_id))
        if member and existing is None:
            session.add(CollectionMember(collection_id=collection_id, image_id=image_id))
            session.commit()
        elif not member and existing is not None:
            session.delete(existing)
            session.commit()
        return member


def collection_ids_for_image(engine, image_id: int) -> list[int]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(CollectionMember.collection_id).where(
                    CollectionMember.image_id == image_id
                )
            ).all()
        )


def collection_counts(engine) -> dict[int, int]:
    """{collection_id: image count} for every collection, in one query, so the
    Collections tab does not fan out to one COUNT per row."""
    from sqlalchemy import func

    with Session(engine) as session:
        rows = session.exec(
            select(CollectionMember.collection_id, func.count(CollectionMember.image_id)).group_by(
                CollectionMember.collection_id
            )
        ).all()
    return {cid: int(n) for cid, n in rows}


def least_recently_shown(engine, limit: int = 500) -> list[tuple[int, datetime | None]]:
    """(image_id, last_shown) ordered oldest-first, never-shown first of all.

    SQLite sorts NULL below every other value, so a plain ASC already puts the
    never-shown at the front — no NULLS FIRST needed (it is only supported from
    3.30 and this has to run on whatever the Pi's base image ships).
    """
    from sqlalchemy import func

    with Session(engine) as session:
        stmt = (
            select(Image.id, func.max(History.shown_at).label("last_shown"))
            .outerjoin(History, History.image_id == Image.id)
            .group_by(Image.id)
            .order_by(func.max(History.shown_at).asc())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in session.exec(stmt).all()]


def record_show(engine, image_id: int) -> None:
    with Session(engine) as session:
        session.add(History(image_id=image_id))
        session.commit()


def get_setting(engine, key: str) -> str | None:
    with Session(engine) as session:
        row = session.exec(select(Setting).where(Setting.key == key)).first()
        return row.value if row else None


def set_setting(engine, key: str, value: str) -> None:
    with Session(engine) as session:
        row = session.exec(select(Setting).where(Setting.key == key)).first()
        if row:
            row.value = value
            session.add(row)
        else:
            session.add(Setting(key=key, value=value))
        session.commit()
