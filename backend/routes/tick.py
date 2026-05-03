"""
routes/tick.py — POST /v1/tick
Fires proactive outbound messages for a given trigger + merchant (+ optional customer).
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from utils.store import store
from services.composer import compose_message

router = APIRouter()


class TickRequest(BaseModel):
    trigger_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    conversation_id: Optional[str] = None


@router.post("/tick")
async def tick(req: TickRequest):
    # Validate required context exists
    trigger = store.get_trigger_payload(req.trigger_id)
    if not trigger:
        raise HTTPException(
            status_code=422,
            detail=f"trigger '{req.trigger_id}' not found — POST /v1/context first",
        )

    merchant = store.get_merchant_payload(req.merchant_id)
    if not merchant:
        raise HTTPException(
            status_code=422,
            detail=f"merchant '{req.merchant_id}' not found — POST /v1/context first",
        )

    # Suppression check
    suppression_key = trigger.get("suppression_key", f"{req.trigger_id}:{req.merchant_id}")
    if store.is_suppressed(suppression_key):
        return {
            "action": "suppress",
            "reason": "already_sent",
            "suppression_key": suppression_key,
        }

    # Compose
    result = await compose_message(req.trigger_id, req.merchant_id, req.customer_id)

    if not result:
        raise HTTPException(
            status_code=500,
            detail="Composition failed — missing category or context layers",
        )

    # Apply suppression
    key = result.get("suppression_key", suppression_key)
    store.suppress(key)

    # Create / retrieve conversation state
    conv_id = req.conversation_id or f"{req.trigger_id}:{req.merchant_id}"
    conv = store.get_conversation(conv_id)
    if not conv:
        conv = store.create_conversation(
            conversation_id=conv_id,
            merchant_id=req.merchant_id,
            customer_id=req.customer_id,
            trigger_id=req.trigger_id,
            send_as=result.get("send_as", "vera"),
        )

    import time
    conv.last_bot_body = result.get("body", "")
    conv.last_bot_sent_at = time.time()

    return {
        "action": "send",
        "conversation_id": conv_id,
        "body": result.get("body"),
        "cta": result.get("cta"),
        "send_as": result.get("send_as"),
        "suppression_key": key,
        "frame": result.get("frame"),
        "rationale": result.get("rationale"),
    }
