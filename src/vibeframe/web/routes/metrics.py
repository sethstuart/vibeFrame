from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from vibeframe import timing
from vibeframe.web.deps import AppState, get_state

router = APIRouter(tags=["metrics"])


def _avg_ms(snap: dict, name: str) -> float:
    s = snap.get(name)
    if not s or not s.get("count"):
        return 0.0
    # snapshot mean is over the ring window; for a stable "avg" use lifetime
    # totals when present.
    total = s.get("total_seconds", 0.0)
    count = s.get("count", 0)
    return (total / count * 1000.0) if count else 0.0


def _tile_context(state: AppState, snap: dict) -> dict:
    # Average image processing = mean of pipeline.process.miss + .hit weighted.
    miss = snap.get("pipeline.process.miss", {})
    hit = snap.get("pipeline.process.hit", {})
    total_count = miss.get("count", 0) + hit.get("count", 0)
    total_seconds = miss.get("total_seconds", 0.0) + hit.get("total_seconds", 0.0)
    avg_proc_s = (total_seconds / total_count) if total_count else 0.0

    # NFS status — photos_dir reachable + has at least one image; read = avg
    # of pipeline.image.open; write = avg of nfs.write.
    photos_dir = state.settings.photos_dir
    nfs_reachable = photos_dir.is_dir()
    nfs_read_ms = _avg_ms(snap, "pipeline.image.open")
    nfs_write_ms = _avg_ms(snap, "nfs.write")
    return {
        "avg_proc_seconds": avg_proc_s,
        "nfs_reachable": nfs_reachable,
        "nfs_read_ms": nfs_read_ms,
        "nfs_write_ms": nfs_write_ms,
        "image_count": state.library.count(),
    }


def _sorted_rows(snap: dict, sort: str) -> tuple[list[dict], str]:
    rows = [{"name": name, **stats} for name, stats in snap.items()]
    sort_key = sort if rows and sort in rows[0] else "p95_ms"
    rows.sort(key=lambda r: r.get(sort_key, 0), reverse=True)
    return rows, sort_key


@router.get("/metrics.json")
def metrics_json():
    return timing.snapshot()


@router.get("/metrics", response_class=HTMLResponse)
def metrics_html(request: Request, sort: str = "p95_ms", state: AppState = Depends(get_state)):
    snap = timing.snapshot()
    rows, sort_key = _sorted_rows(snap, sort)
    ctx = {"rows": rows, "sort": sort_key, **_tile_context(state, snap)}
    return request.app.state.templates.TemplateResponse(request, "metrics.html", ctx)


@router.get("/metrics/fragment", response_class=HTMLResponse)
def metrics_fragment(request: Request, sort: str = "p95_ms", state: AppState = Depends(get_state)):
    """Live-updating fragment: tiles + table body. Polled every 5s by metrics.html."""
    snap = timing.snapshot()
    rows, sort_key = _sorted_rows(snap, sort)
    ctx = {"rows": rows, "sort": sort_key, **_tile_context(state, snap)}
    return request.app.state.templates.TemplateResponse(request, "_metrics_body.html", ctx)


@router.post("/metrics/clear")
def metrics_clear():
    timing.clear()
    return {"cleared": True}
