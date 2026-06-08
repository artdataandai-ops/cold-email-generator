from datetime import date, timedelta

from src.linkedin import _coerce_apify_post, is_pure_repost
from src.schemas import LinkedInPost


def _today_iso() -> str:
    return date.today().isoformat()


def test_coerce_original_post():
    raw = {
        "type": "post",
        "text": "Reflecting on our latest product launch.",
        "postedAt": _today_iso(),
        "url": "https://www.linkedin.com/posts/abc",
        "likesCount": 12,
        "commentsCount": 3,
    }
    p = _coerce_apify_post(raw)
    assert p is not None
    assert p.is_repost is False
    assert p.original_author is None
    assert p.user_commentary is None
    assert p.text.startswith("Reflecting")
    assert p.engagement == "12 likes, 3 comments"


def test_coerce_pure_repost_keeps_original_text_but_flags_repost():
    raw = {
        "type": "repost",
        "postedAt": _today_iso(),
        "repostedPost": {
            "text": "Original author's deep insight.",
            "author": {"name": "Jane Original"},
        },
    }
    p = _coerce_apify_post(raw)
    assert p is not None
    assert p.is_repost is True
    assert p.user_commentary is None
    assert p.original_author == "Jane Original"
    assert p.text == "Original author's deep insight."
    # filter rule: pure repost
    assert is_pure_repost(p) is True


def test_coerce_pure_repost_flat_shape_uses_top_level_text():
    """Actor LQQIXN9Othf8f7R5n flattens reposts: post_type='repost' but no nested
    `repostedPost` object — the original's text + author sit at the top level."""
    raw = {
        "post_type": "repost",
        "posted_at": {"date": "2026-05-30 12:00:00", "timestamp": 1748606400000},
        "text": "Original article body that the user shared without comment.",
        "author": {"first_name": "G.", "last_name": "Vijaya Raghavan"},
        "url": "https://www.linkedin.com/posts/g-vijaya-raghavan_activity-123",
        "stats": {"total_reactions": 97, "comments": 12},
    }
    p = _coerce_apify_post(raw)
    assert p is not None
    assert p.is_repost is True
    assert p.user_commentary is None
    assert p.text.startswith("Original article body")
    assert p.original_author == "G. Vijaya Raghavan"
    # Still a pure repost — the user added no commentary — but it now reaches
    # the downstream LLM scorer instead of being dropped at coerce.
    assert is_pure_repost(p) is True


def test_coerce_repost_with_commentary_uses_commentary_as_text():
    raw = {
        "type": "repost",
        "postedAt": _today_iso(),
        "commentary": "This nails what we've been seeing in our pipeline.",
        "repostedPost": {
            "text": "Original article body.",
            "author": {"name": "Jane Original"},
        },
    }
    p = _coerce_apify_post(raw)
    assert p is not None
    assert p.is_repost is True
    assert p.user_commentary == "This nails what we've been seeing in our pipeline."
    assert p.text == "This nails what we've been seeing in our pipeline."
    assert is_pure_repost(p) is False


def test_coerce_drops_undated_post():
    raw = {"type": "post", "text": "no date here"}
    assert _coerce_apify_post(raw) is None


def test_coerce_drops_empty_text():
    raw = {"type": "post", "postedAt": _today_iso(), "text": ""}
    assert _coerce_apify_post(raw) is None


def test_is_pure_repost_handles_whitespace_only_commentary():
    p = LinkedInPost(
        text="x",
        posted_date=date.today() - timedelta(days=1),
        is_repost=True,
        user_commentary="   \n  ",
    )
    assert is_pure_repost(p) is True


def test_coerce_snake_case_actor_shape():
    """Regression for the apify.linkedin-profile-posts actor (snake_case + nested stats).
    Captured live from the actor on 2026-05-11."""
    raw = {
        "urn": {"activity_urn": "7459522671878291456"},
        "full_urn": "urn:li:activity:7459522671878291456",
        "posted_at": {"date": "2026-05-11 10:39:56", "relative": "2 hours ago", "timestamp": 1778488796205},
        "text": "New on PYMNTS: a featured podcast discussing how AI and real-time transaction intelligence...",
        "url": "https://www.linkedin.com/posts/andrew-mouat_new-on-pymnts-activity-7459522671878291456",
        "post_type": "regular",
        "author": {
            "first_name": "Andy",
            "last_name": "Mouat",
            "headline": "Senior Software Engineer @ Thredd",
            "username": "andrew-mouat",
        },
        "stats": {"total_reactions": 12, "comments_count": 3},
    }
    p = _coerce_apify_post(raw)
    assert p is not None, "Coercer dropped a perfectly good post — field names probably drifted"
    assert p.is_repost is False
    assert p.text.startswith("New on PYMNTS")
    assert p.engagement == "12 likes, 3 comments"
    assert p.posted_date.year == 2026


def test_coerce_handles_nested_posted_at_with_only_timestamp():
    raw = {
        "text": "hello",
        "posted_at": {"timestamp": 1778488796205},
        "post_type": "regular",
    }
    p = _coerce_apify_post(raw)
    assert p is not None
    assert p.posted_date is not None


# ---- Classification axes on LinkedIn match the news pipeline ----

def test_linkedin_filter_system_prompt_includes_classification_axes():
    """The LLM scorer must produce ``sentiment`` and ``event_significance`` for
    each selected post, on the SAME vocabulary used for NewsItem. This is what
    lets a later phase rank news + LinkedIn on a single scale (so a great
    LinkedIn post can beat all the news items if it's more substantive)."""
    import pytest
    pytest.importorskip("langchain_openai")  # linkedin_filter module imports it eagerly
    from src.linkedin_filter import _SYSTEM

    low = _SYSTEM.lower()
    assert "sentiment" in low
    assert "event_significance" in low
    # All six significance classes documented inline.
    for label in ("major", "notable", "routine", "administrative", "peripheral", "negative"):
        assert label in low, f"LinkedIn prompt missing significance class {label!r}"
    # The default-to-notable guardrail.
    assert "default to notable" in low or ("default" in low and "notable" in low)


def test_linkedin_filter_scored_pydantic_schema_carries_classifications():
    """The structured-output schema must expose both fields so the LLM is
    required (by Pydantic) to populate them — otherwise a quietly-omitted
    classification reverts to schema default and the signal is lost."""
    import pytest
    pytest.importorskip("langchain_openai")
    from src.linkedin_filter import _Scored

    fields = _Scored.model_fields
    assert "sentiment" in fields
    assert "event_significance" in fields
    # Defaults match the LinkedInPost schema so callers degrade gracefully.
    instance = _Scored(index=0, relevance_score=0.5)
    assert instance.sentiment == "neutral"
    assert instance.event_significance == "notable"
