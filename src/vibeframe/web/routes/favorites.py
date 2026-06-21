from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from vibeframe.web.deps import AppState, get_state, require_token

router = APIRouter(prefix="/favorites", tags=["favorites"], dependencies=[Depends(require_token)])


@router.post("/{image_id}")
async def add_favorite(image_id: int, state: AppState = Depends(get_state)):
    if state.library.get(image_id) is None:
        raise HTTPException(status_code=404, detail="image not found")
    if state.library.is_favorite(image_id):
        return {"favorite": True}
    state.library.toggle_favorite(image_id)
    return {"favorite": True}


@router.delete("/{image_id}")
async def remove_favorite(image_id: int, state: AppState = Depends(get_state)):
    if not state.library.is_favorite(image_id):
        return {"favorite": False}
    state.library.toggle_favorite(image_id)
    return {"favorite": False}
