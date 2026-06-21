from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from vibeframe import timing
from vibeframe.web.deps import AppState, get_state

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def metrics_json():
    return timing.snapshot()


@router.get("/metrics.html", response_class=HTMLResponse)
def metrics_html(request: Request, sort: str = "p95_ms", state: AppState = Depends(get_state)):
    snap = timing.snapshot()
    rows = [{"name": name, **stats} for name, stats in snap.items()]
    sort_key = sort if rows and sort in rows[0] else "p95_ms"
    rows.sort(key=lambda r: r.get(sort_key, 0), reverse=True)
    return request.app.state.templates.TemplateResponse(
        request,
        "metrics.html",
        {"rows": rows, "sort": sort_key},
    )


@router.post("/metrics/clear")
def metrics_clear():
    timing.clear()
    return {"cleared": True}
