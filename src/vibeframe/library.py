from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from vibeframe.cache import Cache, file_sha256
from vibeframe.db import (
    Favorite,
    History,
    Image,
    delete_image_by_path,
    get_existing_index,
    image_count,
    upsert_images,
)
from vibeframe.timing import timed

log = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class LibraryImage:
    id: int
    path: Path
    sha256: str


def _is_image(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS


class ImageLibrary:
    def __init__(self, root: Path, engine, recursive: bool = True, cache: Cache | None = None) -> None:
        self.root = root
        # Resolved once: safe_path runs per image on every read and delete, and
        # re-resolving the root each time is a syscall per call for a value that
        # does not change.
        self.root_resolved = root.resolve()
        self.engine = engine
        self.recursive = recursive
        self.cache = cache
        self._lock = threading.RLock()

    def scan(self, prune: bool = True) -> int:
        """Walk the root and upsert all images. Returns the count seen.

        Skips re-hashing files whose (mtime, size) matches the DB — sha256
        over NFS is the dominant cost in a periodic rescan.

        When `prune` is True (default), images that are in the DB but no longer
        on disk are deleted along with their favorites and history. Periodic
        rescans pass prune=False so a transient NFS hiccup can't wipe user
        state. A sanity check also skips prune if the scan found zero images
        but the DB has entries.
        """
        with self._lock, timed("library.scan"):
            with timed("library.scan.walk"):
                paths = self._walk()
            with timed("library.scan.db_index"):
                existing = get_existing_index(self.engine)
            rows = []
            rehashed = 0
            with timed("library.scan.stat_loop"):
                for p in paths:
                    try:
                        stat = p.stat()
                    except FileNotFoundError:
                        continue
                    key = str(p)
                    prior = existing.get(key)
                    if prior and prior[0] == stat.st_mtime and prior[1] == stat.st_size:
                        sha = prior[2]
                    else:
                        with timed("library.scan.hash_one"):
                            sha = file_sha256(p)
                        rehashed += 1
                    rows.append({
                        "path": key,
                        "sha256": sha,
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                    })
            with timed("library.scan.db_upsert"):
                upsert_images(self.engine, rows)
            pruned = False
            if prune:
                if not rows and existing:
                    log.warning(
                        "library scan found 0 images under %s but DB has %d — "
                        "skipping prune to protect favorites/history (NFS hiccup?)",
                        self.root,
                        len(existing),
                    )
                else:
                    with timed("library.scan.prune"):
                        self._prune_missing({str(p) for p in paths})
                    pruned = True
            log.info(
                "library scan complete: %d images under %s (rehashed %d, pruned=%s)",
                len(rows),
                self.root,
                rehashed,
                pruned,
            )
            return len(rows)

    def _walk(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        glob = "**/*" if self.recursive else "*"
        return [p for p in self.root.glob(glob) if _is_image(p) and self._contained(p)]

    def _contained(self, p: Path) -> bool:
        """Keep escaping symlinks out of the library entirely.

        Indexing one produces a row that every read and delete route then
        refuses (see safe_path) — an image that shows a broken tile, renders
        nothing, and cannot be deleted. Rejecting it at ingest is the same
        containment rule applied where it costs one check instead of eight,
        and the next pruning scan clears any row indexed before this existed.

        Only symlinks pay the resolve(); a plain file under the root is already
        contained, and scan() walks every file on the Pi's NFS mount.
        """
        if not p.is_symlink():
            return True
        if self.safe_path(p) is not None:
            return True
        log.warning("skipping %s — symlink resolves outside %s", p, self.root)
        return False

    def _prune_missing(self, present_paths: set[str]) -> None:
        with Session(self.engine) as session:
            stored = session.exec(select(Image)).all()
            for img in stored:
                if img.path not in present_paths:
                    sha = delete_image_by_path(self.engine, img.path)
                    if sha and self.cache:
                        self.cache.invalidate_source(sha)

    def add_path(self, path: Path) -> None:
        with self._lock:
            if not _is_image(path) or not self._contained(path):
                return
            try:
                stat = path.stat()
            except FileNotFoundError:
                return
            upsert_images(
                self.engine,
                [{
                    "path": str(path),
                    "sha256": file_sha256(path),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }],
            )

    def count(self, favorites_only: bool = False) -> int:
        return image_count(self.engine, favorites_only=favorites_only)

    def safe_path(self, raw: str | Path) -> Path | None:
        """Resolve `raw` and return it only if the target stays inside the
        library root. Returns None when a DB row (or symlink) points outside —
        callers treat that as "not found" so nothing outside photos_dir is ever
        read or deleted through a stored record."""
        try:
            resolved = Path(raw).resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(self.root_resolved):
            return None
        return resolved

    def remove_path(self, path: Path) -> None:
        with self._lock:
            sha = delete_image_by_path(self.engine, str(path))
            if sha and self.cache:
                self.cache.invalidate_source(sha)

    def list(
        self,
        limit: int = 200,
        offset: int = 0,
        favorites_only: bool = False,
        query: str | None = None,
        sort: str = "newest",
    ) -> list[Image]:
        with Session(self.engine) as session:
            stmt = select(Image)
            if favorites_only:
                stmt = stmt.join(Favorite, Favorite.image_id == Image.id)
            if query:
                stmt = stmt.where(Image.path.contains(query))  # type: ignore[attr-defined]
            if sort == "oldest":
                stmt = stmt.order_by(Image.added_at.asc())
            elif sort == "name":
                stmt = stmt.order_by(Image.path.asc())
            else:
                stmt = stmt.order_by(Image.added_at.desc())
            stmt = stmt.offset(offset).limit(limit)
            return list(session.exec(stmt))

    def bulk_favorite(self, image_ids: list[int], favorited: bool) -> int:
        if not image_ids:
            return 0
        n = 0
        with Session(self.engine) as session:
            for image_id in image_ids:
                existing = session.get(Favorite, image_id)
                if favorited and not existing:
                    session.add(Favorite(image_id=image_id))
                    n += 1
                elif not favorited and existing:
                    session.delete(existing)
                    n += 1
            session.commit()
        return n

    def bulk_delete(self, image_ids: list[int]) -> int:
        import contextlib
        from pathlib import Path as _P

        n = 0
        for image_id in image_ids:
            img = self.get(image_id)
            if img is None:
                continue
            target = self.safe_path(img.path)
            if target is None:
                log.warning(
                    "bulk_delete: skipping id %s — path resolves outside library root",
                    image_id,
                )
                continue
            with contextlib.suppress(OSError):
                _P(target).unlink(missing_ok=True)
            self.remove_path(_P(img.path))
            n += 1
        return n

    def recent_shown(self, limit: int = 12) -> list[tuple[Image, str]]:
        """Return recently displayed (Image, shown_at_iso) pairs."""
        with Session(self.engine) as session:
            from sqlmodel import select as _select
            rows = session.exec(
                _select(History, Image)
                .join(Image, Image.id == History.image_id)
                .order_by(History.shown_at.desc())
                .limit(limit)
            )
            seen: set[int] = set()
            out: list[tuple[Image, str]] = []
            for hist, img in rows:
                if img.id in seen:
                    continue
                seen.add(img.id or 0)
                out.append((img, hist.shown_at.isoformat()))
            return out

    def get(self, image_id: int) -> Image | None:
        with Session(self.engine) as session:
            return session.get(Image, image_id)

    def get_by_path(self, path: str) -> Image | None:
        with Session(self.engine) as session:
            return session.exec(select(Image).where(Image.path == path)).first()

    def all_ids(self, favorites_only: bool = False) -> list[int]:
        with Session(self.engine) as session:
            if favorites_only:
                stmt = select(Image.id).join(Favorite, Favorite.image_id == Image.id)
            else:
                stmt = select(Image.id)
            return list(session.exec(stmt))

    def recent_ids(self, limit: int) -> list[int]:
        with Session(self.engine) as session:
            stmt = select(Image.id).order_by(Image.added_at.desc()).limit(limit)
            return list(session.exec(stmt))

    def toggle_favorite(self, image_id: int) -> bool:
        with Session(self.engine) as session:
            existing = session.get(Favorite, image_id)
            if existing:
                session.delete(existing)
                session.commit()
                return False
            session.add(Favorite(image_id=image_id))
            session.commit()
            return True

    def is_favorite(self, image_id: int) -> bool:
        with Session(self.engine) as session:
            return session.get(Favorite, image_id) is not None

    def last_shown(self, limit: int = 10) -> Iterable[tuple[int, str]]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(History, Image).join(Image, Image.id == History.image_id)
                .order_by(History.shown_at.desc()).limit(limit)
            )
            for _hist, img in rows:
                yield (img.id, img.path)
