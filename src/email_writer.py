"""Cold-email drafter with explicit business reasoning.

The earlier version handed the LLM a system prompt that mostly described
*how* to write (length, hook order, no buzzwords) but didn't require any
*reasoning* about why a given news event matters for the prospect. The
LLM defaulted to surface-level keyword stitching — the canonical failure
was the Storia/upGrad email that paired "you're rolling out AI education"
with "we use AI in films", a meaningless parallel.

This version requires structured chain-of-thought BEFORE writing:

  anchor_chosen → business_implication → sender_fit → intent_alignment → pitch_angle → email

Each step is a field in the LLM's structured output. The same call produces
the reasoning AND the email — no extra cost or latency. The reasoning is
logged with every draft so we can audit *why* a given email reads the way
it does, and tune prompts when reasoning is off.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from .config import LLM_MODEL, OPENAI_API_KEY
from .cost_guard import CostMeter, estimate_call_usd
from .schemas import LinkedInPost, NewsItem

logger = logging.getLogger(__name__)


class DraftedEmail(BaseModel):
    """Structured output of the email writer.

    The four reasoning fields are populated by the LLM BEFORE it writes the
    email body, forcing chain-of-thought. They're persisted in the per-run
    JSON log so we can audit reasoning quality independently of email quality
    — e.g. "the email read fine but the LLM's business_implication didn't
    actually relate to the anchor" is a different kind of bug than "the
    reasoning was right but the email phrasing was awkward".
    """

    anchor_chosen: str = Field(
        description=(
            "5-10 words identifying the news item or LinkedIn post you anchored on. "
            "Use 'no research signal' if RESEARCH is empty."
        ),
    )
    business_implication: str = Field(
        description=(
            "1-2 sentences. NOT a restatement of the news. What downstream needs, "
            "challenges, or opportunities does this event create for the prospect's "
            "business? E.g. 'Rolling out AI education to 1M students implies producing "
            "hundreds of hours of video content quickly.'"
        ),
    )
    sender_fit: str = Field(
        description=(
            "1-2 sentences. Using ONLY the capabilities the sender claims in SENDER "
            "CONTEXT, explain how the sender's offering addresses the implication "
            "above. Be specific — name the capability, not a vague benefit."
        ),
    )
    intent_alignment: str = Field(
        description=(
            "1 sentence. The OBJECTIVE the user typed IS the email's CTA. Explain "
            "how anchor → implication → sender_fit naturally lead to that CTA. If "
            "the chain feels forced, choose a different anchor or use a sender-led "
            "intro — do NOT stretch."
        ),
    )
    pitch_angle: str = Field(
        description="1 sentence summary of the email's angle, written like an internal SDR note.",
    )
    email: str = Field(
        description=(
            "The final email, starting with 'Subject: ...' on the first line. "
            "80-120 words. One CTA at the end aligned with the OBJECTIVE."
        ),
    )


_SYSTEM = (
    "You are a senior B2B SDR drafting a cold email. The quality bar is REASONING, "
    "not narrative stitching — surface-level parallels like 'you use AI in education, "
    "we use AI in films' are exactly the wrong move.\n\n"
    "INPUTS\n"
    "- ## OBJECTIVE = what THIS email must accomplish (the CTA). The user typed this; "
    "honour it strictly.\n"
    "- ## RESEARCH = recent company news + recent LinkedIn posts by the recipient. "
    "May be empty.\n"
    "- ## SENDER CONTEXT = the seller's offering, ICP, and tone. This is what we "
    "pitch FROM; never invent capabilities not claimed here.\n"
    "- Each LinkedIn post is tagged ``post_type``: 'original', 'commentary_on_repost', or 'pure_repost' (the recipient shared this without adding any text of their own).\n\n"
    "MANDATORY REASONING (fill these structured-output fields BEFORE writing the email):\n"
    "1. anchor_chosen — which news item / LinkedIn post you're anchored on, in 5-10 "
    "words. Use 'no research signal' if nothing in RESEARCH is usable.\n"
    "2. business_implication — what does this event MEAN for the prospect's "
    "downstream business needs? Reason about consequences, not the news itself. "
    "(Example: 'Maharashtra AI Excellence Centres at 1M-student scale = huge "
    "ongoing demand for educational video content, fast.')\n"
    "3. sender_fit — how does the sender's specific claimed offering address that "
    "implication? Name the capability. If you'd need to invent a capability the "
    "sender doesn't claim, sender_fit is WEAK and you must reconsider.\n"
    "4. intent_alignment — explain how anchor → implication → sender_fit lead "
    "naturally to the OBJECTIVE's CTA. If the chain is forced, pick a different "
    "anchor or write a sender-led intro.\n"
    "5. pitch_angle — 1-sentence internal summary of the email's angle.\n\n"
    "THEN write the email:\n"
    "- First line: 'Subject: <subject>' (6-10 words, specific to pitch_angle).\n"
    "- 80-120 words body, 3-4 sentences.\n"
    "- Opens by referencing the anchor (the news or post), states the implication "
    "concretely, names the sender's specific fit, ends with the OBJECTIVE's CTA "
    "phrased naturally.\n"
    "- For LinkedIn: phrase by post_type.  'original' → 'your post about X' (their own thought).  "
    "'commentary_on_repost' → 'your take on X' (their commentary, not the original).  "
    "'pure_repost' → 'noticed you shared the piece on X' or 'saw your repost on X' (acknowledge it's a share — never attribute the idea to them as if it were original). "
    "Never name the original author by name.\n"
    "- No buzzwords. No 'cutting-edge' / 'world-class' / 'revolutionary' / "
    "'best-in-class' / 'game-changing'.\n\n"
    "REFUSALS\n"
    "- If RESEARCH is empty: anchor_chosen = 'no research signal'; "
    "business_implication = 'N/A — sender-led intro'; sender_fit = the sender's "
    "main value prop; write a sender-anchored opener that names the prospect's "
    "company and a *plausible* ICP pain point. Acknowledge you're reaching out "
    "cold. Do NOT fabricate quotes, news, or posts.\n"
    "- If sender_fit would require inventing a capability the sender doesn't "
    "claim, treat as 'no research signal' (use a sender-led intro instead of "
    "stretching).\n"
)


def _serialize(items: list[NewsItem], posts: list[LinkedInPost]) -> str:
    payload = {
        "company_news": [
            {
                "title": i.title,
                "summary": i.summary,
                "date": i.published_date.isoformat(),
                "category": i.category,
                "sentiment": i.sentiment,
                "event_significance": i.event_significance,
                "url": str(i.url),
            }
            for i in items[:5]
        ],
        "linkedin_posts": [
            {
                "post_type": (
                    "commentary_on_repost" if p.is_repost and (p.user_commentary or "").strip()
                    else "pure_repost" if p.is_repost
                    else "original"
                ),
                "text": (p.user_commentary or p.text) if p.is_repost and (p.user_commentary or "").strip() else p.text,
                "date": p.posted_date.isoformat(),
                "topics": p.topics,
                "relevance_score": p.relevance_score,
                "sentiment": p.sentiment,
                "event_significance": p.event_significance,
            }
            for p in posts[:3]
        ],
    }
    return json.dumps(payload, indent=2)


def draft_email(
    items: list[NewsItem],
    posts: list[LinkedInPost],
    *,
    recipient_name: Optional[str],
    recipient_role: Optional[str],
    company_url: str,
    meter: CostMeter,
    sender_kb: Optional[str] = None,
    intent: Optional[str] = None,
) -> Optional[DraftedEmail]:
    """Run the chain-of-thought email writer.

    Returns the full :class:`DraftedEmail` (reasoning + email) so the caller
    can persist the reasoning trail in the per-run JSON log. Returns ``None``
    when there's nothing to write about AND no sender context, or when the
    LLM call fails — both are non-fatal at the pipeline level.

    Backward-compatible note: callers that previously did ``email_str =
    draft_email(...)`` need to switch to ``drafted = draft_email(...); email_str
    = drafted.email if drafted else ""``.
    """
    has_research = bool(items) or bool(posts)
    has_sender = bool(sender_kb and sender_kb.strip())
    if not has_research and not has_sender:
        # Truly nothing to anchor on — refuse rather than invent.
        return None

    research = _serialize(items, posts) if has_research else "(no recent news or LinkedIn posts found)"
    sender_block = (
        f"\n## SENDER CONTEXT\n{sender_kb.strip()}\n"
        if has_sender
        else "\n## SENDER CONTEXT\n(no sender profile selected — keep the value prop generic but coherent)\n"
    )
    intent_text = (intent or "").strip() or "Briefly introduce who we are and what we offer; request a 15-min discovery call."

    user_prompt = (
        f"Recipient: {recipient_name or 'there'} ({recipient_role or 'decision maker'}) at {company_url}.\n\n"
        f"## OBJECTIVE (this is the email's CTA — every reasoning field must serve it)\n{intent_text}\n\n"
        f"## RESEARCH\n{research}\n"
        f"{sender_block}\n"
        f"Fill in the reasoning fields, then write the email."
    )

    estimate = estimate_call_usd(input_chars=len(user_prompt), expected_output_tokens=500)
    meter.charge(estimate, label="draft_email")

    model_name = LLM_MODEL.split("/", 1)[1] if LLM_MODEL.startswith("openai/") else LLM_MODEL
    # ``with_structured_output`` forces Pydantic-shape responses — same pattern
    # used by linkedin_filter.py. Temperature kept at 0.4 (mild creativity in
    # email phrasing, deterministic reasoning fields).
    llm = ChatOpenAI(model=model_name, temperature=0.4, api_key=OPENAI_API_KEY).with_structured_output(
        DraftedEmail, include_raw=True
    )
    try:
        raw_and_parsed = llm.invoke(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user_prompt}]
        )
    except Exception as e:
        logger.warning("draft_email failed: %s", e)
        return None

    drafted: Optional[DraftedEmail] = (
        raw_and_parsed["parsed"] if isinstance(raw_and_parsed, dict) else raw_and_parsed
    )
    raw_msg = raw_and_parsed.get("raw") if isinstance(raw_and_parsed, dict) else None

    # Token accounting — same two-shape handling as filter_linkedin_posts.
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
                label="draft_email",
            )

    return drafted
