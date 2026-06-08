"""Sales-aware relevance scoring for news items.

Replaces the old ``recency.rank_news`` (which sorted on category + date only) with
a scorer that combines five signals a human SDR would weigh:

1. **Category baseline** — funding > acquisition > launch > award > press > blog.
2. **Recency** — linear decay over ~90 days, so a fresh item outranks an old one
   in the same category.
3. **Buying-signal language** — phrases like "raises $X", "Series B", "acquires",
   "appoints CTO", "expands into…". These are what SDRs actually open emails with.
4. **Sender-offering overlap** — keyword intersection between the item text and
   the sender profile's KB. A funding round at a payments company is gold for a
   fintech-engineering seller, less so for a charity-sector consultancy.
5. **Recipient-role mention** — a small boost if the item names the recipient's
   role (CTO hire matters more if you're selling to a CTO).

Weights were tuned so that a fresh, sender-relevant funding announcement scores
~12-15 while a 60-day-old generic blog post scores ~0-2. Use ``score_relevance``
directly if you need to debug ranking; most callers just use ``rank_news``.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .schemas import NewsItem

_CATEGORY_WEIGHT = {
    "funding": 5.0,
    "award": 4.0,
    "launch": 4.0,
    "press": 3.0,
    "news": 2.0,
    "blog": 1.0,
    "other": 0.0,
    "profit":5.0,
    "growth":5.0,
    "partnership":4.0,
    "product":4.0,
    "merger":5.0,
    "acquisition":5.0
}

# Headline patterns SDRs actually open with — strong "the company has money or
# intent" signals. Each matched pattern adds a fixed bonus (capped).
_STRONG_SIGNAL_PATTERNS = (
    # Funding rounds with explicit amounts: "raises $40M", "secured $5 million"
    re.compile(
        r"\b(?:raises?|raised|secures?|secured|closes?|closed)\s+(?:a\s+)?\$?[\d.]+\s*(?:k|m|b|million|billion)\b",
        re.IGNORECASE,
    ),
    # Funding rounds named: "Series A/B/C/Seed"
    re.compile(r"\b(?:series\s+[a-k]|seed|pre-?seed)\s+(?:round|funding|raise|investment)\b", re.IGNORECASE),
    # M&A
    re.compile(r"\b(?:acquires?|acquired|acquisition|merges?|merger\s+with)\b", re.IGNORECASE),
    # Leadership hires
    re.compile(
        r"\b(?:appoints?|appointed|names?|named|hires?|hired|welcomes?)\s+"
        r"(?:new\s+|the\s+)?(?:c[eofnti]o|chief|president|vp|head\s+of|director)\b",
        re.IGNORECASE,
    ),
    # New product launches
    re.compile(r"\b(?:launches?|launched|unveils?|unveiled|introduces?|introduced)\s+(?:new|a\s+new|its)\b", re.IGNORECASE),
    # Geographic / market expansion
    re.compile(r"\b(?:expands?|expanded|expansion|enters?|entering|enters?\s+into)\s+(?:to|into|in|new)\b", re.IGNORECASE),
    # Strategic partnerships
    re.compile(r"\b(?:partners?|partnership|alliance|collaborat\w+)\s+with\b", re.IGNORECASE),
    # Major contracts
    re.compile(r"\b(?:wins?|won|signs?|signed|awarded)\s+(?:\$[\d.]+\s*(?:k|m|b)|major|landmark|contract|deal)\b", re.IGNORECASE),
)

# Short function words we don't care about when keyword-matching the sender KB.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "these", "those",
    "you", "your", "our", "their", "have", "has", "had", "will", "would",
    "are", "was", "were", "been", "being", "they", "them", "its", "but",
    "not", "all", "any", "can", "who", "what", "when", "where", "why",
    "how", "into", "than", "such", "via",
})


def _tokens(text: str) -> set[str]:
    """Lowercased word tokens, min 3 chars, with stopwords removed."""
    if not text:
        return set()
    return {
        t for t in re.findall(r"[a-z][a-z\-]{2,}", text.lower())
        if t not in _STOPWORDS
    }


def _extract_keywords(text: str, max_n: int = 40) -> set[str]:
    """Top N distinct content tokens from sender KB. Order doesn't matter for set use,
    but capping the count keeps the overlap signal honest — a 5000-word KB shouldn't
    auto-match almost any news item.
    """
    toks = _tokens(text)
    if len(toks) <= max_n:
        return toks
    # Stable subset: take first N as they appear (re-tokenize ordered).
    if not text:
        return set()
    seen: set[str] = set()
    out: list[str] = []
    for t in re.findall(r"[a-z][a-z\-]{2,}", text.lower()):
        if t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_n:
            break
    return set(out)


def _recency_factor(item_date: date, today: Optional[date] = None, horizon_days: int = 90) -> float:
    """1.0 at today, decays linearly to 0.0 at ``horizon_days`` old."""
    today = today or date.today()
    age = (today - item_date).days
    if age < 0:  # future-dated item — treat as fresh
        return 1.0
    return max(0.0, 1.0 - (age / horizon_days))


def _count_strong_signals(text: str) -> int:
    return sum(1 for p in _STRONG_SIGNAL_PATTERNS if p.search(text))


def _role_synonyms(role: str) -> set[str]:
    """Map common role strings to acronyms an article might use. 'Chief Technology
    Officer' → {'cto', 'chief technology officer'}. Imperfect; covers common cases."""
    role = role.strip().lower()
    syns = {role}
    aliases = {
        "chief executive officer": "ceo",
        "chief technology officer": "cto",
        "chief operating officer": "coo",
        "chief financial officer": "cfo",
        "chief marketing officer": "cmo",
        "chief product officer": "cpo",
        "chief revenue officer": "cro",
        "chief information officer": "cio",
        "vice president": "vp",
    }
    if role in aliases:
        syns.add(aliases[role])
    # Reverse: if they typed an acronym, add the full phrase.
    rev = {v: k for k, v in aliases.items()}
    if role in rev:
        syns.add(rev[role])
    return {s for s in syns if len(s) >= 3}


def score_relevance(
    item: NewsItem,
    *,
    sender_keywords: Optional[set[str]] = None,
    recipient_role: Optional[str] = None,
    today: Optional[date] = None,
) -> float:
    """Single relevance score combining category, recency, signal language, sender
    overlap, and recipient role mention. Higher is better. See module docstring
    for the weighting rationale.
    """
    text = f"{item.title} {item.summary}"
    score = 0.0

    # 1. Category baseline (max 5.0).
    score += _CATEGORY_WEIGHT.get(item.category, 0.0)

    # 2. Recency (max 3.0 for very fresh, linear decay to 0 at 90 days).
    score += 3.0 * _recency_factor(item.published_date, today)

    # 3. Buying-signal patterns (each match adds 1.5, capped at 4.5 — three signals enough).
    score += min(_count_strong_signals(text) * 1.5, 4.5)

    # 4. Sender-keyword overlap (each match adds 0.5, capped at 3.0).
    if sender_keywords:
        item_tokens = _tokens(text)
        score += min(len(item_tokens & sender_keywords) * 0.5, 3.0)

    # 5. Recipient-role mention bonus (small but discriminative when present).
    if recipient_role:
        text_lower = text.lower()
        if any(syn in text_lower for syn in _role_synonyms(recipient_role)):
            score += 1.0

    return score


def rank_news(
    items: list[NewsItem],
    *,
    sender_kb: Optional[str] = None,
    recipient_role: Optional[str] = None,
) -> list[NewsItem]:
    """Sort items by sales-relevance (best first). Stable for equal scores."""
    sender_keywords = _extract_keywords(sender_kb or "")
    today = date.today()
    return sorted(
        items,
        key=lambda i: score_relevance(
            i,
            sender_keywords=sender_keywords,
            recipient_role=recipient_role,
            today=today,
        ),
        reverse=True,
    )
