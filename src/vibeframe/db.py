from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select


class Image(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    sha256: str = Field(index=True)
    width: int | None = None
    height: int | None = None
    mtime: float
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Favorite(SQLModel, table=True):
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
    SQLModel.metadata.create_all(engine)
    return engine


def upsert_images(engine, rows: Iterable[dict]) -> None:
    with Session(engine) as session:
        for row in rows:
            existing = session.exec(select(Image).where(Image.path == row["path"])).first()
            if existing:
                existing.sha256 = row["sha256"]
                existing.width = row.get("width")
                existing.height = row.get("height")
                existing.mtime = row["mtime"]
                session.add(existing)
            else:
                session.add(Image(**row))
        session.commit()


def delete_image_by_path(engine, path: str) -> str | None:
    with Session(engine) as session:
        existing = session.exec(select(Image).where(Image.path == path)).first()
        if existing is None:
            return None
        sha = existing.sha256
        if existing.id is not None:
            for fav in session.exec(select(Favorite).where(Favorite.image_id == existing.id)):
                session.delete(fav)
            for hist in session.exec(select(History).where(History.image_id == existing.id)):
                session.delete(hist)
        session.delete(existing)
        session.commit()
        return sha


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
