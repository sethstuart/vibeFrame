from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

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
from vibeframe.web.routes.images import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX, _page_numbers

router = APIRouter(prefix="/collections", tags=["collections"])

log = logging.getLogger("vibeframe")

MONTHS = [
    (1, "Jan"), (2, "Feb"), (3, "Mar"), (4, "Apr"), (5, "May"), (6, "Jun"),
    (7, "Jul"), (8, "Aug"), (9, "Sep"), (10, "Oct"), (11, "Nov"), (12, "Dec"),
]
_DAYS_IN_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
                  7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
# Matches the cap on /images/bulk/*: a huge list would hit SQLite's variable
# limit and 500 rather than saying no.
_BULK_MAX = 1000


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


def _partial_url(offset: int, limit: int, collection_id: int, q: str | None, sort: str) -> str:
    """URL the infinite-scroll sentinel fetches for the page after this one.

    Built here rather than in the template so the query string goes through
    urlencode — the pager's Jinja macro interpolates `q` raw, and a search
    term containing & would break. The collection id is always included, so
    the scroll chain never drops the active tab."""
    params: dict[str, str] = {
        "offset": str(offset + limit),
        "limit": str(limit),
        "partial": "true",
        "collection_id": str(collection_id),
    }
    if q:
        params["q"] = q
    if sort and sort != "newest":
        params["sort"] = sort
    return f"/collections?{urlencode(params)}"


@router.get("", response_class=HTMLResponse)
async def collections_page(
    request: Request,
    collection_id: int | None = None,
    limit: int = PAGE_SIZE_DEFAULT,
    offset: int = 0,
    q: str | None = None,
    sort: str = "newest",
    partial: bool = False,
    state: AppState = Depends(get_state),
):
    """The collection browser. Lands on the built-in Favorites collection;
    every other collection is a sub-tab (?collection_id=N) whose tab shows
    that collection's photos plus its weight/season settings.

    A ?collection_id that no longer exists (deleted between clicks) falls
    back to Favorites rather than rendering an unexplained empty page — the
    same fallback /images uses for a stale filter id."""
    limit = max(1, min(limit, PAGE_SIZE_MAX))
    offset = max(0, offset)
    cols = list_collections(state.engine)
    active = get_collection(state.engine, collection_id) if collection_id is not None else None
    if active is None:
        # Favorites always exists (boot seeds it, and it cannot be deleted),
        # and list_collections sorts it first.
        active = next((c for c in cols if c.is_default), cols[0])
    counts = collection_counts(state.engine)
    total = state.library.count(collection_id=active.id)
    total_pages = max(1, (total + limit - 1) // limit) if total else 1
    current_page = offset // limit + 1
    images = state.library.list(
        limit=limit,
        offset=offset,
        favorites_only=False,
        query=q,
        sort=sort,
        collection_id=active.id,
    )
    next_partial = (
        _partial_url(offset, limit, active.id, q, sort) if current_page < total_pages else None
    )
    if partial:
        # Bare cards for the mobile sentinel, same as /images?partial=true.
        return request.app.state.templates.TemplateResponse(
            request,
            "_photo_cards.html",
            {"images": images, "next_partial_url": next_partial},
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "collections.html",
        {
            "collections": cols,
            "active_col": active,
            "counts": counts,
            "months": MONTHS,
            "library_total": state.library.count(),
            "images": images,
            "favorite_ids": set(state.library.all_ids(favorites_only=True)),
            "total": total,
            "offset": offset,
            "limit": limit,
            "current_page": current_page,
            "total_pages": total_pages,
            "page_numbers": _page_numbers(current_page, total_pages),
            "q": q or "",
            "sort": sort,
            "next_partial_url": next_partial,
            # A plain str, deliberately NOT Markup: Jinja then autoescapes the
            # quotes inside it when it lands in data-collections="...".
            # jinja's |tojson escapes < > & ' but not ", so a collection named
            # with a quote would otherwise break out of the attribute.
            "collections_json": json.dumps(
                [{"id": c.id, "name": c.name} for c in cols]
            ),
        },
    )


@router.post("", dependencies=[Depends(require_token)])
async def create(
    name: str = Form(...),
    state: AppState = Depends(get_state),
):
    try:
        created = create_collection(state.engine, name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # Land on the new collection's tab rather than a generic page.
    return RedirectResponse(f"/collections?collection_id={created.id}", status_code=303)


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
    # Stay on the tab that was just edited.
    return RedirectResponse(f"/collections?collection_id={collection_id}", status_code=303)


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
    # The tab is gone, so land back on Favorites.
    return RedirectResponse("/collections", status_code=303)


@router.get("/for-image/{image_id}")
async def collections_for_image(image_id: int, state: AppState = Depends(get_state)):
    """Which collections this image is in. Fetched when the picker opens rather
    than embedded in every page render: with infinite scroll the page does not
    know up front which cards it will end up holding."""
    if state.library.get(image_id) is None:
        raise HTTPException(status_code=404, detail="image not found")
    return {"collection_ids": state.library.collections_for(image_id)}


@router.post("/{collection_id}/bulk", dependencies=[Depends(require_token)])
async def bulk_membership(
    collection_id: int, payload: dict, state: AppState = Depends(get_state)
):
    """Add or remove many images at once, for the library's selection mode.

    Declared before the /{collection_id}/images/{image_id} route so "bulk" is
    never parsed as an image id.
    """
    if get_collection(state.engine, collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    raw = payload.get("ids")
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="expected an 'ids' list")
    if len(raw) > _BULK_MAX:
        raise HTTPException(status_code=422, detail=f"at most {_BULK_MAX} ids per request")
    try:
        ids = [int(i) for i in raw]
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail="ids must be integers") from e
    member = bool(payload.get("member", True))
    changed = state.library.bulk_set_collection(ids, collection_id, member)
    return {"changed": changed, "member": member}


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
