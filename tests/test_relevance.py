"""Tests for src/relevance.py — sales-aware news ranking (pure logic)."""
from datetime import date, timedelta

from src.relevance import (
    _count_strong_signals,
    _extract_keywords,
    _recency_factor,
    _role_synonyms,
    rank_news,
    score_relevance,
)
from src.schemas import NewsItem


def _item(*, title="t", summary="s" * 10, days_ago=10, category="news", url="https://a.com/1"):
    return NewsItem(
        title=title,
        summary=summary,
        published_date=date.today() - timedelta(days=days_ago),
        url=url,
        category=category,
    )


# ---- recency factor ----

def test_recency_factor_today_is_max():
    assert _recency_factor(date.today()) == 1.0


def test_recency_factor_decays_linearly():
    today = date(2026, 5, 1)
    half = date(2026, 5, 1) - timedelta(days=45)  # halfway through 90-day horizon
    val = _recency_factor(half, today=today)
    assert 0.45 < val < 0.55


def test_recency_factor_floors_at_zero():
    assert _recency_factor(date(2020, 1, 1), today=date(2026, 5, 1)) == 0.0


# ---- buying-signal counter ----

def test_strong_signals_match_funding_amount():
    assert _count_strong_signals("Acme raises $40M in Series B") >= 1


def test_strong_signals_match_acquisition():
    assert _count_strong_signals("Acme acquires Bravo Corp for $200M") >= 1


def test_strong_signals_match_leadership_hire():
    assert _count_strong_signals("Acme appoints new CTO Jane Doe") >= 1


def test_strong_signals_match_expansion():
    assert _count_strong_signals("Acme expands into the UK market") >= 1


def test_strong_signals_zero_for_generic_blog():
    assert _count_strong_signals("5 tips for better marketing") == 0


# ---- keyword extraction ----

def test_extract_keywords_dedupes_and_drops_stopwords():
    kws = _extract_keywords("We are the leading payments processor for fintechs and you should know this")
    assert "payments" in kws
    assert "processor" in kws
    assert "fintechs" in kws
    assert "the" not in kws
    assert "are" not in kws


def test_extract_keywords_caps_at_max_n():
    text = " ".join(f"word{i}" for i in range(100))
    kws = _extract_keywords(text, max_n=10)
    assert len(kws) <= 10


# ---- role synonym mapping ----

def test_role_synonyms_expands_acronyms():
    syns = _role_synonyms("Chief Technology Officer")
    assert "cto" in syns
    assert "chief technology officer" in syns


def test_role_synonyms_expands_from_acronym():
    syns = _role_synonyms("CTO")
    assert "cto" in syns
    assert "chief technology officer" in syns


# ---- single-item scoring ----

def test_score_baseline_funding_outscores_blog():
    funding = _item(category="funding", title="x", summary="y")
    blog = _item(category="blog", title="x", summary="y")
    assert score_relevance(funding) > score_relevance(blog)


def test_score_recency_breaks_tie_within_category():
    fresh = _item(category="news", days_ago=2)
    stale = _item(category="news", days_ago=80)
    assert score_relevance(fresh) > score_relevance(stale)


def test_score_signal_language_boosts_blog_above_press():
    """A blog post about a $40M raise should outscore a generic press release."""
    blog_with_signal = _item(
        category="blog",
        title="Acme raises $40M in Series B",
        summary="The company announced a Series B funding round.",
    )
    plain_press = _item(category="press", title="Q1 newsletter", summary="our updates")
    assert score_relevance(blog_with_signal) > score_relevance(plain_press)


def test_score_sender_overlap_boosts_relevant_items():
    """If sender sells payments tooling, a payments-flavored news item beats an unrelated one."""
    sender_kws = _extract_keywords(
        "We help fintech companies build payments infrastructure and card-issuing platforms."
    )
    relevant = _item(
        category="news",
        title="Bravo launches new payments product",
        summary="Bravo Corp announced a new card-issuing platform for fintechs.",
    )
    unrelated = _item(
        category="news",
        title="Bravo office party recap",
        summary="A fun time was had by all at the holiday celebration.",
    )
    s_relevant = score_relevance(relevant, sender_keywords=sender_kws)
    s_unrelated = score_relevance(unrelated, sender_keywords=sender_kws)
    assert s_relevant > s_unrelated


def test_score_recipient_role_bonus():
    """Item mentioning CTO scores higher when emailing a CTO."""
    cto_news = _item(title="Acme appoints new CTO", summary="leadership change")
    with_role = score_relevance(cto_news, recipient_role="Chief Technology Officer")
    without_role = score_relevance(cto_news, recipient_role=None)
    assert with_role > without_role


# ---- end-to-end rank_news ----

def test_rank_news_orders_top_item_by_combined_score():
    """A blog post about a funding round should outrank a stale low-category press item."""
    funding_blog = _item(
        title="Acme raises $50M in Series C funding",
        summary="The round will fund expansion into Europe.",
        category="blog",
        days_ago=3,
    )
    old_press = _item(
        title="Acme issues Q2 newsletter",
        summary="quarterly update",
        category="press",
        days_ago=85,
    )
    funding_official = _item(
        title="Acme closes $50M Series C round",
        summary="Acme Corp announced a Series C round led by Sequoia.",
        category="funding",
        days_ago=5,
    )
    ranked = rank_news([old_press, funding_blog, funding_official])
    # funding_official has every signal: highest category + fresh + signal language.
    assert ranked[0] == funding_official
    # old_press is dead weight — lowest of the three.
    assert ranked[-1] == old_press


def test_rank_news_sender_aware_reorders_same_category():
    """Two same-category items: the sender-relevant one comes first."""
    relevant = _item(
        title="Bravo announces new payments API",
        summary="Bravo launched a payments API for fintech developers.",
        category="news",
        days_ago=10,
    )
    irrelevant = _item(
        title="Bravo sponsors marathon",
        summary="Charity event in Dublin.",
        category="news",
        days_ago=10,
    )
    sender_kb = "We build payments infrastructure and APIs for fintechs."
    ranked = rank_news([irrelevant, relevant], sender_kb=sender_kb)
    assert ranked[0] == relevant


def test_rank_news_no_sender_kb_falls_back_to_category_and_recency():
    """With no sender_kb, ordering should match the old category+date rule."""
    funding_old = _item(category="funding", days_ago=30, url="https://a.com/f")
    blog_new = _item(category="blog", days_ago=1, url="https://a.com/b")
    award_mid = _item(category="award", days_ago=5, url="https://a.com/a")
    ranked = rank_news([blog_new, funding_old, award_mid])
    assert ranked[0] == funding_old
    assert ranked[1] == award_mid
    assert ranked[2] == blog_new
