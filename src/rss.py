"""RSS/Atom feed discovery and parsing.

Adapted from C:/tasks/web_scraping/company_news_scraper/discovery.py. RSS is the
single highest-quality news source we have: items are XML-structured, dates and
URLs are not LLM-extracted (so no hallucination), and most companies that
publish a blog also expose a feed at a predictable path.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Optional
from urllib.parse import urljoin, urlparse

from dateutil import parser as dateparser

from .http_headers import request_headers
from .schemas import NewsItem

logger = logging.getLogger(__name__)

# Paths to probe when the homepage doesn't declare a feed via <link>.
_RSS_PATHS = (
    "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
    "/feeds/posts/default", "/blog/feed", "/news/feed",
    "/blog/rss", "/news/rss", "/blog/rss.xml", "/news/rss.xml",
    "/index.xml", "/feed/atom", "/rss/news", "/feed/rss",
)

# Match a <link> tag with type=application/rss+xml (and variants). Handles
# href-before-type and type-before-href; HTML attribute order isn't fixed.
_LINK_TYPE_HREF = re.compile(
    r'<link[^>]*type=["\'](?:application/(?:rss|atom)\+xml|application/xml|text/xml)["\']'
    r'[^>]*href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_LINK_HREF_TYPE = re.compile(
    r'<link[^>]*href=["\']([^"\']+)["\']'
    r'[^>]*type=["\'](?:application/(?:rss|atom)\+xml|application/xml|text/xml)["\']',
    re.IGNORECASE,
)


def _base_url(company_url: str) -> str:
    p = urlparse(company_url)
    scheme = p.scheme or "https"
    host = p.netloc or p.path  # tolerate bare-host input
    return f"{scheme}://{host}"


def _looks_like_feed(text: str, content_type: str) -> bool:
    if any(t in content_type for t in ("xml", "rss", "atom")):
        return True
    head = text[:500].lstrip()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head


def discover_rss_feeds(company_url: str) -> list[str]:
    """Find RSS/Atom feed URLs for the company.

    Strategy 1: parse the homepage for ``<link rel="alternate" type="application/rss+xml">``.
    Strategy 2: probe common feed paths (concurrent).
    Returns deduped list of confirmed feed URLs (empty on failure — caller can ignore).
    """
    import requests  # lazy

    base = _base_url(company_url)
    headers = request_headers(accept_xml=True)
    feeds: list[str] = []
    seen: set[str] = set()

    # 1. Homepage <link> tags
    try:
        resp = requests.get(base, headers=headers, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            for pattern in (_LINK_TYPE_HREF, _LINK_HREF_TYPE):
                for m in pattern.finditer(resp.text):
                    full = urljoin(base, m.group(1))
                    if full not in seen:
                        seen.add(full)
                        feeds.append(full)
    except Exception as e:
        logger.debug("rss: homepage fetch failed for %s: %s", base, e)

    # 2. Probe known paths in parallel
    def _probe(path: str) -> Optional[str]:
        url = base.rstrip("/") + path
        try:
            r = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        ct = r.headers.get("content-type", "").lower()
        if _looks_like_feed(r.text, ct):
            return str(r.url)
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for url in pool.map(_probe, _RSS_PATHS):
            if url and url not in seen:
                seen.add(url)
                feeds.append(url)

    if feeds:
        logger.info("rss: discovered %d feed(s) for %s", len(feeds), base)
    return feeds


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _find_child_text(elem, *names: str) -> Optional[str]:
    for child in elem:
        if _strip_ns(child.tag) in names and child.text:
            return child.text.strip()
    return None


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()


def parse_rss_feed(xml_text: str, feed_url: str = "") -> list[dict]:
    """Parse RSS 2.0 or Atom feed XML into a list of news item dicts.

    Returned dicts have: title, url, date (raw string), summary.
    Items missing title or url are skipped.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("rss: failed to parse XML from %s: %s", feed_url, e)
        return []

    items: list[dict] = []
    root_tag = _strip_ns(root.tag).lower()

    if root_tag == "rss":
        for it in root.iter():
            if _strip_ns(it.tag).lower() != "item":
                continue
            title = _find_child_text(it, "title")
            link = _find_child_text(it, "link")
            date_str = _find_child_text(it, "pubDate", "date", "published")
            summary = _find_child_text(it, "description", "summary", "content")
            if title and link:
                items.append({
                    "title": _strip_html(title),
                    "url": link,
                    "date": date_str,
                    "summary": _strip_html(summary or ""),
                })
    elif root_tag == "feed":
        # Atom: <link> uses href attr, prefer rel="alternate"
        for entry in root.iter():
            if _strip_ns(entry.tag).lower() != "entry":
                continue
            title = _find_child_text(entry, "title")
            link: Optional[str] = None
            for child in entry:
                if _strip_ns(child.tag).lower() != "link":
                    continue
                href = child.get("href")
                if not href:
                    continue
                rel = child.get("rel", "alternate")
                if rel in ("alternate", "", None):
                    link = href
                    break
                if not link:
                    link = href
            date_str = _find_child_text(entry, "published", "updated", "date")
            summary = _find_child_text(entry, "summary", "content", "description")
            if title and link:
                items.append({
                    "title": _strip_html(title),
                    "url": link,
                    "date": date_str,
                    "summary": _strip_html(summary or ""),
                })

    logger.debug("rss: parsed %d item(s) from %s", len(items), feed_url)
    return items


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return dateparser.parse(s).date()
    except (ValueError, TypeError, OverflowError):
        return None


def fetch_rss_items(company_url: str) -> list[NewsItem]:
    """Discover the company's RSS feeds and return parsed NewsItem objects.

    Items missing title/url/date are dropped silently. The schema's URL validator
    also rejects placeholder hosts. Dedupes by title (case-insensitive).
    """
    import requests  # lazy

    feed_urls = discover_rss_feeds(company_url)
    if not feed_urls:
        return []

    raw: list[dict] = []
    for url in feed_urls:
        try:
            # Fresh headers per feed so UA rotation actually rotates.
            resp = requests.get(url, headers=request_headers(accept_xml=True), timeout=15, allow_redirects=True)
            if resp.status_code == 200:
                raw.extend(parse_rss_feed(resp.text, url))
        except Exception as e:
            logger.warning("rss: failed to fetch %s: %s", url, e)

    out: list[NewsItem] = []
    seen_titles: set[str] = set()
    for r in raw:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        d = _parse_date(r.get("date"))
        if not title or not url or d is None:
            continue
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        summary = (r.get("summary") or title)[:500]
        try:
            out.append(NewsItem(
                title=title,
                summary=summary,
                published_date=d,
                url=url,
                category="news",
            ))
        except Exception as e:
            logger.debug("rss: dropping invalid item %r: %s", title, e)

    logger.info("rss: yielded %d valid item(s) for %s", len(out), company_url)
    return out
