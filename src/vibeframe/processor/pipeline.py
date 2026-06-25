from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter

import numpy as np
from PIL import Image, ImageOps

from vibeframe.cache import Cache, CacheKey, params_hash
from vibeframe.config import Settings
from vibeframe.processor import crop, dither, palette, tonemap
from vibeframe.timing import record, timed

try:  # HEIC/HEIF support is optional but desirable.
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - environment-dependent
    pass


DISPLAY_W = 800
DISPLAY_H = 480


@dataclass(frozen=True)
class ProcessedImage:
    path: Path
    image: Image.Image  # mode 'P' with palette set; matches display dims pre-rotation
    source_sha256: str


def _target_size(orientation: int) -> tuple[int, int]:
    if orientation in (90, 270):
        return (DISPLAY_H, DISPLAY_W)
    return (DISPLAY_W, DISPLAY_H)


def _build_p_image(indices: np.ndarray, pal: tuple[palette.RGB, ...]) -> Image.Image:
    h, w = indices.shape
    img = Image.frombytes("P", (w, h), indices.tobytes())
    flat: list[int] = [c for rgb in pal for c in rgb]
    flat += [0] * (768 - len(flat))
    img.putpalette(flat)
    return img


def _pipeline_params(settings: Settings, target_w: int, target_h: int) -> dict:
    return {
        "version": 1,
        "w": target_w,
        "h": target_h,
        "dither": settings.dither,
        "crop": settings.crop_mode,
        "sat": round(settings.saturation, 3),
        "con": round(settings.contrast, 3),
    }


def cached_png_bytes(
    path: Path, settings: Settings, cache: Cache, sha256: str | None = None
) -> bytes | None:
    """Return raw cached PNG bytes if the pipeline cache already has them, else None.

    Used by the /preview.png route to avoid the PIL decode+re-encode round-trip
    on cache hits — the cached file is already a valid PNG matching exactly what
    the panel would render.
    """
    target_w, target_h = _target_size(settings.orientation)
    params = _pipeline_params(settings, target_w, target_h)
    if sha256 is not None:
        source_key = sha256
    else:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        source_key = f"stat-{stat.st_mtime_ns}-{stat.st_size}"
    key = CacheKey(source_sha256=source_key, params_hash=params_hash(params))
    p = cache.get(key)
    if p is None or not p.is_file():
        return None
    return p.read_bytes()


def process(
    path: Path,
    settings: Settings,
    cache: Cache | None = None,
    sha256: str | None = None,
) -> ProcessedImage:
    """Run the full pipeline for one source image and return a display-ready PIL image.

    If `sha256` is supplied (e.g. by the library, which already stores it), we use it
    directly. Otherwise we fall back to (path, mtime, size) so the cache lookup never
    requires reading the source file — only a missed lookup pays the decode cost.
    """
    start = perf_counter()
    target_w, target_h = _target_size(settings.orientation)
    params = _pipeline_params(settings, target_w, target_h)

    if sha256 is not None:
        source_key = sha256
    else:
        stat = path.stat()
        source_key = f"stat-{stat.st_mtime_ns}-{stat.st_size}"
    key = CacheKey(source_sha256=source_key, params_hash=params_hash(params))

    if cache is not None:
        with timed("pipeline.cache.lookup"):
            cached = cache.get(key)
        if cached is not None:
            result = ProcessedImage(
                path=path, image=Image.open(cached), source_sha256=source_key
            )
            record("pipeline.process.hit", perf_counter() - start)
            return result

    with timed("pipeline.image.open"):
        with Image.open(path) as img:
            img.load()
        with timed("pipeline.exif.transpose"):
            oriented = ImageOps.exif_transpose(img).convert("RGB")

    with timed(f"pipeline.crop.{settings.crop_mode}"):
        cropped = crop.crop_to(oriented, target_w, target_h, settings.crop_mode)
    with timed("pipeline.tonemap"):
        toned = tonemap.apply(cropped, settings.saturation, settings.contrast)

    with timed("pipeline.ndarray"):
        src_array = np.array(toned, dtype=np.uint8)
    with timed(f"pipeline.dither.{settings.dither}"):
        indices = dither.dither(src_array, settings.dither, palette.SPECTRA6)
    with timed("pipeline.palette.build_p"):
        out = _build_p_image(indices, palette.SPECTRA6)

    if cache is not None:
        with timed("pipeline.cache.write"):
            buf = BytesIO()
            out.save(buf, format="PNG")
            cache.put_bytes(key, buf.getvalue())

    record("pipeline.process.miss", perf_counter() - start)
    return ProcessedImage(path=path, image=out, source_sha256=source_key)
