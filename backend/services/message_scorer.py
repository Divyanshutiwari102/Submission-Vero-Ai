"""
services/message_scorer.py — Multi-candidate message scoring engine.

v3 upgrades:
- Micro-personalization: time anchors, delta comparisons ("3% → 2%"), predicted impact ("~N more calls")
- Enhanced scoring: bonus axes for comparisons / time references / predictions
- Penalty for vague verbs used without a number nearby ("improve", "boost", "optimize")
- Intelligent CTAs: low-friction, tell merchant exactly what happens after YES
- Human-like tone: conversational without being unprofessional
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ── Score axes ────────────────────────────────────────────────────────────────

@dataclass
class MessageScore:
    specificity: float = 0.0         # Numbers, %, named offers, dates
    urgency: float = 0.0             # Time pressure, scarcity, loss framing
    merchant_relevance: float = 0.0  # References merchant's own data
    category_alignment: float = 0.0  # Matches category voice/expectations
    actionability: float = 0.0       # Clear, low-friction next step
    comparison_bonus: float = 0.0    # "X → Y", "down from A to B", "vs peers"
    time_anchor_bonus: float = 0.0   # "this week", "today", "last 7 days"
    prediction_bonus: float = 0.0    # "~N more calls", "you may lose", "could mean"

    @property
    def total(self) -> float:
        return (
            self.specificity * 1.2 +
            self.urgency * 1.0 +
            self.merchant_relevance * 1.1 +
            self.category_alignment * 0.9 +
            self.actionability * 1.0 +
            self.comparison_bonus * 1.3 +   # highest weight — proves business intelligence
            self.time_anchor_bonus * 1.0 +
            self.prediction_bonus * 1.2
        )

    def __repr__(self):
        return (
            f"Score(total={self.total:.1f} | "
            f"spec={self.specificity:.1f} urg={self.urgency:.1f} "
            f"rel={self.merchant_relevance:.1f} cat={self.category_alignment:.1f} "
            f"act={self.actionability:.1f} cmp={self.comparison_bonus:.1f} "
            f"time={self.time_anchor_bonus:.1f} pred={self.prediction_bonus:.1f})"
        )


@dataclass
class ScoredMessage:
    body: str
    frame: str   # "loss_aversion" | "opportunity_gain" | "social_proof"
    score: MessageScore = field(default_factory=MessageScore)

    @property
    def total_score(self) -> float:
        return self.score.total


# ── Scoring patterns ──────────────────────────────────────────────────────────

_NUMBER_RE = re.compile(
    r"\b\d+[\.,]?\d*\s*(%|₹|k|km|hrs?|days?|min|patients?|reviews?|views|calls|stars?)\b", re.I
)
_NAMED_FIGURE_RE = re.compile(r"\b\d+\b")

_URGENCY_WORDS = {
    "today", "now", "abhi", "aaj", "tonight", "sirf", "only", "last chance",
    "before", "expires", "limited", "deadline", "window", "jaldi",
    "closing", "ends", "final", "urgent", "24h", "48h", "this week", "is hafte",
}

# Comparison: "3.0% → 2.1%", "down from 3% to 2%", "X vs Y with number"
_COMPARISON_RE = re.compile(
    r"(\d[\d.]*%?\s*(→|->|to|se)\s*\d[\d.]*%?)"
    r"|(\bdown from\b)"
    r"|(\bup from\b)"
    r"|(\bvs\b.*\d)"
    r"|(\bfrom\s+\d.*\bto\s+\d)",
    re.I
)

# Time anchors
_TIME_ANCHOR_RE = re.compile(
    r"\b(this week|last week|today|aaj|is hafte|pichle hafte|"
    r"last 7 days|last 30 days|last month|pichle 30 din|"
    r"abhi|right now|in the last|over the (past|last)|"
    r"yesterday|kal|this month|is mahine)\b",
    re.I
)

# Predictions / impact estimates
_PREDICTION_RE = re.compile(
    r"(~\d+\s*(more|extra|additional|calls|patients|bookings|views))"
    r"|(\bcould mean\b)"
    r"|(\byou may (lose|miss|gain)\b)"
    r"|(\bmeans.*\d+\s*(more|extra))"
    r"|(\bleaving.*table\b)"
    r"|(\bcost.*\d+\s*(calls|patients))",
    re.I
)

# Vague verbs — penalised when used without a nearby number
_VAGUE_VERBS_RE = re.compile(
    r"\b(improve|boost|optimize|optimise|enhance|increase|grow|scale|leverage|"
    r"badhana|sudharna|theek karna)\b",
    re.I
)

_HYPE_WORDS = ["amazing", "incredible", "best ever", "unbelievable", "wow", "superb", "fantastic"]

_CTA_PATTERNS = [
    r"reply\s+yes", r"reply\s+ha[anh]?\b", r"batao",
    r"karun\?", r"shall i\?", r"want me to", r"reply \w+",
    r"book a slot", r"confirm", r"\?$",
]


def score_message(body: str, merchant: dict, category: dict, trigger_kind: str) -> MessageScore:
    """Score a single message body across all axes including v3 bonuses."""
    body_lower = body.lower()
    s = MessageScore()

    # ── Specificity ──────────────────────────────────────────────────────
    unit_numbers = len(_NUMBER_RE.findall(body))
    bare_numbers = len(_NAMED_FIGURE_RE.findall(body))
    s.specificity = min(10.0, unit_numbers * 3.0 + bare_numbers * 0.5)

    owner = (merchant.get("identity", {}).get("owner_first_name") or "").lower()
    shop_name = (merchant.get("identity", {}).get("name") or "").lower()
    if owner and owner in body_lower:
        s.specificity += 1.5
    if shop_name and any(w in body_lower for w in shop_name.split() if len(w) > 3):
        s.specificity += 1.0

    offers = merchant.get("offers", [])
    for offer in offers[:3]:
        title = (offer.get("title") or "").lower()
        if title and any(w in body_lower for w in title.split() if len(w) > 3):
            s.specificity += 1.0
            break

    # Penalise vague verbs without nearby numbers
    for m in _VAGUE_VERBS_RE.finditer(body_lower):
        window = body_lower[max(0, m.start() - 60): min(len(body_lower), m.end() + 60)]
        if not re.search(r"\d", window):
            s.specificity = max(0.0, s.specificity - 2.0)

    s.specificity = min(10.0, s.specificity)

    # ── Urgency ──────────────────────────────────────────────────────────
    urgency_hits = sum(1 for w in _URGENCY_WORDS if w in body_lower)
    s.urgency = min(10.0, urgency_hits * 2.0)
    loss_phrases = ["dropped", "below", "falling", "losing", "gir", "kam ho", "behind", "miss", "neeche"]
    s.urgency += sum(1.5 for p in loss_phrases if p in body_lower)
    s.urgency = min(10.0, s.urgency)

    # ── Merchant relevance ────────────────────────────────────────────────
    perf = merchant.get("performance", {})
    views = perf.get("views", 0)
    ctr = perf.get("ctr", 0)
    signals = merchant.get("signals", [])

    if str(views) in body:
        s.merchant_relevance += 3.0
    if str(int(ctr * 100)) in body:
        s.merchant_relevance += 2.5
    for sig in signals[:3]:
        if sig.split(":")[0].replace("_", " ") in body_lower:
            s.merchant_relevance += 2.0
            break

    locality = (merchant.get("identity", {}).get("locality") or "").lower()
    city = (merchant.get("identity", {}).get("city") or "").lower()
    if locality and locality in body_lower:
        s.merchant_relevance += 2.0
    elif city and city in body_lower:
        s.merchant_relevance += 1.0
    s.merchant_relevance = min(10.0, s.merchant_relevance)

    # ── Category alignment ────────────────────────────────────────────────
    peer_stats = category.get("peer_stats", {})
    peer_ctr = peer_stats.get("avg_ctr", 0)
    if str(int(peer_ctr * 100)) in body:
        s.category_alignment += 3.0

    category_slug = category.get("slug", "").lower()
    CAT_KEYWORDS = {
        "dentists": ["patient", "recall", "fluoride", "cleaning", "cavity", "dental", "clinic"],
        "salons": ["appointment", "style", "color", "cut", "beauty", "hair", "booking"],
        "restaurants": ["table", "menu", "order", "delivery", "dine", "footfall", "cover"],
        "spas": ["session", "relax", "massage", "book", "slot", "wellness"],
        "gyms": ["member", "workout", "session", "slot", "fitness", "training"],
    }
    hits = sum(1 for k in CAT_KEYWORDS.get(category_slug, []) if k in body_lower)
    s.category_alignment += min(5.0, hits * 1.5)
    s.category_alignment -= sum(1.5 for h in _HYPE_WORDS if h in body_lower)
    s.category_alignment = min(10.0, max(0.0, s.category_alignment))

    # ── Actionability ─────────────────────────────────────────────────────
    for pattern in _CTA_PATTERNS:
        if re.search(pattern, body_lower):
            s.actionability += 4.0
            break
    if body.strip().endswith("?") or re.search(r"reply (yes|ha|han)\b", body_lower):
        s.actionability += 3.0
    # Bonus: CTA tells merchant what happens in a specific timeframe
    if re.search(r"(in \d+ min|right now|abhi|aaj|today|2 minute|3 min|tonight)", body_lower):
        s.actionability += 2.0
    q = body.count("?")
    if q == 1:
        s.actionability += 3.0
    elif q == 0:
        s.actionability += 1.5
    else:
        s.actionability = max(0.0, s.actionability - 2.0 * (q - 1))
    s.actionability = min(10.0, s.actionability)

    # ── v3: Comparison bonus ──────────────────────────────────────────────
    if _COMPARISON_RE.search(body):
        s.comparison_bonus = 6.0
    elif re.search(r"\bvs\b|\bversus\b", body_lower) and re.search(r"\d", body):
        s.comparison_bonus = 3.0

    # ── v3: Time anchor bonus ─────────────────────────────────────────────
    time_hits = len(_TIME_ANCHOR_RE.findall(body))
    s.time_anchor_bonus = min(6.0, time_hits * 3.0)

    # ── v3: Prediction bonus ──────────────────────────────────────────────
    if _PREDICTION_RE.search(body):
        s.prediction_bonus = 6.0

    return s


# ── CTA builder ───────────────────────────────────────────────────────────────

def build_smart_cta(trigger_kind: str, hi: bool) -> str:
    """
    Returns an intelligent, low-friction CTA that tells the merchant
    exactly what will happen in the next 2-3 minutes after saying YES.
    """
    CTA_EN = {
        "perf_dip":             "Reply YES — I'll push a fresh post in 2 mins.",
        "perf_spike":           "Reply YES — I'll activate a time-limited offer right now.",
        "competitor_opened":    "Reply YES — I'll sharpen your profile before traffic shifts.",
        "stale_posts":          "Reply YES — I'll publish a new post today.",
        "ctr_below_peer":       "Reply YES — I'll close the CTR gap with a profile refresh now.",
        "festival_upcoming":    "Reply YES — I'll have your campaign live before tonight.",
        "milestone_reached":    "Reply YES — I'll get a celebratory post live today.",
        "dormant_with_vera":    "Reply YES — quick 5-min audit, I'll flag the top 2 fixes.",
        "research_digest":      "Reply YES — I'll draft the patient WhatsApp in 3 mins.",
        "category_research_digest_release": "Reply YES — I'll draft the patient WhatsApp in 3 mins.",
        "recall_due":           "Reply YES or share a time — I'll confirm it right away.",
        "customer_lapsed_soft": "Reply YES — I'll send the re-engagement message now.",
        "review_theme_emerged": "Reply YES — I'll draft a response + profile note today.",
        "scheduled_recurring":  "Reply YES — I'll run through the top fixes right now.",
    }
    CTA_HI = {
        "perf_dip":             "Reply YES — main 2 minute mein fresh post push kar dungi.",
        "perf_spike":           "Reply YES — abhi time-limited offer activate karti hoon.",
        "competitor_opened":    "Reply YES — traffic shift se pehle profile sharpen kar dungi.",
        "stale_posts":          "Reply YES — aaj naya post publish kar dungi.",
        "ctr_below_peer":       "Reply YES — abhi CTR gap profile refresh se close kar dungi.",
        "festival_upcoming":    "Reply YES — aaj raat tak campaign live kar dungi.",
        "milestone_reached":    "Reply YES — celebratory post aaj live kar dungi.",
        "dormant_with_vera":    "Reply YES — 5 minute mein top 2 fixes bataungi.",
        "research_digest":      "Reply YES — 3 minute mein patient WhatsApp draft kar dungi.",
        "category_research_digest_release": "Reply YES — 3 minute mein patient WhatsApp draft kar dungi.",
        "recall_due":           "Reply YES ya time batao — slot abhi confirm kar leti hoon.",
        "customer_lapsed_soft": "Reply YES — abhi re-engagement message bhejti hoon.",
        "review_theme_emerged": "Reply YES — aaj response + profile note draft kar dungi.",
        "scheduled_recurring":  "Reply YES — abhi top fixes run karti hoon.",
    }
    mapping = CTA_HI if hi else CTA_EN
    default = "Reply YES — abhi shuru karti hoon." if hi else "Reply YES — I'll take care of it right now."
    return mapping.get(trigger_kind, default)


# ── Micro-personalization helpers ─────────────────────────────────────────────

def _predicted_calls_gain(views: int, ctr_current: float, ctr_target: float) -> int:
    return max(1, int(views * (ctr_target - ctr_current)))


def _ctr_arrow(ctr: float, peer_ctr: float) -> str:
    """e.g. 'peers 3.0% → yours 2.1%'"""
    return f"peers {peer_ctr*100:.1f}% → yours {ctr*100:.1f}%"


# ── Candidate builder ─────────────────────────────────────────────────────────

def build_candidates(
    trigger_kind: str,
    merchant: dict,
    category: dict,
    trigger: dict,
    customer: Optional[dict] = None,
) -> list[ScoredMessage]:
    """
    Build 2-3 psychologically distinct message candidates.
    Every candidate includes: time anchor + delta comparison + predicted impact + smart CTA.
    """
    identity = merchant.get("identity", {})
    owner = identity.get("owner_first_name", identity.get("name", "there"))
    languages = identity.get("languages", ["en"])
    hi = "hi" in languages

    perf = merchant.get("performance", {})
    views = perf.get("views", 0)
    ctr = perf.get("ctr", 0)
    calls = perf.get("calls", 0)
    delta_7d = perf.get("delta_7d", {})
    views_delta = delta_7d.get("views_pct", 0)

    peer_stats = category.get("peer_stats", {})
    peer_ctr = peer_stats.get("avg_ctr", 0)
    ctr_gap = round((peer_ctr - ctr) * 100, 1) if peer_ctr > ctr else 0.0
    predicted_gain = _predicted_calls_gain(views, ctr, peer_ctr)

    category_name = category.get("display_name", category.get("slug", ""))
    offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    active_offer = offers[0].get("title", "") if offers else ""
    locality = identity.get("locality", "your area")
    trigger_payload = trigger.get("payload", {})
    cta = build_smart_cta(trigger_kind, hi)

    candidates: list[ScoredMessage] = []

    # ── perf_dip ──────────────────────────────────────────────────────────
    if trigger_kind == "perf_dip":
        pct = abs(int(views_delta * 100)) if views_delta else 0

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, is hafte profile views {pct}% neeche aaye ({views} last 30 days). "
                        f"CTR {int(ctr*100)}% pe hai — peers {int(peer_ctr*100)}% pe hain, {ctr_gap}% ka gap. "
                        f"Agar yeh trend raha, ~{predicted_gain} calls/month miss hongi. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {views} views last 30 days hain lekin CTR sirf {int(ctr*100)}% — "
                        f"peers {int(peer_ctr*100)}% pe hain ({ctr_gap}% aage). "
                        f"Aaj ek fresh post se ~{predicted_gain} extra calls/month possible hai. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {locality} ke {category_name}s is hafte {int(peer_ctr*100)}% CTR hold kar rahe hain. "
                        f"Aapka {int(ctr*100)}% — {ctr_gap}% gap. Ek post aaj se yeh band hoga. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, views are down {pct}% this week ({views} last 30 days). "
                        f"CTR at {int(ctr*100)}% vs peer {int(peer_ctr*100)}% — {ctr_gap}% gap. "
                        f"At this rate, ~{predicted_gain} calls/month are at risk. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, you have {views} views/month but CTR is {int(ctr*100)}% — peers are at {int(peer_ctr*100)}%. "
                        f"Closing that {ctr_gap}% gap means ~{predicted_gain} more calls/month from the same traffic. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, top {category_name}s in {locality} are holding {int(peer_ctr*100)}% CTR this week. "
                        f"You're at {int(ctr*100)}% — one post refresh today puts you back in that group. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]

    # ── perf_spike ────────────────────────────────────────────────────────
    elif trigger_kind == "perf_spike":
        pct = int(views_delta * 100) if views_delta else 0
        extra_calls = max(1, int(views * peer_ctr * 0.3))

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, is hafte {pct}% zyada views aa rahe hain — {views} last 30 days. "
                        f"Yeh traffic window usually 3-4 din rehti hai. "
                        f"Time-limited offer abhi push karo toh ~{extra_calls} extra calls possible hain. "
                        f"{cta}"
                    ),
                    frame="urgency"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {pct}% traffic spike this week — {views} views. "
                        f"Peer CTR {int(peer_ctr*100)}% pe pohoncho toh ~{extra_calls} calls/month possible. "
                        f"Yeh momentum {locality} mein top 20% performance hai abhi. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {locality} ke {category_name}s traffic spikes pe time-limited offers push karte hain. "
                        f"Aapka traffic {pct}% upar hai this week ({views} views) — same playbook run karo? "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, you're up {pct}% in views this week ({views} last 30 days). "
                        f"These spikes last 3-4 days — a time-limited offer now could mean ~{extra_calls} extra calls. "
                        f"{cta}"
                    ),
                    frame="urgency"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {pct}% traffic spike this week → {views} views. "
                        f"At peer CTR {int(peer_ctr*100)}%, this window alone = ~{extra_calls} more calls. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, best-performing {category_name}s in {locality} always push offers during traffic spikes. "
                        f"You're up {pct}% this week — {views} views. This is exactly that moment. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]

    # ── competitor_opened ─────────────────────────────────────────────────
    elif trigger_kind == "competitor_opened":
        dist = trigger_payload.get("distance_km", "nearby")

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, aaj {dist}km pe ek naya {category_name} khula hai. "
                        f"Aapka CTR {int(ctr*100)}% — peer median {int(peer_ctr*100)}% se {ctr_gap}% peeche. "
                        f"Traffic shift hone se pehle yeh gap close karna zaroori hai. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, naya competitor {dist}km door — aaj khula. "
                        f"{locality} ke top {category_name}s {int(peer_ctr*100)}% CTR aise hold karte hain: "
                        f"fresh offers + active posts. Aapko unhi standards pe laata hoon. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, a new {category_name} opened {dist}km from you — today. "
                        f"Your CTR is {int(ctr*100)}% vs peer median {int(peer_ctr*100)}% — {ctr_gap}% gap. "
                        f"Getting ahead of this now is much cheaper than recovering later. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, new competition {dist}km away, opened today. "
                        f"Top {category_name}s in {locality} defend CTR at {int(peer_ctr*100)}% with fresh offers + active posts. "
                        f"Want me to bring you to that standard? "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]

    # ── milestone_reached ─────────────────────────────────────────────────
    elif trigger_kind == "milestone_reached":
        milestone = trigger_payload.get("milestone", "a milestone")

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, badhaai — {milestone} achieve kiya! "
                        f"Yeh attention spike 2-3 din rehta hai. "
                        f"Celebratory post aaj push karo — momentum high hai abhi. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {milestone} — {locality} ke {category_name}s mein top tier. "
                        f"Ek celebratory post + limited offer aaj conversions capture kar sakta hai. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, congratulations — {milestone} hit today! "
                        f"Attention spikes like this last 2-3 days. A celebratory post now locks in the momentum. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {milestone} puts you in the top tier of {category_name}s in {locality}. "
                        f"A celebratory post + limited offer today can convert that attention into bookings. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]

    # ── festival_upcoming ─────────────────────────────────────────────────
    elif trigger_kind == "festival_upcoming":
        festival = trigger_payload.get("festival_name", "upcoming festival")
        days = trigger_payload.get("days_away", "a few")

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, {festival} mein sirf {days} din baaki hain. "
                        f"{locality} ke competitors aaj already campaigns live kar chuke hain. "
                        f"{category_name}s festive window mein avg 30-35% zyada views dekhte hain. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {festival} {days} din door hai — festive traffic peak pe "
                        f"{category_name}s 30-35% more views milte hain. "
                        f"Aaj campaign set karo toh early bookers aapke paas aayenge. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, {festival} is {days} days away. "
                        f"Competitors in {locality} already have campaigns live today — "
                        f"{category_name}s typically see 30-35% more views during this window. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {festival} traffic peaks in {days} days — {category_name}s get 30-35% more views. "
                        f"Setting up today captures early bookers before competitors lock them in. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]

    # ── stale_posts / ctr_below_peer / scheduled_recurring ────────────────
    elif trigger_kind in ("stale_posts", "ctr_below_peer", "scheduled_recurring"):
        signals = merchant.get("signals", [])
        days_stale = next(
            (s.split(":")[1] for s in signals if s.startswith("stale_posts")), "21"
        )

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, last post {days_stale} din purana hai aur CTR {int(ctr*100)}% pe hai — "
                        f"peers {int(peer_ctr*100)}% pe hain. Yeh {ctr_gap}% gap ~{predicted_gain} calls/month ka difference hai. "
                        f"Aaj ek fresh post se yeh close ho sakta hai. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {locality} ke active {category_name}s weekly post karte hain — "
                        f"aur {int(peer_ctr*100)}% CTR hold karte hain. "
                        f"Aapka last post {days_stale} din purana hai, CTR {int(ctr*100)}% pe atak gayi. "
                        f"Ek update aaj — wapas peer level pe. {cta}"
                    ),
                    frame="social_proof"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, last post is {days_stale} days old — CTR at {int(ctr*100)}% vs peer {int(peer_ctr*100)}%. "
                        f"That {ctr_gap}% gap costs ~{predicted_gain} calls/month. One post today closes it. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {category_name}s in {locality} that post weekly hold {int(peer_ctr*100)}% CTR. "
                        f"Your last post was {days_stale} days ago — CTR slipped to {int(ctr*100)}%. "
                        f"One update today gets you back to their level. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
            ]

    # ── recall_due / customer_lapsed_soft ─────────────────────────────────
    elif trigger_kind in ("recall_due", "customer_lapsed_soft") and customer:
        cust_identity = customer.get("identity", {})
        cust_name = cust_identity.get("name", "there")
        relationship = customer.get("relationship", {})
        services = relationship.get("services_received", [])
        last_service = services[-1] if services else "your last visit"
        months_since = relationship.get("months_since_visit", 6)
        prefs = customer.get("preferences", {}).get("preferred_slots", "")
        merchant_name = identity.get("name", "")
        slot_hint_hi = " Shaam ka slot abhi khula hai." if "evening" in prefs else ""
        slot_hint_en = " Evening slots are open right now." if "evening" in prefs else ""

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"Hi {cust_name}, {merchant_name} ki taraf se. "
                        f"{last_service} ke baad {months_since} mahine ho gaye — recall due hai. "
                        f"Regular check-up miss karna chhoti problems ko badha sakta hai.{slot_hint_hi} "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"Hi {cust_name}, {merchant_name} yahan se. "
                        f"{months_since} mahine ho gaye {last_service} ke baad — sirf 30 min ka quick recall.{slot_hint_hi} "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"Hi {cust_name}, {merchant_name} here. "
                        f"It's been {months_since} months since your {last_service} — your recall is due. "
                        f"Skipping it can let small issues grow.{slot_hint_en} "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"Hi {cust_name}, {merchant_name} here. "
                        f"{months_since} months since your {last_service} — just a 30-min recall.{slot_hint_en} "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]

    # ── dormant_with_vera ─────────────────────────────────────────────────
    elif trigger_kind == "dormant_with_vera":
        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, kuch time ho gaya — check-in karna tha. "
                        f"Last 30 days: {views} views, lekin CTR {int(ctr*100)}% pe ruk gayi — "
                        f"peers {int(peer_ctr*100)}% pe hain. ~{predicted_gain} calls/month table pe chhoot rahi hain. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {views} views last 30 days — solid base. "
                        f"Sirf CTR {int(ctr*100)}% se {int(peer_ctr*100)}% tak laana hai — ~{predicted_gain} extra calls/month milenge. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, checking in — been a while. "
                        f"Last 30 days: {views} views, but CTR at {int(ctr*100)}% vs peer {int(peer_ctr*100)}%. "
                        f"~{predicted_gain} calls/month are being left on the table. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {views} views last 30 days — solid traffic. "
                        f"Getting CTR from {int(ctr*100)}% to peer level {int(peer_ctr*100)}% = ~{predicted_gain} more calls/month. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]

    # ── research_digest ───────────────────────────────────────────────────
    elif trigger_kind in ("research_digest", "category_research_digest_release"):
        digest = category.get("digest", [{}])
        item = digest[0] if digest else {}
        title = item.get("title", f"new {category_name} research")
        source = item.get("source", "")
        trial_n = item.get("trial_n", "")
        n_str = f"{trial_n}-patient study" if trial_n else "recent study"
        src_str = f" ({source})" if source else ""

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, ek important {n_str} aaya: \"{title}\"{src_str}. "
                        f"{locality} ke active {category_name}s isko patient trust build karne ke liye use karte hain. "
                        f"Aapke patients bhi yeh pooch sakte hain — ready rehna chahiye. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, {n_str}: \"{title}\"{src_str}. "
                        f"Patients jo yeh poochhen unke liye answer taiyaar ho — "
                        f"yeh trust aur revisits dono badhata hai. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, a {n_str} just dropped: \"{title}\"{src_str}. "
                        f"Top {category_name}s in {locality} share updates like this to build patient trust and drive revisits. "
                        f"{cta}"
                    ),
                    frame="social_proof"
                ),
                ScoredMessage(
                    body=(
                        f"{owner}, new {n_str}: \"{title}\"{src_str}. "
                        f"Patients asking about this will remember you answered it first — trust and repeat visits both go up. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]

    # ── review_theme_emerged ──────────────────────────────────────────────
    elif trigger_kind == "review_theme_emerged":
        theme = trigger_payload.get("theme", "wait time")
        count = trigger_payload.get("count", 3)

        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, last week {count} reviews mein ek common theme tha: '{theme}'. "
                        f"Yeh Google rating ko affect kar sakta hai agle 30 dino mein. "
                        f"Aaj response + profile note se damage control ho sakta hai. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, {count} reviews this week flagged a common theme: '{theme}'. "
                        f"Left unaddressed, this can hurt your Google rating over the next 30 days. "
                        f"A response + profile note today gets ahead of it. "
                        f"{cta}"
                    ),
                    frame="loss_aversion"
                ),
            ]

    # ── generic fallback ──────────────────────────────────────────────────
    else:
        if hi:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, last 30 days: {views} views — lekin CTR {int(ctr*100)}% pe hai, "
                        f"peers {int(peer_ctr*100)}% pe hain. "
                        f"~{predicted_gain} calls/month ka gap hai jo ek update se close ho sakta hai aaj. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]
        else:
            candidates = [
                ScoredMessage(
                    body=(
                        f"{owner}, last 30 days: {views} views — but CTR at {int(ctr*100)}% vs peer {int(peer_ctr*100)}%. "
                        f"~{predicted_gain} calls/month gap a single update closes today. "
                        f"{cta}"
                    ),
                    frame="opportunity_gain"
                ),
            ]

    return candidates


def select_best_message(
    candidates: list[ScoredMessage],
    merchant: dict,
    category: dict,
    trigger_kind: str,
) -> ScoredMessage:
    """Score all candidates and return the highest-scoring one."""
    for cand in candidates:
        cand.score = score_message(cand.body, merchant, category, trigger_kind)
    return max(candidates, key=lambda c: c.total_score)


def get_best_candidate(
    trigger_kind: str,
    merchant: dict,
    category: dict,
    trigger: dict,
    customer: dict | None = None,
) -> ScoredMessage:
    """End-to-end: build → score → return winner."""
    candidates = build_candidates(trigger_kind, merchant, category, trigger, customer)
    return select_best_message(candidates, merchant, category, trigger_kind)
