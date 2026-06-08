from __future__ import annotations

from datetime import date
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

NewsCategory = Literal["news", "blog", "award", "funding", "launch", "press", "other"]

# Sentiment polarity for an extracted news item. Orthogonal to ``NewsCategory``:
# a funding round can be "positive", a missed earnings call "negative", and an
# industry roundup featuring the company "neutral". The LLM populates this at
# extraction time; downstream callers (relevance scorer, email writer) can
# weight it as they choose. Default is "neutral" — when the LLM doesn't or
# can't classify, we don't want to bias for or against the item.
NewsSentiment = Literal["positive", "neutral", "negative"]

# How substantive the underlying event is, from a cold-email-anchor perspective.
# Sentiment tells us if the news is good/bad; significance tells us if it's a
# REAL event worth mentioning to a prospect. A legal-entity rename to support
# an IPO ("Lenskart Solutions Limited Rebrands") is sentiment=positive but
# significance=administrative — customers don't see it, an SDR who quotes it
# sounds tone-deaf. Conversely a $200M funding round is significance=major.
# Both axes are needed: weight by sentiment AND significance later, so we
# don't reach out about a back-office filing OR a controversy.
#
#  - major:          funding round, acquisition, major launch/partnership, key hire
#  - notable:        incremental product news, smaller partnership, industry recognition
#  - routine:        scheduled earnings, conference talk, executive interview
#  - administrative: legal-entity rename, ticker change, board reshuffle
#  - peripheral:     passing mention in roundup, opinion column, market analysis
#  - negative:       controversy, layoff, lawsuit, breach, missed earnings
EventSignificance = Literal[
    "major", "notable", "routine", "administrative", "peripheral", "negative",
]

# Hosts the LLM falls back to when it can't find a real link. RFC 2606 reserves
# example.* for documentation/placeholders; localhost is obviously fabricated.
_PLACEHOLDER_HOSTS = frozenset({
    "example.com", "example.org", "example.net", "example.edu",
    "localhost", "yourdomain.com", "yoursite.com", "placeholder.com",
})


class DiscoveredUrls(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=12)


class NewsItem(BaseModel):
    title: str
    summary: str = Field(description="2-3 sentence summary")
    published_date: date = Field(description="ISO-8601 date the item was published")
    url: str
    category: NewsCategory = "news"
    # Phase 1: classification only — we populate this from the LLM but do NOT
    # yet factor it into relevance scoring. The next phase will introduce
    # weights in src/relevance.py once we trust the LLM's classifications.
    # Default "neutral" so callers that don't set sentiment (JSON-LD-only
    # extraction, RSS, legacy paths) don't accidentally get biased ranking.
    sentiment: NewsSentiment = "neutral"
    # Phase 2a: classification only. Default "notable" so unknown-significance
    # items don't get over- or under-promoted in ranking — they sit at the
    # mid-tier and tie-break on other signals (recency, sender-fit, etc.).
    # Populated by the same LLM call that does sentiment, no extra cost.
    event_significance: EventSignificance = "notable"

    @field_validator("url")
    @classmethod
    def _reject_placeholder_urls(cls, v: str) -> str:
        host = (urlparse(v).hostname or "").lower().removeprefix("www.")
        if host in _PLACEHOLDER_HOSTS:
            raise ValueError(f"URL host {host!r} looks like an LLM placeholder")
        # Reject hostnames that aren't real domains. A hostname without a dot
        # (e.g. "in" from a mangled relative URL like "in/newsroom/...") cannot
        # resolve to a website — Pydantic was accepting these because urlparse
        # happily treated single-segment paths as hostnames.
        if not host or "." not in host:
            raise ValueError(f"URL host {host!r} is not a valid domain (no dot)")
        return v


class NewsItemList(BaseModel):
    items: list[NewsItem] = Field(default_factory=list)


class LinkedInPost(BaseModel):
    text: str
    posted_date: date
    url: Optional[str] = None
    engagement: Optional[str] = None
    is_repost: bool = False
    original_author: Optional[str] = None
    user_commentary: Optional[str] = None
    relevance_score: Optional[float] = None
    topics: list[str] = Field(default_factory=list)
    # Phase 2a: same axes as NewsItem so news and LinkedIn posts can be compared
    # on a single unified scale when we add cross-source ranking later.
    # ``relevance_score`` (existing) is "how anchor-able is THIS post for THIS
    # recipient"; ``sentiment`` and ``event_significance`` are the same content
    # properties we tag on news. Default "notable" / "neutral" so legacy callers
    # don't bias rankings either way.
    sentiment: NewsSentiment = "neutral"
    event_significance: EventSignificance = "notable"


class LinkedInPostList(BaseModel):
    posts: list[LinkedInPost] = Field(default_factory=list)


SourceStatus = Literal["ok", "empty", "skipped", "error"]


class ResearchBundle(BaseModel):
    company_url: str
    linkedin_url: Optional[str] = None
    items: list[NewsItem] = Field(default_factory=list)
    linkedin_posts: list[LinkedInPost] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0
    news_status: SourceStatus = "empty"
    news_status_detail: Optional[str] = None
    linkedin_status: SourceStatus = "skipped"
    linkedin_status_detail: Optional[str] = None
