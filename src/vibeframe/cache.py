from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CacheKey:
    source_sha256: str
    params_hash: str

    def filename(self) -> str:
        return f"{self.source_sha256}-{self.params_hash}.png"

    def relpath(self) -> Path:
        return Path(self.source_sha256[:2]) / self.filename()


class Cache:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: CacheKey) -> Path:
        return self.root / key.relpath()

    def has(self, key: CacheKey) -> bool:
        return self.path_for(key).is_file()

    def get(self, key: CacheKey) -> Path | None:
        p = self.path_for(key)
        if p.is_file():
            os.utime(p, None)
            return p
        return None

    def put_bytes(self, key: CacheKey, data: bytes) -> Path:
        p = self.path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.evict_if_needed()
        return p

    def evict_if_needed(self) -> None:
        entries = [(p.stat().st_atime, p.stat().st_size, p) for p in self.root.rglob("*.png")]
        total = sum(size for _, size, _ in entries)
        if total <= self.max_bytes:
            return
        entries.sort(key=lambda e: e[0])
        for _, size, p in entries:
            if total <= self.max_bytes:
                break
            try:
                p.unlink()
                total -= size
            except FileNotFoundError:
                pass

    def invalidate_source(self, source_sha256: str) -> int:
        bucket = self.root / source_sha256[:2]
        if not bucket.is_dir():
            return 0
        removed = 0
        for p in bucket.glob(f"{source_sha256}-*.png"):
            try:
                p.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def params_hash(params: dict) -> str:
    canon = "|".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
