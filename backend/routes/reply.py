"""
routes/reply.py — POST /v1/reply
Handles inbound replies from merchants or customers and returns the next bot action.
"""

import time
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from utils.store import store
from services.reply_handler import handle_reply

router = APIRouter()


class ReplyRequest(BaseModel):
    conversation_id: str
    from_role: str           # "merchant" | "customer"
    message: str
    received_at: Optional[str] = None   # ISO timestamp; defaults to now


@router.post("/reply")
async def reply(req: ReplyRequest):
    conv = store.get_conversation(req.conversation_id)
    if not conv:
        raise HTTPException(
            status_code=404,
            detail=f"conversation '{req.conversation_id}' not found — fire /v1/tick first",
        )

    # Resolve timestamp string and float
    received_at_str = req.received_at or datetime.now(timezone.utc).isoformat()
    try:
        received_ts = datetime.fromisoformat(
            received_at_str.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        received_ts = time.time()
        received_at_str = datetime.now(timezone.utc).isoformat()

    turn_number = len(conv.turns) + 1

    result = await handle_reply(
        conv=conv,
        message=req.message,
        from_role=req.from_role,
        received_at=received_at_str,
        turn_number=turn_number,
    )

    return {
        "conversation_id": req.conversation_id,
        **result,
    }
