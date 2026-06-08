from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .config import (
    APIFY_LINKEDIN_POST_LIMIT,
    DISABLE_LINKEDIN_DEFAULT,
    DISABLE_WEB_SEARCH_DEFAULT,
    MAX_DISCOVERED_URLS_DEFAULT,
    MAX_NEWS_ITEMS,
    RECENCY_DAYS,
    REQUIRE_CONTEXT_FOR_EMAIL_DEFAULT,
    SEARCH_MAX_RESULTS,
)
from .db import AppSettings, get_session

SINGLETON_ID = 1
DEFAULT_INTENT_FALLBACK = (
    "Briefly introduce who we are and what we offer; request a 15-min discovery call."
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bootstrap_from_env() -> AppSettings:
    return AppSettings(
        id=SINGLETON_ID,
        recency_days=RECENCY_DAYS,
        max_news_items=MAX_NEWS_ITEMS,
        max_discovered_urls=MAX_DISCOVERED_URLS_DEFAULT,
        search_max_results=SEARCH_MAX_RESULTS,
        disable_web_search=DISABLE_WEB_SEARCH_DEFAULT,
        linkedin_post_limit=APIFY_LINKEDIN_POST_LIMIT,
        default_intent=DEFAULT_INTENT_FALLBACK,
        disable_linkedin=DISABLE_LINKEDIN_DEFAULT,
        require_context_for_email=REQUIRE_CONTEXT_FOR_EMAIL_DEFAULT,
        updated_at=_utcnow_iso(),
    )


def get_settings() -> AppSettings:
    """Return the singleton settings row, bootstrapping from .env on first call."""
    with get_session() as session:
        row = session.get(AppSettings, SINGLETON_ID)
        if row is None:
            row = _bootstrap_from_env()
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        # Back-fill new columns added in later releases (still NULL on old rows).
        changed = False
        if not row.default_intent:
            row.default_intent = DEFAULT_INTENT_FALLBACK
            changed = True
        if row.disable_linkedin is None:
            row.disable_linkedin = DISABLE_LINKEDIN_DEFAULT
            changed = True
        if row.require_context_for_email is None:
            row.require_context_for_email = REQUIRE_CONTEXT_FOR_EMAIL_DEFAULT
            changed = True
        if changed:
            row.updated_at = _utcnow_iso()
            session.add(row)
            session.commit()
            session.refresh(row)
        return row


def update_settings(
    *,
    recency_days: Optional[int] = None,
    max_news_items: Optional[int] = None,
    max_discovered_urls: Optional[int] = None,
    search_max_results: Optional[int] = None,
    disable_web_search: Optional[bool] = None,
    linkedin_post_limit: Optional[int] = None,
    default_intent: Optional[str] = None,
    disable_linkedin: Optional[bool] = None,
    require_context_for_email: Optional[bool] = None,
) -> AppSettings:
    with get_session() as session:
        row = session.get(AppSettings, SINGLETON_ID) or _bootstrap_from_env()
        if recency_days is not None:
            row.recency_days = recency_days
        if max_news_items is not None:
            row.max_news_items = max_news_items
        if max_discovered_urls is not None:
            row.max_discovered_urls = max_discovered_urls
        if search_max_results is not None:
            row.search_max_results = search_max_results
        if disable_web_search is not None:
            row.disable_web_search = disable_web_search
        if linkedin_post_limit is not None:
            row.linkedin_post_limit = linkedin_post_limit
        if default_intent is not None:
            row.default_intent = default_intent.strip() or DEFAULT_INTENT_FALLBACK
        if disable_linkedin is not None:
            row.disable_linkedin = disable_linkedin
        if require_context_for_email is not None:
            row.require_context_for_email = require_context_for_email
        row.updated_at = _utcnow_iso()
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def reset_settings() -> AppSettings:
    """Wipe DB row, re-bootstrap from .env."""
    with get_session() as session:
        row = session.get(AppSettings, SINGLETON_ID)
        if row is not None:
            session.delete(row)
            session.commit()
    return get_settings()
