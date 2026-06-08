"""Tests for the chain-of-thought email writer.

These pin the structured-output schema and prompt content. We don't mock the
LLM here — full integration is exercised by the batch runner against the
fixture set. The goal of these tests is to guarantee the prompt KEEPS asking
for the reasoning fields (so a casual prompt edit can't quietly drop them).
"""
import pytest


def test_drafted_email_schema_has_reasoning_fields():
    """All four reasoning fields plus the email itself MUST be on the Pydantic
    model — otherwise the LLM is free to omit them and chain-of-thought
    silently degrades into the old narrative-stitching behaviour."""
    pytest.importorskip("langchain_openai")
    from src.email_writer import DraftedEmail

    fields = DraftedEmail.model_fields
    for f in (
        "anchor_chosen",
        "business_implication",
        "sender_fit",
        "intent_alignment",
        "pitch_angle",
        "email",
    ):
        assert f in fields, f"DraftedEmail missing required field {f!r}"


def test_drafted_email_round_trip():
    """Schema must accept valid reasoning + email content."""
    pytest.importorskip("langchain_openai")
    from src.email_writer import DraftedEmail

    d = DraftedEmail(
        anchor_chosen="upGrad AI Excellence Centres partnership",
        business_implication="Educating 1M+ students in AI implies producing hundreds of hours of educational video at scale and speed.",
        sender_fit="Storia's AI-driven production pipeline can deliver broadcast-quality educational video in days, not weeks.",
        intent_alignment="Connecting the production scale problem to Storia's speed advantage leads naturally to a 15-min discovery call.",
        pitch_angle="Help upGrad ship cohort-1 content for the AI Excellence Centres faster.",
        email="Subject: Helping upGrad scale AI Excellence Centres faster\n\nHi there,\n\n...",
    )
    assert d.anchor_chosen.startswith("upGrad")
    assert d.email.startswith("Subject:")


def test_system_prompt_requires_chain_of_thought_reasoning():
    """The system prompt must instruct the LLM to fill the reasoning fields
    BEFORE the email body. Without this, structured output works but
    chain-of-thought never fires, defeating the whole point."""
    pytest.importorskip("langchain_openai")
    from src.email_writer import _SYSTEM

    low = _SYSTEM.lower()
    # Each reasoning step is referenced explicitly.
    for step in (
        "anchor_chosen",
        "business_implication",
        "sender_fit",
        "intent_alignment",
        "pitch_angle",
    ):
        assert step in low, f"Reasoning step {step!r} missing from system prompt"
    # The "fill these BEFORE writing" instruction is present (else the LLM may
    # write first and fill reasoning as an afterthought).
    assert "before writing" in low


def test_system_prompt_calls_out_surface_keyword_stitching_as_wrong():
    """The Storia/upGrad failure mode — pairing 'you use AI in education' with
    'we use AI in films' — must be explicitly framed as wrong in the prompt,
    otherwise the LLM defaults back to that pattern under temperature."""
    pytest.importorskip("langchain_openai")
    from src.email_writer import _SYSTEM

    low = _SYSTEM.lower()
    # The phrase mentioning surface-level / stitching / parallels should appear.
    assert "surface" in low or "stitching" in low
    # The example from the actual failure case is preserved as a counter-example.
    assert "ai in films" in low


def test_system_prompt_makes_user_intent_a_first_class_input():
    """The user-entered OBJECTIVE must be referenced as the CTA driver. If the
    prompt doesn't tie reasoning back to OBJECTIVE, the LLM may produce a
    great-sounding but wrong-CTA email."""
    pytest.importorskip("langchain_openai")
    from src.email_writer import _SYSTEM

    low = _SYSTEM.lower()
    assert "objective" in low
    # intent_alignment must explicitly link reasoning chain to the OBJECTIVE.
    assert "intent_alignment" in low and "cta" in low


def test_system_prompt_documents_refusal_path_when_research_empty():
    """If RESEARCH is empty, the writer must NOT fabricate news — it must
    fall back to a sender-led intro. This was the existing behaviour; keep
    it explicit so the refactor doesn't regress."""
    pytest.importorskip("langchain_openai")
    from src.email_writer import _SYSTEM

    low = _SYSTEM.lower()
    assert "no research signal" in low
    # And the no-fabrication rule.
    assert "fabricate" in low or "invent" in low


def test_system_prompt_documents_sender_capability_invention_refusal():
    """If sender_fit would require inventing capabilities not in SENDER
    CONTEXT, the writer must back off to a sender-led intro. This is the
    second-tier defense against keyword-stitching pitfalls."""
    pytest.importorskip("langchain_openai")
    from src.email_writer import _SYSTEM

    low = _SYSTEM.lower()
    # The "weak fit → sender-led intro" escape valve is present.
    assert "invent" in low and ("sender-led" in low or "no research signal" in low)


def test_draft_email_returns_none_when_no_research_and_no_sender():
    """Backward-compat guard: pipeline relies on None signalling 'don't draft'."""
    pytest.importorskip("langchain_openai")
    from src.cost_guard import CostMeter
    from src.email_writer import draft_email

    out = draft_email(
        items=[],
        posts=[],
        recipient_name=None,
        recipient_role=None,
        company_url="https://acme.com/",
        meter=CostMeter(),
        sender_kb=None,
        intent=None,
    )
    assert out is None


def test_serialize_exposes_classification_axes_to_llm():
    """The LLM must SEE each news/LinkedIn item's sentiment + event_significance
    when reasoning — otherwise it can't downweight an 'administrative' anchor."""
    pytest.importorskip("langchain_openai")
    from datetime import date

    from src.email_writer import _serialize
    from src.schemas import LinkedInPost, NewsItem

    items = [
        NewsItem(
            title="Lenskart Solutions Limited Rebrands",
            summary="Corporate rename ahead of IPO.",
            published_date=date(2026, 5, 14),
            url="https://www.lenskart.com/corporate",
            category="news",
            sentiment="positive",
            event_significance="administrative",  # the bug-class signal
        ),
    ]
    posts = [
        LinkedInPost(
            text="our team shipped X this week",
            posted_date=date(2026, 5, 10),
            relevance_score=0.8,
            sentiment="positive",
            event_significance="major",
        ),
    ]
    payload = _serialize(items, posts)
    assert "administrative" in payload
    assert "event_significance" in payload
    assert "sentiment" in payload
