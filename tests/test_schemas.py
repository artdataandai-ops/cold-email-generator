from datetime import date

import pytest
from pydantic import ValidationError

from src.schemas import LinkedInPost, NewsItem, ResearchBundle


def test_news_item_requires_date():
    with pytest.raises(ValidationError):
        NewsItem(title="t", summary="s", url="https://a.com/1")  # missing published_date


def test_news_item_rejects_bad_category():
    with pytest.raises(ValidationError):
        NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="https://a.com/1",
            category="nonsense",
        )


def test_research_bundle_defaults():
    b = ResearchBundle(company_url="https://a.com/")
    assert b.items == []
    assert b.linkedin_posts == []
    assert b.warnings == []
    assert b.estimated_cost_usd == 0.0


def test_linkedin_post_optional_url():
    p = LinkedInPost(text="hello", posted_date=date(2026, 5, 1))
    assert p.url is None
    assert p.is_repost is False
    assert p.original_author is None
    assert p.user_commentary is None
    assert p.relevance_score is None
    assert p.topics == []


def test_linkedin_post_repost_fields():
    p = LinkedInPost(
        text="my added take",
        posted_date=date(2026, 5, 1),
        is_repost=True,
        original_author="Original Person",
        user_commentary="my added take",
        relevance_score=0.85,
        topics=["AI strategy"],
    )
    assert p.is_repost is True
    assert p.original_author == "Original Person"
    assert p.relevance_score == 0.85
    assert p.topics == ["AI strategy"]


def test_news_item_rejects_example_com_url():
    """LLM hallucinates example.com when it can't find a real article link."""
    with pytest.raises(ValidationError):
        NewsItem(
            title="Real-sounding title",
            summary="Plausible summary.",
            published_date=date(2026, 5, 1),
            url="https://example.com/dialect-partnership-news",
        )


def test_news_item_rejects_www_example_org():
    with pytest.raises(ValidationError):
        NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="https://www.example.org/x",
        )


def test_news_item_rejects_localhost():
    with pytest.raises(ValidationError):
        NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="http://localhost:3000/x",
        )


def test_news_item_accepts_real_urls():
    item = NewsItem(
        title="t",
        summary="s",
        published_date=date(2026, 5, 1),
        url="https://www.thredd.ai/resources/article-x",
    )
    assert item.url.endswith("/article-x")


# ---- Sentiment field (Phase 1: classification only, no scoring impact) ----

def test_news_item_sentiment_defaults_to_neutral():
    """Backward-compat: pre-sentiment callers (RSS path, JSON-LD short-circuit,
    legacy fixtures) don't set sentiment and must continue working. Default of
    'neutral' means downstream scorers won't bias for or against the item."""
    item = NewsItem(
        title="t",
        summary="s",
        published_date=date(2026, 5, 1),
        url="https://www.thredd.ai/x",
    )
    assert item.sentiment == "neutral"


def test_news_item_accepts_each_sentiment_value():
    """All three NewsSentiment literals round-trip through validation."""
    for sentiment in ("positive", "neutral", "negative"):
        item = NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="https://www.thredd.ai/x",
            sentiment=sentiment,
        )
        assert item.sentiment == sentiment


def test_news_item_rejects_unknown_sentiment():
    """Guard against the LLM emitting freeform polarity strings (e.g. 'mixed',
    'bad', 'positive!'). Schema must reject so we don't silently propagate."""
    with pytest.raises(ValidationError):
        NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="https://www.thredd.ai/x",
            sentiment="mixed",  # not in NewsSentiment literal
        )


# ---- event_significance field (Phase 2a: classification only) ----

def test_news_item_event_significance_defaults_to_notable():
    """Default 'notable' so unknown-significance items sit mid-tier and don't
    skew ranking either way before the LLM classifications are trusted."""
    item = NewsItem(
        title="t",
        summary="s",
        published_date=date(2026, 5, 1),
        url="https://www.thredd.ai/x",
    )
    assert item.event_significance == "notable"


def test_news_item_accepts_each_event_significance_value():
    """All six EventSignificance literals must round-trip through validation.
    The administrative+peripheral classes are the whole point — they're how
    the Lenskart 'legal-entity rename' bug stops surfacing as an email anchor."""
    for value in ("major", "notable", "routine", "administrative", "peripheral", "negative"):
        item = NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="https://www.thredd.ai/x",
            event_significance=value,
        )
        assert item.event_significance == value


def test_news_item_rejects_unknown_event_significance():
    """LLM emitting 'huge', 'important', 'meh' must not silently degrade
    downstream ranking."""
    with pytest.raises(ValidationError):
        NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="https://www.thredd.ai/x",
            event_significance="huge",
        )


def test_linkedin_post_classification_fields_default_neutral_notable():
    """LinkedIn posts and news items share the same two classification axes so
    cross-source ranking can compare them on one scale. Defaults must keep
    existing callers working with no behavioural change."""
    p = LinkedInPost(text="hello", posted_date=date(2026, 5, 1))
    assert p.sentiment == "neutral"
    assert p.event_significance == "notable"


def test_linkedin_post_accepts_classification_values():
    p = LinkedInPost(
        text="we raised our Series B",
        posted_date=date(2026, 5, 1),
        sentiment="positive",
        event_significance="major",
    )
    assert p.sentiment == "positive"
    assert p.event_significance == "major"


def test_linkedin_post_rejects_unknown_event_significance():
    with pytest.raises(ValidationError):
        LinkedInPost(
            text="x",
            posted_date=date(2026, 5, 1),
            event_significance="enormous",
        )


def test_news_item_rejects_dotless_host():
    """The Stripe regression: LLM emitted relative paths like 'in/newsroom/...'
    which urljoin turned into 'https://in/newsroom/...' — and 'in' is a single-
    word hostname that can't resolve to a real website."""
    with pytest.raises(ValidationError):
        NewsItem(
            title="Stripe announces something",
            summary="Real-looking summary text.",
            published_date=date(2026, 5, 1),
            url="https://in/newsroom/news/aws-stripe-agentcore-privy",
        )


def test_news_item_rejects_empty_host():
    """e.g. 'newsroom/news/x' resolved by urljoin under a base with no scheme."""
    with pytest.raises(ValidationError):
        NewsItem(
            title="t",
            summary="s",
            published_date=date(2026, 5, 1),
            url="newsroom/news/x",
        )


def test_sender_profile_round_trip():
    from src.db import SenderProfile
    p = SenderProfile(
        id="abc-123",
        name="Acme AI",
        description="AI tools for enterprise",
        kb_text="we sell X to Y",
        created_at="2026-05-07T10:00:00Z",
        updated_at="2026-05-07T10:00:00Z",
    )
    assert p.name == "Acme AI"
    assert p.kb_text == "we sell X to Y"
