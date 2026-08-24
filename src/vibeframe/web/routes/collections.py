from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from vibeframe.db import (
    CollectionInUseError,
    collection_counts,
    create_collection,
    delete_collection,
    get_collection,
    list_collections,
    set_membership,
    update_collection,
)
from vibeframe.web.deps import AppState, get_state, require_token

router = APIRouter(prefix="/collections", tags=["collections"])

log = logging.getLogger("vibeframe")

MONTHS = [
    (1, "Jan"), (2, "Feb"), (3, "Mar"), (4, "Apr"), (5, "May"), (6, "Jun"),
    (7, "Jul"), (8, "Aug"), (9, "Sep"), (10, "Oct"), (11, "Nov"), (12, "Dec"),
]
_DAYS_IN_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                  7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _opt_int(raw: str | None) -> int | None:
    """Form fields come back as '' when the user leaves them blank, which is
    how "no season" is expressed."""
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"expected a number, got {raw!r}") from e


def _season(
    start_month: str | None, start_day: str | None, end_month: str | None, end_day: str | None
) -> dict:
    """Validate a recurring-annual window into model fields.

    Either the whole window is set or none of it is — a half-filled season
    would silently never match, which is worse than refusing to save it. Feb 29
    is allowed: the window is month/day only, so it is meaningful in leap years
    and simply never matches otherwise.
    """
    sm, sd = _opt_int(start_month), _opt_int(start_day)
    em, ed = _opt_int(end_month), _opt_int(end_day)
    parts = [sm, sd, em, ed]
    if all(p is None for p in parts):
        return {"start_month": None, "start_day": None, "end_month": None, "end_day": None}
    if any(p is None for p in parts):
        raise HTTPException(
            status_code=422,
            detail="a season needs both a start and an end date, or leave all four blank",
        )
    for month, day in ((sm, sd), (em, ed)):
        if not 1 <= month <= 12:
            raise HTTPException(status_code=422, detail=f"month must be 1-12, got {month}")
        if not 1 <= day <= _DAYS_IN_MONTH[month]:
            raise HTTPException(
                status_code=422,
                detail=f"day must be 1-{_DAYS_IN_MONTH[month]} for that month, got {day}",
            )
    return {"start_month": sm, "start_day": sd, "end_month": em, "end_day": ed}


def _positive(raw: str, label: str) -> float:
    try:
        value = float(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"{label} must be a number") from e
    if value < 0:
        raise HTTPException(status_code=422, detail=f"{label} cannot be negative")
    return value


@router.get("", response_class=HTMLResponse)
async def collections_page(request: Request, state: AppState = Depends(get_state)):
    cols = list_collections(state.engine)
    counts = collection_counts(state.engine)
    return request.app.state.templates.TemplateResponse(
        request,
        "collections.html",
        {
            "collections": cols,
            "counts": counts,
            "months": MONTHS,
            "total": state.library.count(),
        },
    )


@router.post("", dependencies=[Depends(require_token)])
async def create(
    name: str = Form(...),
    state: AppState = Depends(get_state),
):
    try:
        create_collection(state.engine, name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return RedirectResponse("/collections", status_code=303)


@router.post("/{collection_id}", dependencies=[Depends(require_token)])
async def update(
    collection_id: int,
    name: str = Form(...),
    weight: str = Form("1"),
    boost: str = Form("3"),
    start_month: str = Form(""),
    start_day: str = Form(""),
    end_month: str = Form(""),
    end_day: str = Form(""),
    state: AppState = Depends(get_state),
):
    fields = {
        "weight": _positive(weight, "weight"),
        "boost": _positive(boost, "boost"),
        **_season(start_month, start_day, end_month, end_day),
    }
    existing = get_collection(state.engine, collection_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="collection not found")
    # The default collection keeps its name; sending it back unchanged is not
    # an error, so only forward `name` when it actually differs.
    if not existing.is_default or name.strip() != existing.name:
        fields["name"] = name
    try:
        updated = update_collection(state.engine, collection_id, **fields)
    except CollectionInUseError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if updated is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return RedirectResponse("/collections", status_code=303)


@router.post("/{collection_id}/delete", dependencies=[Depends(require_token)])
async def remove(collection_id: int, state: AppState = Depends(get_state)):
    """POST rather than DELETE because this is submitted by an HTML form, and
    forms cannot issue DELETE."""
    try:
        deleted = delete_collection(state.engine, collection_id)
    except CollectionInUseError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not deleted:
        raise HTTPException(status_code=404, detail="collection not found")
    return RedirectResponse("/collections", status_code=303)


@router.post("/{collection_id}/images/{image_id}", dependencies=[Depends(require_token)])
async def add_image(collection_id: int, image_id: int, state: AppState = Depends(get_state)):
    if get_collection(state.engine, collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    if state.library.get(image_id) is None:
        raise HTTPException(status_code=404, detail="image not found")
    set_membership(state.engine, collection_id, image_id, True)
    return {"collection_id": collection_id, "image_id": image_id, "member": True}


@router.delete("/{collection_id}/images/{image_id}", dependencies=[Depends(require_token)])
async def remove_image(collection_id: int, image_id: int, state: AppState = Depends(get_state)):
    if get_collection(state.engine, collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    set_membership(state.engine, collection_id, image_id, False)
    return {"collection_id": collection_id, "image_id": image_id, "member": False}
