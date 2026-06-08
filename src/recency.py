from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from dateutil import parser as dateparser

from .schemas import LinkedInPost, NewsItem


def parse_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        # LinkedIn / JS commonly use millisecond timestamps. Anything past ~year 2286
        # in seconds (10^12) is clearly ms — divide back to seconds.
        ts = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (OSError, ValueError, OverflowError):
            return None
    try:
        return dateparser.parse(str(value)).date()
    except (ValueError, TypeError, OverflowError):
        return None


def _within_window(d: Optional[date], days: int, today: Optional[date] = None) -> bool:
    if d is None:
        return False
    today = today or date.today()
    return today - timedelta(days=days) <= d <= today


def filter_recent_news(items: Iterable[NewsItem], days: int, today: Optional[date] = None) -> list[NewsItem]:
    return [i for i in items if _within_window(i.published_date, days, today)]


def filter_recent_posts(posts: Iterable[LinkedInPost], days: int, today: Optional[date] = None) -> list[LinkedInPost]:
    return [p for p in posts if _within_window(p.posted_date, days, today)]


def dedupe_news(items: Iterable[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    out: list[NewsItem] = []
    for it in items:
        key = str(it.url).rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


