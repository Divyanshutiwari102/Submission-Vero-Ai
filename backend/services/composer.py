"""
services/composer.py — Core message composition engine.

Takes the 4 context layers (category, merchant, trigger, customer?)
and returns a composed WhatsApp message using Claude as the LLM backbone.

UPGRADES vs v1:
- Message scoring engine: 2-3 candidates generated, scored, best selected
- Enhanced system prompt: forces specificity, numbers, category voice
- Stronger fallback: uses scored candidates (no generic text ever)
- Suppression-aware: time-based + trigger-level dedup
- Category-specific tone injection
- Never fails: fallback always produces a high-quality message

Design principles:
- Deterministic (temperature=0)
- Context-aware (all 4 layers injected)
- Voice-matched (peer/clinical/retail per category)
- Anti-hallucination (strict "use only given data" instruction)
- Fast (< 25s per call, leaving buffer for the 30s judge timeout)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

import httpx

from utils.store import store
from services.message_scorer import (
    get_best_candidate,
    score_message,
    build_candidates,
    select_best_message,
)

# ── Constants ─────────────────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1200  # slightly more room for richer messages

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

# ── Trigger routing — maps trigger kind → prompt variant ──────────────────────

TRIGGER_VARIANT_MAP = {
    "research_digest": "research",
    "category_research_digest_release": "research",
    "regulation_change": "research",
    "category_trend_movement": "trend",
    "festival_upcoming": "festival",
    "weather_heatwave": "weather",
    "local_news_event": "local_news",
    "competitor_opened": "competitor",
    "perf_spike": "perf_positive",
    "perf_dip": "perf_negative",
    "milestone_reached": "milestone",
    "dormant_with_vera": "re_engagement",
    "customer_lapsed_soft": "recall",
    "recall_due": "recall",
    "appointment_tomorrow": "appointment",
    "review_theme_emerged": "review",
    "scheduled_recurring": "curiosity",
    "stale_posts": "profile",
    "ctr_below_peer": "profile",
}

# ── CTA routing ───────────────────────────────────────────────────────────────

BINARY_CTA_TRIGGERS = {
    "perf_spike", "perf_dip", "milestone_reached", "dormant_with_vera",
    "festival_upcoming", "competitor_opened", "stale_posts", "ctr_below_peer",
    "customer_lapsed_soft", "recall_due", "appointment_tomorrow",
}

NO_CTA_TRIGGERS = {"regulation_change"}

# ── Category-specific voice injection ─────────────────────────────────────────

CATEGORY_VOICE_HINTS = {
    "dentists": (
        "Tone: peer-clinical. Speak doctor-to-doctor or clinic-to-patient. "
        "Use clinical specificity: recall intervals, treatment names, study citations. "
        "Never use sales hype. Trust and expertise anchor every message."
    ),
    "salons": (
        "Tone: warm, friendly, aspirational. Use style vocabulary: cut, color, treatment, look. "
        "Focus on seasonal trends, appointment availability, and social validation. "
        "Emoji are welcome if used sparingly (1 max)."
    ),
    "restaurants": (
        "Tone: local, community-focused. Emphasise footfall, covers, table availability. "
        "Reference food occasions, delivery windows, and neighbourhood context. "
        "Use urgency around peak meal times when relevant."
    ),
    "spas": (
        "Tone: calm, wellness-oriented. Emphasise relaxation, time, and self-care. "
        "Slot availability and exclusivity are strong levers. Avoid hard-sell."
    ),
    "gyms": (
        "Tone: motivational but data-grounded. Use membership counts, session slots, "
        "and transformation proof points. Early-morning/off-peak urgency works well."
    ),
}

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Vera, magicpin's merchant AI assistant.
You compose short, HIGH-IMPACT WhatsApp messages for merchant partners and their customers.

CRITICAL RULES:
1. Use ONLY data provided in the context JSON. Never invent facts, numbers, names, or citations.
2. ALWAYS anchor on at least 2 concrete numbers from context (views, CTR%, peer stats, days, ₹ amounts).
3. Match the merchant's language preference (hi-en mix if they speak Hindi+English).
4. Match the CATEGORY VOICE HINT provided — it overrides generic tone defaults.
5. Single CTA only — binary YES/STOP for action triggers, open-ended for info triggers.
6. No promotional hype. No "AMAZING DEAL!" No long preambles.
7. Keep the message concise — 3-5 sentences max for WhatsApp.
8. The CTA must be the LAST sentence.
9. Never re-introduce yourself after the first message.
10. Never fabricate competitor names, paper citations, or stats.
11. SPECIFICITY OVER VAGUENESS: "your CTR dropped 8% this week (18 → 16 calls)" beats "your performance dipped".
12. Use ONE of these psychological frames: loss_aversion | opportunity_gain | social_proof.
    Pick whichever the EXAMPLE CANDIDATE demonstrates — follow that frame.

OUTPUT FORMAT — return ONLY a JSON object, no preamble, no markdown:
{
  "body": "<the WhatsApp message>",
  "cta": "<open_ended|binary_yes_stop|none>",
  "send_as": "<vera|merchant_on_behalf>",
  "suppression_key": "<from trigger or derived>",
  "rationale": "<1-2 sentences: why this message, what it achieves>",
  "frame": "<loss_aversion|opportunity_gain|social_proof>"
}"""


def _get_category_voice_hint(category: dict) -> str:
    slug = category.get("slug", "").lower()
    hint = CATEGORY_VOICE_HINTS.get(slug)
    if hint:
        return hint
    # Derive from category voice object if available
    voice = category.get("voice", {})
    tone = voice.get("tone", "")
    if "clinical" in tone or "peer" in tone:
        return "Tone: peer-clinical. Prioritise clinical precision and trust over commercial language."
    if "warm" in tone or "friendly" in tone:
        return "Tone: warm and friendly. Build rapport before the ask."
    return "Tone: professional, specific, concise. Data-anchored."


# ── Deterministic opening-style hints (keyed on trigger kind, no randomness) ─

_OPENING_HINT_EN = {
    "perf_dip":             "Quick heads-up {name} —",
    "perf_spike":           "{name}, noticing a pattern —",
    "competitor_opened":    "{name}, heads-up —",
    "milestone_reached":    "{name}, great news —",
    "festival_upcoming":    "{name}, timing alert —",
    "stale_posts":          "{name}, noticing a pattern —",
    "ctr_below_peer":       "{name}, quick data point —",
    "dormant_with_vera":    "Hey {name}, checking in —",
    "research_digest":      "{name}, something relevant just dropped —",
    "category_research_digest_release": "{name}, something relevant just dropped —",
    "recall_due":           "Hi {name},",
    "customer_lapsed_soft": "Hi {name},",
    "review_theme_emerged": "{name}, flagging something —",
    "scheduled_recurring":  "Quick heads-up {name} —",
}

_OPENING_HINT_HI = {
    "perf_dip":             "Quick update {name} —",
    "perf_spike":           "{name}, ek pattern dikh raha hai —",
    "competitor_opened":    "{name}, ek important update —",
    "milestone_reached":    "{name}, badhaai —",
    "festival_upcoming":    "{name}, timing important hai —",
    "stale_posts":          "{name}, ek pattern dikh raha hai —",
    "ctr_below_peer":       "{name}, ek quick data point —",
    "dormant_with_vera":    "Hey {name}, check-in karna tha —",
    "research_digest":      "{name}, kuch relevant aaya hai —",
    "category_research_digest_release": "{name}, kuch relevant aaya hai —",
    "recall_due":           "Hi {name},",
    "customer_lapsed_soft": "Hi {name},",
    "review_theme_emerged": "{name}, kuch flag karna tha —",
    "scheduled_recurring":  "Quick update {name} —",
}


def _get_opening_hint(trigger_kind: str, owner_name: str, hi: bool) -> str:
    """Return a deterministic opening phrase based on trigger kind and language."""
    mapping = _OPENING_HINT_HI if hi else _OPENING_HINT_EN
    template = mapping.get(trigger_kind, "{name},")
    return template.format(name=owner_name)


def _build_prompt(
    trigger_kind: str,
    variant: str,
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
    example_candidate: Optional[str] = None,
) -> str:
    """Build the user-turn prompt with all 4 context layers embedded."""

    merchant_name = merchant.get("identity", {}).get("name", "Merchant")
    owner_name = merchant.get("identity", {}).get("owner_first_name", merchant_name)
    languages = merchant.get("identity", {}).get("languages", ["en"])
    hi = "hi" in languages
    lang_note = "Use Hindi-English code-mix naturally (like: 'Aapke liye best option hai...')" \
        if hi else "Use English."
    category_slug = category.get("slug", "")
    voice_hint = _get_category_voice_hint(category)
    opening_hint = _get_opening_hint(trigger_kind, owner_name, hi)

    cta_hint = "none" if trigger_kind in NO_CTA_TRIGGERS else \
               "binary_yes_stop" if trigger_kind in BINARY_CTA_TRIGGERS else "open_ended"

    send_as = "merchant_on_behalf" if customer else "vera"
    audience = f"Customer: {customer.get('identity',{}).get('name','Customer')}" if customer else \
               f"Merchant: {owner_name}"

    perf = merchant.get("performance", {})
    peer_stats = category.get("peer_stats", {})

    parts = [
        f"COMPOSE a WhatsApp message for the following scenario.\n",
        f"Opening style (use this exact phrase to start the message): \"{opening_hint}\"",
        f"Variant: {variant.upper().replace('_', ' ')}",
        f"Audience: {audience}",
        f"Language: {lang_note}",
        f"CTA hint: {cta_hint}",
        f"Category voice: {voice_hint}",
        f"\n--- KEY METRICS (USE THESE NUMBERS) ---",
        json.dumps({
            "merchant_views_30d": perf.get("views", 0),
            "merchant_ctr": f"{int(perf.get('ctr', 0)*100)}%",
            "peer_ctr": f"{int(peer_stats.get('avg_ctr', 0)*100)}%",
            "ctr_gap_pct": round((peer_stats.get("avg_ctr", 0) - perf.get("ctr", 0)) * 100, 1),
            "views_delta_7d": f"{int(perf.get('delta_7d', {}).get('views_pct', 0)*100)}%",
        }, ensure_ascii=False),
        f"\n--- CATEGORY CONTEXT ({category_slug}) ---",
        json.dumps({
            "voice": category.get("voice", {}),
            "offer_catalog": category.get("offer_catalog", [])[:4],
            "peer_stats": peer_stats,
            "digest": category.get("digest", [])[:3],
            "seasonal_beats": category.get("seasonal_beats", [])[:2],
            "trend_signals": category.get("trend_signals", [])[:2],
        }, ensure_ascii=False, indent=2),
        f"\n--- MERCHANT CONTEXT ---",
        json.dumps({
            "merchant_id": merchant.get("merchant_id"),
            "identity": merchant.get("identity"),
            "subscription": merchant.get("subscription"),
            "performance": perf,
            "offers": merchant.get("offers", [])[:4],
            "signals": merchant.get("signals", []),
            "customer_aggregate": merchant.get("customer_aggregate"),
            "conversation_history": merchant.get("conversation_history", [])[-2:],
        }, ensure_ascii=False, indent=2),
        f"\n--- TRIGGER CONTEXT ---",
        json.dumps(trigger, ensure_ascii=False, indent=2),
    ]

    if customer:
        parts += [
            f"\n--- CUSTOMER CONTEXT ---",
            json.dumps(customer, ensure_ascii=False, indent=2),
        ]

    if example_candidate:
        parts += [
            f"\n--- EXAMPLE CANDIDATE (improve/elevate this, keep the frame) ---",
            example_candidate,
        ]

    parts.append(
        "\n\nNow compose the BEST possible message. "
        "Anchor on at least 2 specific numbers. "
        "Return ONLY the JSON object as specified. "
        "Do not add markdown, preamble, or explanation outside the JSON."
    )

    return "\n".join(parts)


async def compose_message(
    trigger_id: str,
    merchant_id: str,
    customer_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Main composition function.
    Returns a dict with: body, cta, send_as, suppression_key, rationale
    Returns None if any required context is missing.
    """
    trigger = store.get_trigger_payload(trigger_id)
    if not trigger:
        return None

    merchant = store.get_merchant_payload(merchant_id)
    if not merchant:
        return None

    category_slug = merchant.get("category_slug", "")
    category = store.get_category_payload(category_slug)
    if not category:
        return None

    customer = store.get_customer_payload(customer_id) if customer_id else None

    trigger_kind = trigger.get("kind", "scheduled_recurring")
    variant = TRIGGER_VARIANT_MAP.get(trigger_kind, "general")

    # ── Pre-generate scored fallback candidates ───────────────────────────
    # This gives us: (a) an example to seed Claude with, (b) a ready fallback
    try:
        scored_winner = get_best_candidate(trigger_kind, merchant, category, trigger, customer)
        example_body = scored_winner.body
    except Exception:
        scored_winner = None
        example_body = None

    # ── Build prompt and call Claude ──────────────────────────────────────
    prompt = _build_prompt(
        trigger_kind, variant, category, merchant, trigger, customer,
        example_candidate=example_body
    )

    result = await _call_claude(prompt)

    if not result and scored_winner:
        # Fallback: use the pre-scored winner
        result = _scored_candidate_to_result(scored_winner, trigger, merchant, trigger_kind, customer)
    elif not result:
        # Last resort: deterministic rule-based
        result = _rule_based_compose(trigger, merchant, category, customer)

    if result:
        # ── Post-process: ensure suppression_key is set ───────────────────
        if "suppression_key" not in result or not result["suppression_key"]:
            result["suppression_key"] = trigger.get("suppression_key", f"{trigger_id}:{merchant_id}")

        # ── Quality gate: if Claude returned a generic body, swap in scored winner ──
        if scored_winner and _is_generic(result.get("body", "")):
            result["body"] = scored_winner.body
            result["frame"] = scored_winner.frame
            result["rationale"] = f"Scored winner ({scored_winner.frame}, score={scored_winner.total_score:.1f})"

    return result


def _scored_candidate_to_result(winner, trigger: dict, merchant: dict, trigger_kind: str, customer) -> dict:
    """Convert a ScoredMessage into the standard result dict."""
    cta = "none" if trigger_kind in NO_CTA_TRIGGERS else \
          "binary_yes_stop" if trigger_kind in BINARY_CTA_TRIGGERS else "open_ended"
    send_as = "merchant_on_behalf" if customer else "vera"
    return {
        "body": winner.body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": f"Scored fallback: {winner.frame} frame, score={winner.total_score:.1f}",
        "frame": winner.frame,
    }


def _is_generic(body: str) -> bool:
    """Return True if a message body is too generic to be useful."""
    generic_markers = [
        "there's an update worth your attention",
        "kya main details share karun",
        "want me to share details",
        "ek update available hai",
    ]
    body_lower = body.lower()
    return any(m in body_lower for m in generic_markers)


async def _call_claude(prompt: str) -> Optional[dict]:
    """Call Claude API and parse JSON response."""
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
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
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if resp.status_code != 200:
            return None

        data = resp.json()
        raw = "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        )

        return _parse_json_response(raw)
    except Exception:
        return None


def _parse_json_response(raw: str) -> Optional[dict]:
    """Extract and parse JSON from Claude's response."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
        if all(k in parsed for k in ("body", "cta", "send_as", "rationale")):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _rule_based_compose(trigger: dict, merchant: dict, category: dict,
                        customer: Optional[dict]) -> dict:
    """
    Deterministic fallback when Claude API is unavailable AND scorer fails.
    Uses the message_scorer to generate and select the best candidate.
    This ensures the fallback is NEVER generic.
    """
    trigger_kind = trigger.get("kind", "scheduled_recurring")

    try:
        winner = get_best_candidate(trigger_kind, merchant, category, trigger, customer)
        return _scored_candidate_to_result(winner, trigger, merchant, trigger_kind, customer)
    except Exception:
        pass

    # Absolute last resort — still specific
    identity = merchant.get("identity", {})
    owner = identity.get("owner_first_name", identity.get("name", "there"))
    languages = identity.get("languages", ["en"])
    hi = "hi" in languages
    perf = merchant.get("performance", {})
    views = perf.get("views", 0)
    ctr = int(perf.get("ctr", 0) * 100)
    peer_ctr = int(category.get("peer_stats", {}).get("avg_ctr", 0) * 100)
    category_name = category.get("display_name", category.get("slug", ""))

    if hi:
        body = (f"{owner}, aapke profile pe {views} views last 30 days aaye hain. "
                f"CTR {ctr}% hai — peer median {peer_ctr}% se neeche. "
                f"Ek quick update se yeh gap close hoga. Karun? Reply YES.")
    else:
        body = (f"{owner}, your profile pulled {views} views last 30 days. "
                f"CTR at {ctr}% — {peer_ctr - ctr}% below peer median {peer_ctr}%. "
                f"A quick update closes that gap. Shall I? Reply YES.")

    cta = "binary_yes_stop" if trigger_kind in BINARY_CTA_TRIGGERS else "open_ended"
    return {
        "body": body,
        "cta": cta,
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", f"{trigger_kind}:{merchant.get('merchant_id','')}"),
        "rationale": "Absolute fallback — specific numbers always included",
        "frame": "loss_aversion",
    }
