from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from .config import LLM_MODEL, OPENAI_API_KEY
from .cost_guard import CostMeter, estimate_call_usd
# is_pure_repost is no longer used here — pure reposts get scored by the LLM
# like any other post. The helper itself stays available for callers that want it.
from .schemas import EventSignificance, LinkedInPost, NewsItem, NewsSentiment

logger = logging.getLogger(__name__)

MAX_KEPT_POSTS = 3


class _Scored(BaseModel):
    index: int = Field(description="Index into the input list")
    relevance_score: float = Field(ge=0.0, le=1.0)
    topics: list[str] = Field(default_factory=list)
    # Phase 2a: same classification axes used on NewsItem so news + LinkedIn
    # share a unified vocabulary in downstream ranking.
    sentiment: NewsSentiment = Field(
        default="neutral",
        description="positive / neutral / negative — polarity of the post's content",
    )
    event_significance: EventSignificance = Field(
        default="notable",
        description=(
            "major | notable | routine | administrative | peripheral | negative "
            "— same meaning as on NewsItem; describes the underlying event this "
            "post is about, not the writing style"
        ),
    )


class _ScoredList(BaseModel):
    selected: list[_Scored] = Field(default_factory=list)


_SYSTEM = (
    "You score LinkedIn posts for relevance as a cold-email anchor. "
    "Higher scores go to posts that are: substantive (not 'thanks!' / emojis-only), "
    "professionally relevant to the recipient's role, recent, and specific enough that "
    "an outreach email could quote a concrete idea. Lower scores for personal life updates, "
    "vague platitudes, or content that would sound stalker-ish if quoted.\n\n"
    "For each post you select, ALSO classify:\n"
    "- sentiment: positive | neutral | negative — the tone of the underlying topic "
    "(a celebratory product launch is positive; a layoff discussion is negative; "
    "a factual industry update is neutral). When unsure, default to neutral.\n"
    "- event_significance: how substantive the underlying event is for a "
    "cold-email opener:\n"
    "  * major = funding raised, marquee partnership, major launch, key hire, "
    "industry-leading milestone — the kind of thing SDRs lead with.\n"
    "  * notable = incremental product news, smaller partnership, recognition.\n"
    "  * routine = thought-leadership on a known topic, conference recap, "
    "scheduled corporate update.\n"
    "  * administrative = legal-entity rename, ticker change, board reshuffle — "
    "internally important but NOT a customer-facing event.\n"
    "  * peripheral = passing mention or generic industry commentary that "
    "doesn't really describe a specific event.\n"
    "  * negative = controversy, layoff, lawsuit, scandal, public criticism, "
    "regardless of size.\n"
    "  When unsure, default to notable.\n\n"
    "Return at most the top N posts. Output JSON: "
    "{selected: [{index, relevance_score, topics, sentiment, event_significance}, ...]}."
)


def _serialize_posts_for_scoring(posts: list[LinkedInPost]) -> str:
    out = []
    for i, p in enumerate(posts):
        if p.is_repost and (p.user_commentary or "").strip():
            post_type = "commentary_on_repost"
        elif p.is_repost:
            post_type = "pure_repost"
        else:
            post_type = "original"
        out.append({
            "index": i,
            "post_type": post_type,
            "text": (p.text or "")[:600],
            "posted_date": p.posted_date.isoformat(),
            "engagement": p.engagement,
        })
    return json.dumps(out, indent=2)


def _serialize_news_context(items: list[NewsItem]) -> str:
    return json.dumps(
        [
            {"title": i.title, "summary": i.summary[:200], "category": i.category, "date": i.published_date.isoformat()}
            for i in items[:5]
        ],
        indent=2,
    )


def filter_linkedin_posts(
    posts: list[LinkedInPost],
    *,
    recipient_role: Optional[str],
    company_items: list[NewsItem],
    meter: CostMeter,
    max_kept: int = MAX_KEPT_POSTS,
) -> list[LinkedInPost]:
    """LLM-rank the posts (including pure reposts). Return at most `max_kept`, sorted by relevance desc."""
    if not posts:
        return []

    # Pure reposts used to be filtered here; we now keep them and let the LLM
    # decide their relevance. The email writer phrases them as "noticed you
    # shared X" rather than as the recipient's original thought.
    candidates = posts

    user_prompt = (
        f"Recipient role: {recipient_role or 'unknown decision maker'}\n\n"
        f"Recent company news (for topical alignment context):\n{_serialize_news_context(company_items)}\n\n"
        f"Candidate LinkedIn posts (score and pick the top {max_kept}):\n"
        f"{_serialize_posts_for_scoring(candidates)}\n\n"
        f"Return at most {max_kept} entries, ordered by relevance_score descending. "
        f"Each entry: {{index (int into the candidate list), relevance_score (0..1), topics: list[str] of 1-3 short tags}}."
    )

    estimate = estimate_call_usd(input_chars=len(user_prompt), expected_output_tokens=300)
    meter.charge(estimate, label="filter_linkedin_posts")

    model_name = LLM_MODEL.split("/", 1)[1] if LLM_MODEL.startswith("openai/") else LLM_MODEL
    # include_raw=True so we can read token usage off the underlying AIMessage.
    llm = ChatOpenAI(model=model_name, temperature=0, api_key=OPENAI_API_KEY).with_structured_output(
        _ScoredList, include_raw=True,
    )

    try:
        raw_and_parsed = llm.invoke(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user_prompt}]
        )
    except Exception as e:
        logger.warning("filter_linkedin_posts LLM call failed (%s); returning recency-only top %d", e, max_kept)
        return sorted(candidates, key=lambda p: p.posted_date, reverse=True)[:max_kept]

    result: _ScoredList = raw_and_parsed["parsed"] if isinstance(raw_and_parsed, dict) else raw_and_parsed
    raw_msg = raw_and_parsed.get("raw") if isinstance(raw_and_parsed, dict) else None
    if raw_msg is not None:
        usage_meta = getattr(raw_msg, "usage_metadata", None) or {}
        if usage_meta:
            prompt_tok = int(usage_meta.get("input_tokens") or 0)
            completion_tok = int(usage_meta.get("output_tokens") or 0)
        else:
            legacy = (getattr(raw_msg, "response_metadata", {}) or {}).get("token_usage") or {}
            prompt_tok = int(legacy.get("prompt_tokens") or 0)
            completion_tok = int(legacy.get("completion_tokens") or 0)
        if prompt_tok or completion_tok:
            meter.record_llm_usage(
                prior_estimate_usd=estimate,
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                model=LLM_MODEL,
                label="filter_linkedin_posts",
            )

    kept: list[LinkedInPost] = []
    for s in result.selected[:max_kept]:
        if 0 <= s.index < len(candidates):
            p = candidates[s.index]
            p.relevance_score = round(float(s.relevance_score), 3)
            p.topics = list(s.topics or [])
            # Phase 2a: propagate sentiment + event_significance from the LLM
            # onto the post itself, so cross-source ranking in a later phase
            # sees both news and LinkedIn on the same axes. Defaults from the
            # Pydantic model apply when the LLM omits them.
            p.sentiment = s.sentiment
            p.event_significance = s.event_significance
            kept.append(p)

    kept.sort(key=lambda p: (p.relevance_score or 0.0), reverse=True)
    return kept
