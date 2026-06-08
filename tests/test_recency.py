from datetime import date, timedelta

from src.recency import dedupe_news, filter_recent_news, parse_date
from src.relevance import rank_news
from src.schemas import NewsItem


def _item(url: str, days_ago: int, category: str = "news") -> NewsItem:
    return NewsItem(
        title=f"item-{url}",
        summary="x" * 10,
        published_date=date.today() - timedelta(days=days_ago),
        url=url,
        category=category,
    )


def test_parse_date_handles_iso_and_natural():
    assert parse_date("2026-04-15") == date(2026, 4, 15)
    assert parse_date("Apr 15, 2026") == date(2026, 4, 15)
    assert parse_date(None) is None
    assert parse_date("not a date") is None


def test_filter_recent_drops_old():
    today = date(2026, 5, 7)
    items = [
        NewsItem(title="t", summary="s" * 10, published_date=date(2026, 4, 30), url="https://a.com/1", category="news"),
        NewsItem(title="t", summary="s" * 10, published_date=date(2025, 1, 1), url="https://a.com/2", category="news"),
    ]
    kept = filter_recent_news(items, days=60, today=today)
    assert len(kept) == 1
    assert str(kept[0].url) == "https://a.com/1"


def test_dedupe_news_by_url():
    items = [_item("https://a.com/x", 1), _item("https://a.com/x/", 2), _item("https://a.com/y", 3)]
    assert {str(i.url).rstrip("/") for i in dedupe_news(items)} == {"https://a.com/x", "https://a.com/y"}


def test_rank_news_orders_by_category_then_date():
    funding_old = _item("https://a.com/f", 30, "funding")
    blog_new = _item("https://a.com/b", 1, "blog")
    award_recent = _item("https://a.com/a", 5, "award")
    ranked = rank_news([blog_new, funding_old, award_recent])
    assert ranked[0] == funding_old
    assert ranked[1] == award_recent
    assert ranked[2] == blog_new
