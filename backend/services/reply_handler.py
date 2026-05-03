"""
services/reply_handler.py — Multi-turn conversation state machine.

v3 upgrades over v2:
- Conversation momentum: after YES action, always propose the NEXT logical step
- Confusion handling: detects "kya bol rahe ho?" / "what?" and re-anchors clearly
- Duplicate YES guard: repeated YES doesn't re-fire the same action
- Delayed reply handling: re-contextualises if conversation is stale (>4h gap)
- Human-like tone: warm, specific, never robotic
- All deterministic intents still bypass LLM (fast path preserved)
"""

from __future__ import annotations

import os
import re
import time
import json
from typing import Optional

import httpx

from utils.store import store, ConversationState

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 600

STOP_SUPPRESSION_DAYS = 30
NO_REPLY_SUPPRESSION_DAYS = 7
STALE_CONVERSATION_SECONDS = 4 * 3600  # 4 hours

# ── Auto-reply detection ──────────────────────────────────────────────────────

AUTO_REPLY_PATTERNS = [
    r"thank you for contact",
    r"aapki madad ke liye shukriya",
    r"this is an automated",
    r"i am an automated",
    r"main ek automated",
    r"will get back to you",
    r"our team will",
    r"hamari team.*pahuncha",
    r"please leave a message",
    r"currently unavailable",
    r"auto.?reply",
    r"out of office",
    r"on leave",
    r"unavailable till",
    r"bahut.bahut shukriya.*team",
]

# ── Intent patterns ───────────────────────────────────────────────────────────

ACCEPT_SIGNALS = [
    r"\byes\b", r"\bha[anh]\b", r"\bhaan\b", r"\bok\b", r"\bokay\b",
    r"\bsure\b", r"\bgo ahead\b", r"\bdo it\b", r"\bkaro\b", r"\bkar do\b",
    r"\bchale\b", r"\bchalega\b", r"\blet'?s do\b", r"\bsend\b",
    r"\bproceed\b", r"\bconfirm\b", r"\bbook\b", r"\bstart kar\b", r"\bshuru kar\b",
]

STOP_SIGNALS = [
    r"\bno\b", r"\bnahi\b", r"\bnahin\b", r"\bnot interested\b",
    r"\bstop\b", r"\bband kar\b", r"\bmat bhejo\b", r"\bblock\b",
    r"\bundsubscribe\b", r"\bunsubscribe\b", r"\bremove me\b",
    r"\bopt out\b", r"\bopt-out\b", r"\bdon'?t contact\b",
]

HELP_SIGNALS = [
    r"\bwhat\b", r"\bhow\b", r"\bkya hai\b", r"\bkaise\b", r"\bbatao\b",
    r"\btell me\b", r"\bexplain\b", r"\bsamjhao\b", r"\bwhy\b",
    r"\bkyun\b", r"\bwhat is\b", r"\bwhat does\b", r"\bmeans\b", r"\bmatlb\b",
]

CONFUSION_SIGNALS = [
    r"\bkya bol\b", r"\bsamjha nahi\b", r"\bsamajh nahi\b", r"\bnot sure\b",
    r"\bconfused\b", r"\bdon'?t understand\b", r"\bwhat do you mean\b",
    r"\bkya matlab\b", r"\bhuh\b", r"\bwhat\?\s*$", r"\beh\b",
    r"\bsory\b", r"\bsorry\b",   # "sorry, what?" type confusion
]

HOSTILE_SIGNALS = [
    r"\bscam\b", r"\bfraud\b", r"\bfake\b", r"\bstop spam\b",
    r"\bwho are you\b", r"\bnever contact\b", r"\breport\b", r"\bcomplaint\b",
]

# ── Post-action next-step map (conversation momentum) ────────────────────────
# After the bot takes action for trigger_kind X, what's the NEXT logical ask?

NEXT_STEP_MAP_EN = {
    "perf_dip": (
        "Post is live. While I have you — your {category} profile photo is {photo_age} old. "
        "A fresh photo typically adds 8-12% more clicks. Want me to flag the best one to swap?"
    ),
    "perf_spike": (
        "Offer is live. One more thing — your last Google review response was {review_gap} ago. "
        "Responding to recent reviews during a traffic spike improves conversions. "
        "Want me to draft responses to your last 3 reviews?"
    ),
    "stale_posts": (
        "Post published. Quick follow-up — your active offer expires in {offer_expiry_days} days. "
        "Want me to refresh it now so you don't lose momentum?"
    ),
    "ctr_below_peer": (
        "Profile updated. Next highest-impact fix: your offer section hasn't changed in {offer_age} days. "
        "Updating it now would compound the CTR improvement. Shall I?"
    ),
    "competitor_opened": (
        "Profile sharpened. One more defensive move — want me to check your GBP listing too? "
        "It takes 5 mins and closes another visibility gap competitors could exploit."
    ),
    "festival_upcoming": (
        "Campaign is live. To maximise it — want me to schedule a reminder post for 1 day before the festival too? "
        "That double-tap drives 20-25% more conversions than a single post."
    ),
    "dormant_with_vera": (
        "Profile audit done — top 2 fixes flagged. Want me to action both right now, or start with the higher-impact one?"
    ),
    "research_digest": (
        "Draft is ready. Want me to also schedule it to go out tomorrow morning at 9am? "
        "Morning sends get 30-40% better open rates for patient outreach."
    ),
    "recall_due": (
        "Recall message sent. Would you like me to set an auto-reminder if the patient doesn't respond in 48 hours?"
    ),
    "customer_lapsed_soft": (
        "Re-engagement sent. Want me to set a follow-up for 3 days from now if they don't reply?"
    ),
    "milestone_reached": (
        "Celebratory post is live. Want me to also send a thank-you message to your top {top_cust_count} returning customers? "
        "It builds loyalty and can drive repeat bookings this week."
    ),
}

NEXT_STEP_MAP_HI = {
    "perf_dip": (
        "Post live ho gaya. Ek aur cheez — aapki {category} profile photo {photo_age} purani hai. "
        "Fresh photo se 8-12% zyada clicks milte hain. Swap karne ke liye best wali flag karun?"
    ),
    "perf_spike": (
        "Offer live. Ek aur kaam — last Google review response {review_gap} pehle tha. "
        "Traffic spike mein reviews respond karne se conversions badhte hain. "
        "Last 3 reviews ke liye draft kar dun?"
    ),
    "stale_posts": (
        "Post publish ho gaya. Quick follow-up — aapka active offer {offer_expiry_days} din mein expire hoga. "
        "Abhi refresh karo toh momentum continue rahega. Karun?"
    ),
    "ctr_below_peer": (
        "Profile update ho gaya. Agla high-impact fix: offer section {offer_age} din se same hai. "
        "Ise update karna CTR improvement aur compound karega. Karun?"
    ),
    "competitor_opened": (
        "Profile sharp ho gaya. Ek aur defensive move — GBP listing bhi check karun? "
        "5 minute lagenge aur ek aur visibility gap band ho jaayega."
    ),
    "festival_upcoming": (
        "Campaign live hai. Maximize karne ke liye — festival se 1 din pehle ek reminder post bhi schedule karun? "
        "Double-tap se 20-25% zyada conversions milte hain."
    ),
    "dormant_with_vera": (
        "Profile audit done — top 2 fixes ready. Dono abhi action karun, ya pehle higher-impact wala?"
    ),
    "research_digest": (
        "Draft ready hai. Kal subah 9am pe schedule kar dun? "
        "Morning sends pe 30-40% better open rates milte hain patient outreach mein."
    ),
    "recall_due": (
        "Recall message bhej diya. 48 hours mein reply na aaye toh auto-reminder set kar dun?"
    ),
    "customer_lapsed_soft": (
        "Re-engagement message bhej diya. 3 din baad reply na aaye toh follow-up set karun?"
    ),
    "milestone_reached": (
        "Celebratory post live! Aapke top {top_cust_count} returning customers ko thank-you message bhi bhejun? "
        "Loyalty badhti hai aur is hafte repeat bookings aa sakti hain."
    ),
}


def _get_next_step(trigger_kind: str, merchant: dict, category: dict, hi: bool) -> str:
    """Build the next-step message after a YES action."""
    mapping = NEXT_STEP_MAP_HI if hi else NEXT_STEP_MAP_EN
    template = mapping.get(trigger_kind, "")
    if not template:
        return ""

    # Fill template variables with real data where possible
    perf = merchant.get("performance", {})
    offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    customer_agg = merchant.get("customer_aggregate", {})
    category_name = category.get("display_name", category.get("slug", ""))

    replacements = {
        "{category}": category_name,
        "{photo_age}": "6+ months",   # would come from merchant profile metadata
        "{review_gap}": "2 weeks",
        "{offer_expiry_days}": str(offers[0].get("days_remaining", 7)) if offers else "7",
        "{offer_age}": "14 days",
        "{top_cust_count}": str(customer_agg.get("top_customer_count", 5)),
    }
    for k, v in replacements.items():
        template = template.replace(k, v)

    return template


# ── Trigger → immediate action description ────────────────────────────────────

TRIGGER_ACTION_MAP = {
    "perf_dip": {
        "en": (
            "On it — publishing a fresh post and refreshing your offer right now. "
            "Should be live in ~2 mins. Any specific service you want highlighted, or shall I pick the best one?"
        ),
        "hi": (
            "Shuru kar diya — fresh post aur offer refresh abhi ho raha hai. "
            "~2 minute mein live hoga. Koi specific service highlight karni hai, ya main best wala choose karoon?"
        ),
    },
    "perf_spike": {
        "en": (
            "Activating a time-limited offer now to capture the traffic. "
            "Want to set an expiry (e.g., 'this week only') or keep it open-ended?"
        ),
        "hi": (
            "Time-limited offer abhi activate kar rahi hoon. "
            "Expiry set karein? (e.g., 'sirf is hafte') Ya open-ended rakhein?"
        ),
    },
    "competitor_opened": {
        "en": (
            "Starting now — profile refresh, offer update, GBP check. "
            "Which is most urgent for you: profile content, offer, or GBP listing?"
        ),
        "hi": (
            "Shuru ho gaya — profile, offer, GBP sab. "
            "Kahan se pehle? Profile content, offer, ya GBP listing?"
        ),
    },
    "stale_posts": {
        "en": (
            "Publishing a fresh post right now with your active offer. "
            "Any specific angle — new service, seasonal, or customer story?"
        ),
        "hi": (
            "Abhi fresh post publish kar rahi hoon with active offer. "
            "Koi specific angle? New service, seasonal, ya customer story?"
        ),
    },
    "ctr_below_peer": {
        "en": (
            "Updating your profile now — post, offer refresh, keywords. "
            "Any specific service you want front-and-centre?"
        ),
        "hi": (
            "Profile update ho raha hai — post, offer, keywords sab. "
            "Koi specific service front-and-centre chahiye?"
        ),
    },
    "festival_upcoming": {
        "en": (
            "Setting up your festive campaign now. "
            "Do you want a specific end date, or run it through the full festival?"
        ),
        "hi": (
            "Festive campaign set ho raha hai. "
            "Specific end date chahiye, ya pura festival chalao?"
        ),
    },
    "milestone_reached": {
        "en": (
            "Drafting your celebratory post now — milestone front-and-centre with a booking CTA. "
            "Any specific message or personal note you want included?"
        ),
        "hi": (
            "Celebratory post draft ho raha hai — milestone highlight + booking CTA. "
            "Koi personal message add karni hai?"
        ),
    },
    "dormant_with_vera": {
        "en": (
            "Running a quick profile audit now — I'll flag the top 2 highest-impact fixes. "
            "Give me 2 mins."
        ),
        "hi": (
            "Profile audit shuru ho gaya — top 2 highest-impact fixes bataungi. "
            "2 minute do."
        ),
    },
    "research_digest": {
        "en": (
            "Drafting the patient-education WhatsApp now — anchored on the key finding, "
            "simple language. Want a booking prompt at the end?"
        ),
        "hi": (
            "Patient-education WhatsApp draft ho raha hai — key finding anchor, simple language. "
            "End mein booking prompt add karoon?"
        ),
    },
    "recall_due": {
        "en": (
            "Sending the recall message now with your active offer. "
            "Morning or evening send time?"
        ),
        "hi": (
            "Recall message active offer ke saath bhej rahi hoon. "
            "Morning ya evening?"
        ),
    },
    "customer_lapsed_soft": {
        "en": (
            "Sending the re-engagement message now. "
            "Want to include a returning-patient special offer?"
        ),
        "hi": (
            "Re-engagement message bhej rahi hoon. "
            "Returning patient ke liye special offer include karoon?"
        ),
    },
    "review_theme_emerged": {
        "en": (
            "Drafting a professional response to the reviews right now. "
            "Also adding a note to your profile to address the theme proactively. "
            "Want to review the draft before I publish?"
        ),
        "hi": (
            "Reviews ke liye professional response draft ho raha hai. "
            "Profile note bhi add kar rahi hoon proactively. "
            "Publish se pehle draft dekhna chahenge?"
        ),
    },
}


def detect_auto_reply(message: str) -> bool:
    msg_lower = message.lower().strip()
    return any(re.search(p, msg_lower) for p in AUTO_REPLY_PATTERNS)


def detect_intent(message: str) -> str:
    """Returns: 'accept' | 'stop' | 'help' | 'confusion' | 'hostile' | 'neutral'"""
    msg_lower = message.lower().strip()
    for p in HOSTILE_SIGNALS:
        if re.search(p, msg_lower):
            return "hostile"
    for p in STOP_SIGNALS:
        if re.search(p, msg_lower):
            return "stop"
    for p in CONFUSION_SIGNALS:
        if re.search(p, msg_lower):
            return "confusion"
    for p in ACCEPT_SIGNALS:
        if re.search(p, msg_lower):
            return "accept"
    for p in HELP_SIGNALS:
        if re.search(p, msg_lower):
            return "help"
    return "neutral"


def _is_stale_conversation(conv: ConversationState, received_at_ts: float) -> bool:
    """Return True if last bot message was sent >4h ago."""
    if not conv.last_bot_sent_at:
        return False
    return (received_at_ts - conv.last_bot_sent_at) > STALE_CONVERSATION_SECONDS


def _make_turn(from_role: str, message: str, bot_reply: str, ts: str):
    from utils.store import ConversationTurn
    return ConversationTurn(from_role=from_role, message=message, bot_reply=bot_reply, ts=ts)


def _apply_suppression(conv: ConversationState, days: int):
    key = f"conv_suppress:{conv.merchant_id}:{conv.trigger_id}"
    store.suppress(key)
    expiry = int(time.time() + days * 86400)
    store.suppress(f"conv_suppress_until:{conv.merchant_id}:{conv.trigger_id}:{expiry}")


# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_reply(
    conv: ConversationState,
    message: str,
    from_role: str,
    received_at: str,
    turn_number: int,
) -> dict:
    """
    Process an inbound reply and return the bot's next action dict.
    """
    received_ts = time.time()

    # ── Auto-reply guard ───────────────────────────────────────────────────
    if detect_auto_reply(message):
        conv.auto_reply_count = getattr(conv, "auto_reply_count", 0) + 1
        conv.turns.append(_make_turn(from_role, message, "", received_at))

        if conv.auto_reply_count == 1:
            merchant = store.get_merchant_payload(conv.merchant_id)
            hi = "hi" in (merchant or {}).get("identity", {}).get("languages", ["en"])
            owner = (merchant or {}).get("identity", {}).get("owner_first_name", "there")
            body = (
                f"Samajh gayi — automated reply lag raha hai. "
                f"{owner} ji, kya aap thodi der ke liye available hain? Short update share karna tha."
            ) if hi else (
                f"Got it — looks like an automated reply. "
                f"{owner}, are you available for a moment? Had a short update to share."
            )
            conv.last_bot_body = body
            conv.last_bot_sent_at = received_ts
            return {"action": "send", "body": body, "cta": "open_ended",
                    "rationale": "Auto-reply detected — one clarifying attempt"}
        else:
            conv.status = "ended"
            _apply_suppression(conv, days=3)
            return {"action": "end", "rationale": "Auto-reply confirmed twice — graceful exit"}

    # ── Record turn ────────────────────────────────────────────────────────
    conv.turns.append(_make_turn(from_role, message, "", received_at))

    # ── Stale conversation re-context ──────────────────────────────────────
    if _is_stale_conversation(conv, received_ts):
        return _handle_stale_reentry(conv, message, received_ts)

    # ── Intent detection ───────────────────────────────────────────────────
    intent = detect_intent(message)

    # ── HOSTILE ───────────────────────────────────────────────────────────
    if intent == "hostile":
        conv.status = "ended"
        _apply_suppression(conv, days=STOP_SUPPRESSION_DAYS)
        merchant = store.get_merchant_payload(conv.merchant_id)
        hi = "hi" in (merchant or {}).get("identity", {}).get("languages", ["en"])
        body = (
            "Maafi chahti hoon agar inconvenience hua. Hum dobara contact nahi karenge."
            if hi else
            "Apologies for any inconvenience. We will not contact you again."
        )
        conv.last_bot_body = body
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "Hostile intent — exit + 30d suppress"}

    # ── STOP ──────────────────────────────────────────────────────────────
    if intent == "stop":
        conv.status = "ended"
        _apply_suppression(conv, days=STOP_SUPPRESSION_DAYS)
        merchant = store.get_merchant_payload(conv.merchant_id)
        hi = "hi" in (merchant or {}).get("identity", {}).get("languages", ["en"])
        body = (
            "Bilkul, koi baat nahi. Zaroorat ho toh main hamesha available hoon. 🙂"
            if hi else
            "Understood, no problem at all. I'm here whenever you need me. 🙂"
        )
        conv.last_bot_body = body
        return {"action": "send", "body": body, "cta": "none",
                "rationale": f"Merchant opted out — {STOP_SUPPRESSION_DAYS}d suppression applied"}

    # ── CONFUSION ─────────────────────────────────────────────────────────
    if intent == "confusion":
        return _handle_confusion(conv, message, received_ts)

    # ── ACCEPT (YES) ──────────────────────────────────────────────────────
    if intent == "accept":
        return _handle_accept(conv, message, received_ts)

    # ── HELP ──────────────────────────────────────────────────────────────
    if intent == "help":
        return _handle_help(conv, message, received_ts)

    # ── Turn limit ─────────────────────────────────────────────────────────
    if len(conv.turns) >= 6:
        conv.status = "ended"
        _apply_suppression(conv, days=NO_REPLY_SUPPRESSION_DAYS)
        merchant = store.get_merchant_payload(conv.merchant_id)
        hi = "hi" in (merchant or {}).get("identity", {}).get("languages", ["en"])
        body = (
            "Koi baat nahi! Jab bhi ready ho, main yahan hoon. 🙂"
            if hi else
            "No worries! I'm here whenever you're ready. 🙂"
        )
        conv.last_bot_body = body
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "6+ turns — graceful exit + 7d suppress"}

    # ── NEUTRAL → LLM ─────────────────────────────────────────────────────
    result = await _call_claude_for_reply(conv, message, intent)
    if result and result.get("action") == "send" and result.get("body"):
        conv.last_bot_body = result["body"]
        conv.last_bot_sent_at = received_ts
        return result

    return _rule_based_neutral(conv, message, received_ts)


# ── Intent handlers ───────────────────────────────────────────────────────────

def _handle_accept(conv: ConversationState, message: str, ts: float) -> dict:
    """
    YES intent.
    - First YES: immediate action description + ask clarifying sub-question
    - Second YES (same action already triggered): propose NEXT logical step
    - Repeated YES beyond that: acknowledge warmly, no duplicate action
    """
    merchant = store.get_merchant_payload(conv.merchant_id)
    trigger = store.get_trigger_payload(conv.trigger_id)
    category_slug = (merchant or {}).get("category_slug", "")
    category = store.get_category_payload(category_slug) or {}
    languages = (merchant or {}).get("identity", {}).get("languages", ["en"])
    hi = "hi" in languages
    trigger_kind = (trigger or {}).get("kind", "")

    # ── Duplicate YES guard ────────────────────────────────────────────────
    already_accepted = getattr(conv, "intent_accepted", False)
    next_step_done = getattr(conv, "next_step_proposed", False)

    if already_accepted and not next_step_done:
        # Second YES — propose next step (conversation momentum)
        conv.next_step_proposed = True
        next_step = _get_next_step(trigger_kind, merchant or {}, category, hi)
        if next_step:
            conv.last_bot_body = next_step
            conv.last_bot_sent_at = ts
            return {"action": "send", "body": next_step, "cta": "binary_yes_stop",
                    "rationale": "Second YES — conversation momentum: next logical step proposed"}
        # Fallback if no next step defined
        body = ("Sab kuch already update ho gaya hai — koi aur cheez chahiye?" if hi
                else "Everything's already updated — anything else you'd like me to look at?")
        conv.last_bot_body = body
        conv.last_bot_sent_at = ts
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Second YES — no next step defined, open-ended"}

    if already_accepted and next_step_done:
        # Third+ YES — acknowledge, don't re-fire
        body = ("Woh bhi ho jaayega! Koi aur cheez?" if hi
                else "On it! Anything else while I'm at it?")
        conv.last_bot_body = body
        conv.last_bot_sent_at = ts
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Repeated YES — warm ack, no duplicate action"}

    # ── First YES: immediate action ────────────────────────────────────────
    conv.intent_accepted = True
    conv.next_step_proposed = False

    action_entry = TRIGGER_ACTION_MAP.get(trigger_kind, {})
    body = action_entry.get("hi" if hi else "en", "")
    if not body:
        owner = (merchant or {}).get("identity", {}).get("owner_first_name", "there")
        body = (f"Perfect, {owner} ji — shuru ho gaya. Thodi der mein update karungi." if hi
                else f"On it, {owner} — starting now. Will update you shortly.")

    conv.last_bot_body = body
    conv.last_bot_sent_at = ts
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": f"First YES → immediate action for {trigger_kind}"}


def _handle_confusion(conv: ConversationState, message: str, ts: float) -> dict:
    """
    Re-anchor the conversation with the core insight, simply stated.
    Don't repeat the full original message — distil it to one clear sentence.
    """
    merchant = store.get_merchant_payload(conv.merchant_id)
    trigger = store.get_trigger_payload(conv.trigger_id)
    languages = (merchant or {}).get("identity", {}).get("languages", ["en"])
    hi = "hi" in languages
    perf = (merchant or {}).get("performance", {})
    views = perf.get("views", 0)
    ctr = int(perf.get("ctr", 0) * 100)
    trigger_kind = (trigger or {}).get("kind", "")
    category_slug = (merchant or {}).get("category_slug", "")
    category = store.get_category_payload(category_slug) or {}
    peer_ctr = int(category.get("peer_stats", {}).get("avg_ctr", 0) * 100)
    predicted_gain = max(1, int(views * (category.get("peer_stats", {}).get("avg_ctr", 0) - perf.get("ctr", 0))))

    # Distil the core problem to one concrete sentence
    CLARITY_EN = {
        "perf_dip": (
            f"Simply: your profile has {views} views/month but only {ctr}% convert to calls — "
            f"peers get {peer_ctr}%. One post fix today = ~{predicted_gain} more calls/month. Want me to?"
        ),
        "perf_spike": (
            f"You're getting unusually high traffic right now ({views} views). "
            f"A quick offer push converts that into actual calls. Say YES and I'll set it up."
        ),
        "competitor_opened": (
            f"A new competitor just opened nearby. Your CTR is {ctr}% — below the {peer_ctr}% area average. "
            f"I can sharpen your profile before customers switch. Want me to?"
        ),
        "stale_posts": (
            f"Your last post is old. Fresh posts keep CTR up — right now yours is {ctr}% vs peer {peer_ctr}%. "
            f"I'll publish one in 2 mins. YES?"
        ),
        "research_digest": (
            f"New clinical research dropped — useful for patient trust-building. "
            f"I'll draft a simple WhatsApp you can send patients. YES?"
        ),
    }
    CLARITY_HI = {
        "perf_dip": (
            f"Simple baat: {views} views hain per month, lekin sirf {ctr}% calls mein convert hota hai — "
            f"peers {peer_ctr}% pe hain. Ek post aaj = ~{predicted_gain} extra calls/month. Karun?"
        ),
        "perf_spike": (
            f"Abhi unusual zyada traffic aa raha hai ({views} views). "
            f"Quick offer push se yeh actual calls mein badal sakta hai. YES bolo — abhi set karti hoon."
        ),
        "competitor_opened": (
            f"Paas mein naya competitor khula hai. Aapka CTR {ctr}% — area average {peer_ctr}% se kam. "
            f"Profile sharpen kar sakti hoon before customers shift hon. Karun?"
        ),
        "stale_posts": (
            f"Last post purana hai. Fresh posts CTR upar rakhte hain — aapka {ctr}% vs peer {peer_ctr}%. "
            f"2 minute mein ek publish kar deti hoon. YES?"
        ),
        "research_digest": (
            f"Nayi clinical research aayi — patient trust ke liye useful. "
            f"Ek simple WhatsApp draft karti hoon jo aap patients ko bhej sako. YES?"
        ),
    }
    mapping = CLARITY_HI if hi else CLARITY_EN
    body = mapping.get(
        trigger_kind,
        (f"Maafi — clearly bolti hoon. Aapke {views} views hain lekin CTR {ctr}% pe hai — "
         f"peers {peer_ctr}% pe hain. Ek quick update se ~{predicted_gain} zyada calls aa sakti hain. Karun?"
         if hi else
         f"Let me be clearer. You have {views} views but CTR at {ctr}% — peers are at {peer_ctr}%. "
         f"One quick update could mean ~{predicted_gain} more calls/month. Want me to?")
    )
    conv.last_bot_body = body
    conv.last_bot_sent_at = ts
    return {"action": "send", "body": body, "cta": "binary_yes_stop",
            "rationale": "Confusion detected — re-anchored with distilled single-sentence insight"}


def _handle_help(conv: ConversationState, message: str, ts: float) -> dict:
    """Answer with merchant's actual data, not placeholders."""
    merchant = store.get_merchant_payload(conv.merchant_id)
    trigger = store.get_trigger_payload(conv.trigger_id)
    languages = (merchant or {}).get("identity", {}).get("languages", ["en"])
    hi = "hi" in languages
    perf = (merchant or {}).get("performance", {})
    views = perf.get("views", 0)
    ctr = int(perf.get("ctr", 0) * 100)
    calls = perf.get("calls", 0)
    trigger_kind = (trigger or {}).get("kind", "")
    msg_lower = message.lower()

    if any(w in msg_lower for w in ["ctr", "click", "click through"]):
        body = (
            f"CTR matlab click-through rate — kitne log profile dekhte hain aur call/direction tap karte hain. "
            f"Aapka {ctr}% hai — matlab {views} views mein se sirf {calls} calls. "
            f"Peers {int((store.get_category_payload((merchant or {}).get('category_slug','')) or {}).get('peer_stats',{}).get('avg_ctr',0)*100)}% pe hain."
            if hi else
            f"CTR = click-through rate — how many profile viewers tap to call or get directions. "
            f"Yours is {ctr}%, so {views} views → {calls} calls/month. "
            f"Peers run higher, which is why we're here."
        )
    elif any(w in msg_lower for w in ["views", "traffic", "dekha"]):
        body = (
            f"Views matlab last 30 din mein kitne log aapka magicpin profile dekha. "
            f"Aapke {views} views hain — aur {ctr}% CTR se {calls} calls mil rahi hain. "
            f"CTR upar aaye toh usi traffic se zyada calls milenge."
            if hi else
            f"Views = how many people saw your magicpin profile in 30 days — yours is {views}. "
            f"With {ctr}% CTR, that converts to {calls} calls/month. "
            f"Raising CTR means more calls from the same traffic — no extra ad spend."
        )
    elif any(w in msg_lower for w in ["vera", "kaun", "who", "aap kaun"]):
        body = (
            f"Main Vera hoon — magicpin ka AI assistant. Aapki profile, offers, aur customer outreach manage karti hoon. "
            f"Aapke last 30 days: {views} views, {ctr}% CTR, {calls} calls. Batao kya help chahiye?"
            if hi else
            f"I'm Vera — magicpin's AI assistant. I manage your profile, offers, and customer outreach. "
            f"Your last 30 days: {views} views, {ctr}% CTR, {calls} calls. What can I help with?"
        )
    else:
        offers = (merchant or {}).get("offers", [])
        active = [o for o in offers if o.get("status") == "active"]
        if hi:
            offer_line = f"Active offer: '{active[0].get('title','')}'" if active else "Koi active offer nahi hai"
            body = (
                f"Vera hoon — magicpin assistant. Last 30 days: {views} views, {ctr}% CTR, {calls} calls. "
                f"{offer_line}. Kisi specific cheez mein madad chahiye?"
            )
        else:
            offer_line = f"Active offer: '{active[0].get('title','')}'" if active else "No active offer currently"
            body = (
                f"I'm Vera, your magicpin assistant. Last 30 days: {views} views, {ctr}% CTR, {calls} calls. "
                f"{offer_line}. What specifically can I help with?"
            )

    conv.last_bot_body = body
    conv.last_bot_sent_at = ts
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": "Help intent — answered with merchant-specific data"}


def _handle_stale_reentry(conv: ConversationState, message: str, ts: float) -> dict:
    """
    Merchant replied after a 4h+ gap.
    Don't assume context — briefly re-anchor and re-ask.
    """
    merchant = store.get_merchant_payload(conv.merchant_id)
    trigger = store.get_trigger_payload(conv.trigger_id)
    languages = (merchant or {}).get("identity", {}).get("languages", ["en"])
    hi = "hi" in languages
    perf = (merchant or {}).get("performance", {})
    views = perf.get("views", 0)
    ctr = int(perf.get("ctr", 0) * 100)
    trigger_kind = (trigger or {}).get("kind", "")
    category_slug = (merchant or {}).get("category_slug", "")
    category = store.get_category_payload(category_slug) or {}
    peer_ctr = int(category.get("peer_stats", {}).get("avg_ctr", 0) * 100)

    STALE_EN = {
        "perf_dip": (
            f"Welcome back! Quick recap: your CTR is at {ctr}% vs peer {peer_ctr}% — "
            f"I was about to push a fresh post + offer refresh to close that gap. Still good to go? Reply YES."
        ),
        "perf_spike": (
            f"Hey, welcome back! Your traffic spike ({views} views) is still active. "
            f"Still time to push a time-limited offer. Want me to? Reply YES."
        ),
        "festival_upcoming": (
            f"Welcome back! Festival campaign — still {(trigger or {}).get('payload', {}).get('days_away', 'a few')} days to go. "
            f"Good to set it up now? Reply YES."
        ),
    }
    STALE_HI = {
        "perf_dip": (
            f"Wapas aaye! Quick recap: CTR {ctr}% pe hai vs peer {peer_ctr}% — "
            f"fresh post + offer refresh push karne wali thi gap close karne ke liye. Abhi bhi karun? Reply YES."
        ),
        "perf_spike": (
            f"Wapas aaye! Traffic spike ({views} views) abhi bhi active hai. "
            f"Time-limited offer push karun? Reply YES."
        ),
        "festival_upcoming": (
            f"Wapas aaye! Festival campaign — abhi bhi {(trigger or {}).get('payload', {}).get('days_away', 'kuch')} din baaki hain. "
            f"Abhi set karun? Reply YES."
        ),
    }
    mapping = STALE_HI if hi else STALE_EN
    body = mapping.get(
        trigger_kind,
        (f"Wapas aaye! Main aapke profile ke baare mein baat kar rahi thi — "
         f"CTR {ctr}% vs peer {peer_ctr}%. Abhi bhi help karun? Reply YES."
         if hi else
         f"Welcome back! I was helping with your profile — "
         f"CTR at {ctr}% vs peer {peer_ctr}%. Still want me to take care of it? Reply YES.")
    )
    conv.last_bot_body = body
    conv.last_bot_sent_at = ts
    return {"action": "send", "body": body, "cta": "binary_yes_stop",
            "rationale": "Stale conversation (>4h) — re-anchored with brief recap"}


def _rule_based_neutral(conv: ConversationState, message: str, ts: float) -> dict:
    """Fallback for neutral intent when LLM unavailable."""
    merchant = store.get_merchant_payload(conv.merchant_id)
    languages = (merchant or {}).get("identity", {}).get("languages", ["en"])
    hi = "hi" in languages
    perf = (merchant or {}).get("performance", {})
    views = perf.get("views", 0)
    ctr = int(perf.get("ctr", 0) * 100)
    category_slug = (merchant or {}).get("category_slug", "")
    category = store.get_category_payload(category_slug) or {}
    peer_ctr = int(category.get("peer_stats", {}).get("avg_ctr", 0) * 100)
    predicted_gain = max(1, int(views * (category.get("peer_stats", {}).get("avg_ctr", 0) - perf.get("ctr", 0))))

    body = (
        f"Samajh gayi! Sirf yaad dilata hoon — {views} views last month, CTR {ctr}% hai, "
        f"peers {peer_ctr}% pe hain. ~{predicted_gain} extra calls/month possible hai ek update se. Karun? Reply YES."
        if hi else
        f"Got it! Just to recap — {views} views last month, CTR at {ctr}% vs peer {peer_ctr}%. "
        f"~{predicted_gain} more calls/month available with one update. Shall I? Reply YES."
    )
    conv.last_bot_body = body
    conv.last_bot_sent_at = ts
    return {"action": "send", "body": body, "cta": "binary_yes_stop",
            "rationale": "Neutral fallback — re-anchor with numbers + prediction"}


# ── LLM call (neutral intent only) ───────────────────────────────────────────

REPLY_SYSTEM_PROMPT = """You are Vera, magicpin's merchant AI assistant — WhatsApp conversation.

RULES:
1. Use ONLY facts from context. Never invent data.
2. 2-4 sentences max. WhatsApp friendly.
3. Match merchant's language (hi-en mix if they speak Hindi).
4. Reference at least 1 number from performance data.
5. Single CTA as the last sentence.
6. No repetition of prior messages.
7. Warm but professional — not robotic, not over-enthusiastic.

OUTPUT — return ONLY JSON:
{"action": "send"|"wait"|"end", "body": "<msg if send>", "cta": "open_ended|binary_yes_stop|none", "rationale": "<brief>"}"""


async def _call_claude_for_reply(conv: ConversationState, message: str, intent: str) -> Optional[dict]:
    try:
        merchant = store.get_merchant_payload(conv.merchant_id)
        trigger = store.get_trigger_payload(conv.trigger_id)
        if not merchant or not trigger:
            return None

        category_slug = merchant.get("category_slug", "")
        category = store.get_category_payload(category_slug) or {}
        perf = merchant.get("performance", {})

        history_lines = []
        for t in conv.turns[-4:]:
            role = "MERCHANT" if t.from_role in ("merchant", "customer") else "VERA"
            history_lines.append(f"{role}: {t.message}")
            if getattr(t, "bot_reply", ""):
                history_lines.append(f"VERA: {t.bot_reply}")

        prompt = (
            f"Intent: {intent.upper()} | Turn: {len(conv.turns)}\n\n"
            f"METRICS: views={perf.get('views',0)}, ctr={int(perf.get('ctr',0)*100)}%, "
            f"calls={perf.get('calls',0)}, peer_ctr={int(category.get('peer_stats',{}).get('avg_ctr',0)*100)}%\n\n"
            f"TRIGGER: {json.dumps({'kind': trigger.get('kind'), 'payload': trigger.get('payload', {})}, ensure_ascii=False)}\n\n"
            f"MERCHANT: {json.dumps({'identity': merchant.get('identity'), 'offers': merchant.get('offers',[])[:2]}, ensure_ascii=False)}\n\n"
            f"CONVERSATION:\n{chr(10).join(history_lines)}\n\n"
            f"LATEST: {message}\n\n"
            f"Advance toward YES. Include 1+ number. Return only JSON."
        )

        async with httpx.AsyncClient(timeout=22.0) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": MODEL,
                    "max_tokens": MAX_TOKENS,
                    "temperature": 0,
                    "system": REPLY_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            return None

        data = resp.json()
        raw = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        if "action" in parsed:
            return parsed
    except Exception:
        pass
    return None
