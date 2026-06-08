from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .config import APIFY_LINKEDIN_ACTOR, APIFY_LINKEDIN_POST_LIMIT, APIFY_TOKEN, CACHE_ROOT, domain_slug
from .recency import parse_date
from .schemas import LinkedInPost

logger = logging.getLogger(__name__)

LINKEDIN_CACHE_DIR = CACHE_ROOT / "linkedin"
LINKEDIN_CACHE_HOURS = int(os.getenv("LINKEDIN_CACHE_HOURS", "24"))


def _cache_path(linkedin_url: str) -> Path:
    LINKEDIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return LINKEDIN_CACHE_DIR / f"{domain_slug(linkedin_url)}_{abs(hash(linkedin_url)) % 1_000_000:06d}.json"


def _load_cache(linkedin_url: str) -> list[dict] | None:
    path = _cache_path(linkedin_url)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at > timedelta(hours=LINKEDIN_CACHE_HOURS):
            return None
        return payload.get("raw_posts") or []
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.debug("LinkedIn cache read failed for %s: %s", linkedin_url, e)
        return None


def _save_cache(linkedin_url: str, raw_posts: list[dict]) -> None:
    path = _cache_path(linkedin_url)
    tmp = path.with_suffix(".tmp")
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "raw_posts": raw_posts}
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    tmp.replace(path)


def _extract_author_name(author_field: Any) -> Optional[str]:
    """Author can be a plain name string, {'name': ...}, or {'first_name': ..., 'last_name': ...}."""
    if isinstance(author_field, str):
        return author_field
    if not isinstance(author_field, dict):
        return None
    if author_field.get("name"):
        return author_field["name"]
    if author_field.get("authorName"):
        return author_field["authorName"]
    parts = [author_field.get("first_name"), author_field.get("last_name")]
    joined = " ".join(p for p in parts if p).strip()
    return joined or None


def _parse_posted_at(raw: Any) -> Optional[Any]:
    """The actor wraps the date in a dict {date, relative, timestamp}; other actors send a flat value."""
    if isinstance(raw, dict):
        return parse_date(raw.get("date")) or parse_date(raw.get("timestamp"))
    return parse_date(raw)


def _coerce_apify_post(raw: dict[str, Any]) -> LinkedInPost | None:
    """Map one Apify actor record to a LinkedInPost. Tolerates multiple actor schemas:
    snake_case + nested (this actor) and camelCase + flat (older / alternative actors)."""
    post_type = (raw.get("type") or raw.get("postType") or raw.get("post_type") or "").lower()
    reposted = (
        raw.get("repostedPost")
        or raw.get("reshared")
        or raw.get("reshared_post")
        or raw.get("resharedPost")
        or {}
    )
    is_repost = (
        post_type in {"repost", "reshare", "share", "reshared"}
        or bool(reposted)
    )

    user_commentary = (
        raw.get("commentary")
        or raw.get("repostCommentary")
        or raw.get("repost_commentary")
        or raw.get("userText")
    )

    if is_repost:
        if reposted:
            # Older actors nest the original post under repostedPost / reshared / etc.
            original_text = (
                reposted.get("text") or reposted.get("postText") or reposted.get("content") or ""
            ) if isinstance(reposted, dict) else ""
            original_author = _extract_author_name(reposted.get("author") if isinstance(reposted, dict) else None)
        else:
            # Flat-shape actors (e.g. LQQIXN9Othf8f7R5n) collapse the original into
            # the top-level fields: `text` is the original post's body, `author` is
            # the original poster. No separate `repostedPost` object is emitted.
            original_text = raw.get("text") or raw.get("postText") or raw.get("content") or ""
            original_author = _extract_author_name(raw.get("author"))
        text = (user_commentary or "").strip() or original_text
    else:
        text = raw.get("text") or raw.get("postText") or raw.get("content") or ""
        original_author = None
        user_commentary = None

    posted = _parse_posted_at(
        raw.get("postedAt")
        or raw.get("publishedAt")
        or raw.get("date")
        or raw.get("postedAtTimestamp")
        or raw.get("posted_at")
    )
    if not text or posted is None:
        return None

    url = raw.get("url") or raw.get("postUrl") or raw.get("permalink")

    # Engagement: try flat fields first, then nested `stats` (this actor's shape).
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    likes = (
        raw.get("likesCount")
        or raw.get("numLikes")
        or raw.get("totalReactionCount")
        or stats.get("total_reactions")
        or stats.get("totalReactions")
    )
    comments = (
        raw.get("commentsCount")
        or raw.get("numComments")
        or stats.get("comments_count")
        or stats.get("commentsCount")
        or stats.get("comments")
    )
    engagement = None
    if likes is not None or comments is not None:
        engagement = f"{likes or 0} likes, {comments or 0} comments"

    try:
        return LinkedInPost(
            text=text,
            posted_date=posted,
            url=url,
            engagement=engagement,
            is_repost=is_repost,
            original_author=original_author,
            user_commentary=(user_commentary.strip() if isinstance(user_commentary, str) and user_commentary.strip() else None),
        )
    except Exception:
        return None


def _extract_linkedin_username(url: str) -> str | None:
    """Pull the slug from a LinkedIn profile URL: https://www.linkedin.com/in/<slug>/ -> <slug>."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "in":
        slug = parts[1]
        if re.match(r"^[A-Za-z0-9_\-%.]+$", slug):
            return slug
    return None


def _apify_fetch_raw(linkedin_url: str, post_limit: Optional[int] = None) -> list[dict]:
    """Call the Apify actor and return the raw post dicts (unfiltered)."""
    try:
        from apify_client import ApifyClient
    except ImportError:
        logger.warning("apify-client not installed; cannot fetch LinkedIn.")
        return []

    username = _extract_linkedin_username(linkedin_url)
    if not username:
        logger.warning("Could not extract LinkedIn username from %s", linkedin_url)
        return []

    client = ApifyClient(APIFY_TOKEN)
    actor = client.actor(APIFY_LINKEDIN_ACTOR)
    limit = post_limit if post_limit is not None else APIFY_LINKEDIN_POST_LIMIT
    run_input = {
        "username": username,
        "profileUrls": [linkedin_url],
        "page_number": 1,
        "limit": limit,
    }
    try:
        run = actor.call(run_input=run_input)
    except Exception as e:
        logger.warning("Apify LinkedIn actor %s failed: %s", APIFY_LINKEDIN_ACTOR, e)
        return []

    raw_posts: list[dict] = []
    for record in client.dataset(run["defaultDatasetId"]).iterate_items():
        # This actor returns one post per dataset record. Some other LinkedIn actors
        # nest posts under "posts"/"activity" — handle both shapes.
        if isinstance(record, dict) and ("posts" in record or "activity" in record):
            for raw_post in record.get("posts") or record.get("activity") or []:
                raw_posts.append(raw_post)
        elif isinstance(record, dict):
            raw_posts.append(record)
    return raw_posts


def fetch_linkedin(linkedin_url: str | None, warnings: list[str],
                   post_limit: Optional[int] = None) -> list[LinkedInPost]:
    """Fetch + map LinkedIn posts. Apify-only path; uses 24h disk cache."""
    if not linkedin_url:
        return []

    if not APIFY_TOKEN:
        warnings.append("APIFY_TOKEN not set; skipping LinkedIn fetch.")
        return []

    raw_posts = _load_cache(linkedin_url)
    cache_hit = raw_posts is not None
    if not cache_hit:
        raw_posts = _apify_fetch_raw(linkedin_url, post_limit=post_limit)
        if raw_posts:
            _save_cache(linkedin_url, raw_posts)

    if not raw_posts:
        warnings.append("LinkedIn fetch returned no posts.")
        return []

    posts: list[LinkedInPost] = []
    for raw in raw_posts:
        p = _coerce_apify_post(raw)
        if p is not None:
            posts.append(p)

    logger.info(
        "LinkedIn fetch: %d raw → %d mapped (cache_hit=%s)",
        len(raw_posts),
        len(posts),
        cache_hit,
    )
    return posts


def is_pure_repost(post: LinkedInPost) -> bool:
    """A repost with no commentary added by the user."""
    return post.is_repost and not (post.user_commentary or "").strip()
