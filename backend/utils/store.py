"""
utils/store.py — In-memory store for all context state and conversations.
Thread-safe for single-worker FastAPI (uvicorn default).
"""

from __future__ import annotations
import time
from typing import Optional


class ContextRecord:
    __slots__ = ("version", "payload", "stored_at")

    def __init__(self, version: int, payload: dict, stored_at: float):
        self.version = version
        self.payload = payload
        self.stored_at = stored_at


class ConversationTurn:
    __slots__ = ("from_role", "message", "bot_reply", "ts")

    def __init__(self, from_role: str, message: str, bot_reply: str, ts: str):
        self.from_role = from_role
        self.message = message
        self.bot_reply = bot_reply
        self.ts = ts


class ConversationState:
    def __init__(
        self,
        conversation_id: str,
        merchant_id: str,
        customer_id: Optional[str],
        trigger_id: str,
        send_as: str,
    ):
        self.conversation_id = conversation_id
        self.merchant_id = merchant_id
        self.customer_id = customer_id
        self.trigger_id = trigger_id
        self.send_as = send_as
        self.turns: list[ConversationTurn] = []
        self.status = "active"          # active | waiting | ended
        self.wait_until: Optional[float] = None
        self.auto_reply_count = 0
        self.last_bot_body: str = ""
        self.last_bot_sent_at: Optional[float] = None   # set after every bot send
        self.intent_accepted = False
        self.started_at = time.time()


class Store:
    """Singleton in-memory store."""

    def __init__(self):
        # (scope, context_id) -> ContextRecord
        self._contexts: dict[tuple[str, str], ContextRecord] = {}
        # conversation_id -> ConversationState
        self._conversations: dict[str, ConversationState] = {}
        # suppression_key -> bool (dedup sent messages)
        self._suppressed: set[str] = set()
        self.boot_time: float = time.time()

    # ── Context operations ─────────────────────────────────────────────────

    def get_context(self, scope: str, context_id: str) -> Optional[ContextRecord]:
        return self._contexts.get((scope, context_id))

    def put_context(self, scope: str, context_id: str, version: int, payload: dict) -> bool:
        """
        Returns True if stored, False if stale (already have same or newer version).
        """
        key = (scope, context_id)
        existing = self._contexts.get(key)
        if existing and existing.version >= version:
            return False
        self._contexts[key] = ContextRecord(
            version=version, payload=payload, stored_at=time.time()
        )
        return True

    def count_by_scope(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for (scope, _) in self._contexts:
            counts[scope] = counts.get(scope, 0) + 1
        return counts

    def get_all_by_scope(self, scope: str) -> list[dict]:
        return [rec.payload for (s, _), rec in self._contexts.items() if s == scope]

    def get_payload(self, scope: str, context_id: str) -> Optional[dict]:
        rec = self._contexts.get((scope, context_id))
        return rec.payload if rec else None

    # ── Conversation operations ────────────────────────────────────────────

    def get_conversation(self, conversation_id: str) -> Optional[ConversationState]:
        return self._conversations.get(conversation_id)

    def create_conversation(
        self,
        conversation_id: str,
        merchant_id: str,
        customer_id: Optional[str],
        trigger_id: str,
        send_as: str,
    ) -> ConversationState:
        state = ConversationState(
            conversation_id, merchant_id, customer_id, trigger_id, send_as
        )
        self._conversations[conversation_id] = state
        return state

    def active_conversations_for_merchant(self, merchant_id: str) -> list[ConversationState]:
        return [
            c for c in self._conversations.values()
            if c.merchant_id == merchant_id and c.status == "active"
        ]

    # ── Suppression ────────────────────────────────────────────────────────

    def is_suppressed(self, key: str) -> bool:
        return key in self._suppressed

    def suppress(self, key: str):
        self._suppressed.add(key)

    # ── Shortcut helpers ───────────────────────────────────────────────────

    def get_trigger_payload(self, trigger_id: str) -> Optional[dict]:
        return self.get_payload("trigger", trigger_id)

    def get_merchant_payload(self, merchant_id: str) -> Optional[dict]:
        return self.get_payload("merchant", merchant_id)

    def get_category_payload(self, slug: str) -> Optional[dict]:
        return self.get_payload("category", slug)

    def get_customer_payload(self, customer_id: str) -> Optional[dict]:
        return self.get_payload("customer", customer_id)


# Global singleton
store = Store()
