"""
routes/context.py — POST /v1/context
Stores any context layer (category, merchant, trigger, customer) into the in-memory store.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from utils.store import store

router = APIRouter()

VALID_SCOPES = {"category", "merchant", "trigger", "customer"}


class ContextRequest(BaseModel):
    scope: str
    id: str
    version: int
    payload: dict[str, Any]


@router.post("/context")
def store_context(req: ContextRequest):
    if req.scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope '{req.scope}'. Must be one of: {sorted(VALID_SCOPES)}",
        )
    if not req.id:
        raise HTTPException(status_code=400, detail="id must not be empty")

    stored = store.put_context(req.scope, req.id, req.version, req.payload)

    return {
        "status": "stored" if stored else "skipped",
        "scope": req.scope,
        "id": req.id,
        "version": req.version,
        "reason": None if stored else "stale_version",
    }
