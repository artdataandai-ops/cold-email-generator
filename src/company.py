from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

from .config import MAX_DISCOVERED_URLS_DEFAULT, RECENCY_DAYS, build_graph_config
from .cost_guard import CostMeter, estimate_call_usd
from .http_headers import request_headers
from .schemas import DiscoveredUrls, NewsItem, NewsItemList

logger = logging.getLogger(__name__)

# Path keywords that almost every company puts news/content under.
# Match is case-insensitive substring on the URL path.
_NEWS_PATH_KEYWORDS = (
    "news", "blog", "press", "newsroom", "announcements",
    "insights", "insight", "resources", "resource", "articles", "article",
    "case-stud", "case_stud", "casestud", "stories", "story",
    "whitepapers", "whitepaper", "library", "knowledge",
    "perspectives", "thinking", "media",
)
_NEWS_PATH_RE = re.compile(r"/(?:" + "|".join(_NEWS_PATH_KEYWORDS) + r")", re.IGNORECASE)

_DISCOVERY_PROMPT = (
    "From the navigation, footer, and body of this page, return up to 12 absolute URLs "
    "that point to pages where this company publishes news, blog posts, press releases, "
    "announcements, industry insights, resources, articles, case studies, whitepapers, "
    "stories, or perspectives. Include both listing pages (e.g. /blog) AND individual "
    "recent article pages if linked. Prefer URLs on the company's own domains "
    "(including sibling TLDs like .ai/.io/.co versions of the brand). "
    "Output JSON: {urls: [...]}."
)

# Probed when sitemap + LLM discovery both come up empty. Built without fetching;
# the downstream extractor handles 404s as empty results.
#
# The "/<region>/..." entries handle global sites that put news under a locale prefix.
# We try common English locales (in, en, us, uk, eu) — each adds a small fixed cost.
_COMMON_NEWS_PATHS = (
    # ── Tier 1: canonical newsroom paths. These have the highest hit rate AND
    # the highest signal-quality. Ordered first so cap=8 doesn't exclude them.
    "/news", "/blog", "/press", "/newsroom",
    "/press-releases", "/press-room",
    "/company/press-releases", "/company/news", "/company/newsroom",
    # ── Tier 2: secondary newsroom-ish paths used by larger / corporate sites.
    "/media-center", "/media-centre", "/about/news", "/about/newsroom",
    "/about/press", "/investors/news", "/investor-relations/news",
    # ── Tier 3: marketing/insight paths — usually NOT a real news listing but
    # some companies file news under them.
    "/announcements", "/insights", "/resources", "/articles",
    "/case-studies", "/stories", "/media",
    # ── Tier 4: region-prefixed variants (Stripe's /in/, /en/, etc.) — likely
    # to be probed only when the company has explicit region routing.
    "/en/news", "/en/newsroom", "/en/press", "/en/insights",
    "/in/news", "/in/newsroom", "/in/blog",
    "/us/news", "/us/newsroom", "/uk/news", "/uk/newsroom",
)

# Words we look for in <a> hrefs and link text when scanning the homepage. Wider
# than _COMMON_NEWS_PATHS — covers natural-language link labels like "What's new".
_ANCHOR_KEYWORDS = (
    "news", "blog", "press", "media", "insights", "insight", "updates",
    "announcements", "newsroom", "stories", "story", "articles", "article",
    "latest", "whats-new", "what-s-new", "media-center", "press-releases",
    "case-studies", "case-stud", "perspectives", "thinking", "resources",
)

# Sentinel mount points emitted by SPA frameworks before JS hydration. Same set
# as src/extractor.py uses — when a probed listing page is just a framework shell,
# we keep it (instead of dropping at the listing-score check) so the downstream
# extractor's httpx → Playwright escalation can render it. Without this, real SPA
# newsrooms like paymentology.com/newsroom never reach extraction.
_SPA_SHELL_PATTERN = re.compile(
    r'<div[^>]+id=["\']?(?:root|app|__next|__nuxt|svelte)\b', re.IGNORECASE,
)


def _looks_like_spa_shell(html: str) -> bool:
    """Cheap signal: page contains a known SPA framework mount point AND its
    static HTML is small (the real content gets rendered client-side)."""
    if not html or not _SPA_SHELL_PATTERN.search(html):
        return False
    # Plain page-size heuristic — full server-rendered sites are usually > 50KB.
    return len(html) < 40_000


# Date strings that suggest a page is a real news listing.
_LISTING_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s*\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)


def _registrable_name(host: str) -> str:
    """Return the second-level brand name from a host. thredd.com -> 'thredd', news.acme.co.uk -> 'acme'."""
    host = (host or "").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) <= 1:
        return host
    # Handle common 2-part TLDs (.co.uk, .com.au, .co.jp).
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2]


def _same_brand(seed_url: str, candidate_url: str) -> bool:
    """Same company across sibling TLDs/subdomains. thredd.com matches thredd.ai matches blog.thredd.com."""
    seed_host = urlparse(seed_url).hostname or ""
    cand_host = urlparse(candidate_url).hostname or ""
    if not cand_host:
        return False
    seed_brand = _registrable_name(seed_host)
    cand_brand = _registrable_name(cand_host)
    # Require both brands to be non-empty and identical. (Avoid matching "" with "".)
    return bool(seed_brand) and seed_brand == cand_brand


def _score_listing_page(html: str) -> int:
    """Score how strongly a fetched page looks like a news/blog listing.

    Counts structural signals: <article> tags, headings, date strings, link
    density. The threshold for "this is a real listing" lives in the caller.
    """
    try:
        from bs4 import BeautifulSoup  # lazy
    except ImportError:
        return 0
    soup = BeautifulSoup(html, "html.parser")
    score = 0
    score += len(soup.find_all("article")) * 2
    score += len(soup.find_all("h2"))
    score += len(soup.find_all("h3"))
    score += len(_LISTING_DATE_RE.findall(html)) * 2
    if len(soup.find_all("a", href=True)) > 15:
        score += 5
    return score


def _homepage_anchor_scan(company_url: str) -> list[tuple[str, int]]:
    """Fetch homepage, find same-brand links whose href or text mention news keywords.

    Returns ``[(url, score), ...]`` sorted by score descending. Score rewards keywords
    appearing in the URL path (more reliable signal) over just the link text.

    Empty list on any HTTP failure — the caller falls through to the next strategy.
    """
    try:
        from bs4 import BeautifulSoup  # lazy
        import requests  # lazy
    except ImportError:
        return []

    headers = request_headers()
    try:
        resp = requests.get(company_url, headers=headers, timeout=10, allow_redirects=True)
    except Exception as e:
        logger.debug("anchor_scan: homepage fetch failed for %s: %s", company_url, e)
        return []
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    base = str(resp.url)
    candidates: dict[str, int] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base, href).split("#", 1)[0]
        if not _same_brand(company_url, full):
            continue
        href_lower = full.lower()
        text_lower = (a.get_text() or "").strip().lower()
        path = (urlparse(full).path or "/").lower()

        # Score: prefer matches in the URL path (most reliable), then text labels.
        for kw in _ANCHOR_KEYWORDS:
            in_path = kw in path
            in_text = kw in text_lower
            if not (in_path or in_text):
                continue
            score = 5
            if in_path:
                score += 3
            # Penalty for URLs that look like an article-slug rather than a listing.
            segments = [s for s in path.split("/") if s]
            if segments and segments[-1].count("-") >= 2 and len(segments) >= 2:
                score -= 4
            # Penalty for deeply nested URLs (listings are usually shallow).
            score -= max(0, (len(segments) - 2) * 2)
            candidates[full] = max(candidates.get(full, 0), score)
            break  # one keyword match per link is enough

    ranked = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)
    if ranked:
        logger.info(
            "anchor_scan: %d candidate(s) from %s (top: %s @ %d)",
            len(ranked), company_url, ranked[0][0], ranked[0][1],
        )
    return ranked


def _validate_listing_candidates(candidates: list[str], min_score: int = 3) -> list[str]:
    """Fetch each URL, score it via _score_listing_page, keep those above threshold.
    Parallel HTTP. On network errors we keep the URL (Cloudflare-walled sites still
    deserve a Playwright try downstream).
    """
    if not candidates:
        return []
    try:
        import requests  # lazy
        from concurrent.futures import ThreadPoolExecutor
    except ImportError:
        return candidates

    def _check(url: str) -> Optional[str]:
        # Per-call fresh headers so UA rotation actually rotates across the pool.
        try:
            r = requests.get(url, headers=request_headers(), timeout=8, allow_redirects=True)
        except Exception:
            return url  # network blip ≠ "not a listing"
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            return url  # 403/401/5xx etc — pass through to Playwright
        ct = r.headers.get("content-type", "").lower()
        if "html" not in ct and not r.text.lstrip().startswith("<"):
            return None
        # Real SPA newsrooms (Paymentology /newsroom etc.) score < threshold on
        # their static shells. Keep them so the extractor's Playwright fallback
        # can render them downstream.
        if _looks_like_spa_shell(r.text):
            return url
        return url if _score_listing_page(r.text) >= min_score else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [u for u in pool.map(_check, candidates) if u]


def _sitemap_paths_from_robots(base: str, headers: dict) -> list[str]:
    """Read /robots.txt and extract any ``Sitemap: <url>`` directives.
    Returns absolute URLs in robots-declared order. Empty list if no robots.txt
    or no Sitemap line. Falls back gracefully so non-RFC-compliant sites still
    work via the fixed-path probes."""
    import requests  # lazy
    try:
        resp = requests.get(urljoin(base, "/robots.txt"), headers=headers, timeout=6)
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    sitemaps: list[str] = []
    for line in resp.text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() == "sitemap":
            url = value.strip()
            if url and url not in sitemaps:
                sitemaps.append(url)
    return sitemaps


def _parse_sitemap_locs(xml: str) -> list[tuple[str, Optional[str]]]:
    """Extract ``(loc, lastmod)`` tuples from a sitemap XML body. ``lastmod``
    is None when the entry doesn't declare one.

    Uses regex over <url>…</url> blocks (and <sitemap>…</sitemap> for indexes)
    so we don't need an XML parser — sitemaps in the wild often have small
    well-formedness issues that ET.fromstring trips on.
    """
    out: list[tuple[str, Optional[str]]] = []
    # Match either <url>…</url> or <sitemap>…</sitemap> blocks.
    for block in re.finditer(r"<(?:url|sitemap)\b[^>]*>(.*?)</(?:url|sitemap)>",
                              xml, flags=re.IGNORECASE | re.DOTALL):
        body = block.group(1)
        loc_match = re.search(r"<loc>\s*([^<\s]+)\s*</loc>", body, flags=re.IGNORECASE)
        if not loc_match:
            continue
        loc = loc_match.group(1)
        lastmod_match = re.search(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", body, flags=re.IGNORECASE)
        out.append((loc, lastmod_match.group(1) if lastmod_match else None))
    return out


def _try_sitemap_with_lastmod(company_url: str) -> list[tuple[str, Optional[str]]]:
    """Like ``_try_sitemap`` but returns ``[(url, lastmod), ...]`` tuples so callers
    can use the sitemap-declared lastmod as a fallback publish-date downstream.

    See ``_try_sitemap`` for the discovery strategy."""
    import requests  # lazy
    base = f"{urlparse(company_url).scheme or 'https'}://{urlparse(company_url).hostname}"
    headers = request_headers(accept_xml=True)

    # Tier A: robots.txt-advertised sitemap paths (RFC 9309).
    robots_sitemaps = _sitemap_paths_from_robots(base, headers)
    # Tier B: conventional fallbacks. De-dup against tier A.
    seen_candidates: set[str] = set(robots_sitemaps)
    fallbacks = [
        urljoin(base, p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")
        if urljoin(base, p) not in seen_candidates
    ]
    sitemap_urls = robots_sitemaps + fallbacks

    entries: list[tuple[str, Optional[str]]] = []  # (loc, lastmod) collected across all maps
    for sm_url in sitemap_urls:
        try:
            resp = requests.get(sm_url, headers=headers, timeout=8)
        except Exception as e:
            logger.debug("sitemap fetch failed for %s: %s", sm_url, e)
            continue
        if resp.status_code != 200 or "<" not in resp.text:
            continue

        parsed = _parse_sitemap_locs(resp.text)
        sub_sitemaps = [u for u, _ in parsed if u.lower().endswith(".xml")]
        article_entries = [(u, lm) for u, lm in parsed if not u.lower().endswith(".xml")]
        entries.extend(article_entries)

        # Follow up to 5 child sitemaps IN PARALLEL — used to be serial, which on
        # large sitemap indexes (Paymentology has many) added 5-8s per company.
        if sub_sitemaps:
            from concurrent.futures import ThreadPoolExecutor

            def _fetch_sub(sub_url: str) -> list[tuple[str, Optional[str]]]:
                try:
                    sub_resp = requests.get(sub_url, headers=headers, timeout=8)
                except Exception:
                    return []
                if sub_resp.status_code != 200:
                    return []
                return [(u, lm) for u, lm in _parse_sitemap_locs(sub_resp.text)
                        if not u.lower().endswith(".xml")]

            with ThreadPoolExecutor(max_workers=5) as pool:
                for sub_entries in pool.map(_fetch_sub, sub_sitemaps[:5]):
                    entries.extend(sub_entries)

        # If we already got useful entries from this sitemap, stop trying more.
        if entries:
            break

    # Filter to news-like paths only.
    news_entries = [(u, lm) for u, lm in entries if _NEWS_PATH_RE.search(urlparse(u).path or "")]

    # Sort by lastmod descending — None goes last (treated as oldest).
    # Lexicographic sort works for ISO-8601 strings (the standard sitemap format).
    news_entries.sort(key=lambda kv: kv[1] or "", reverse=True)

    if news_entries:
        logger.info("sitemap discovery: %d news-like URL(s)", len(news_entries))
    return news_entries


def _try_sitemap(company_url: str) -> list[str]:
    """Backwards-compatible wrapper that drops lastmod values. Use
    ``_try_sitemap_with_lastmod`` when you need the lastmod for fallback dating."""
    return [u for u, _ in _try_sitemap_with_lastmod(company_url)]


def _build_probe_urls(company_url: str, cap: Optional[int] = None) -> list[str]:
    """Pure URL construction. No network.

    Returns ALL probe URLs when ``cap`` is None or larger than the path list — we
    want to validate every candidate and only truncate AT THE END to the cap of
    survivors. (Old behaviour sliced first, so newly-added paths at the bottom
    of ``_COMMON_NEWS_PATHS`` were never tried for typical caps of 8.)
    """
    if cap is not None and cap <= 0:
        return []
    parsed = urlparse(company_url)
    base = f"{parsed.scheme or 'https'}://{parsed.hostname}"
    paths = _COMMON_NEWS_PATHS if cap is None or cap >= len(_COMMON_NEWS_PATHS) else _COMMON_NEWS_PATHS[:cap]
    return [base + p for p in paths]


def _validate_probe_urls(urls: list[str]) -> list[str]:
    """HTTP-validate candidates; drop 404/410. Network errors keep the URL —
    bot-walled sites (Cloudflare 403 to plain requests) still deserve Playwright."""
    if not urls:
        return []
    import requests  # lazy
    from concurrent.futures import ThreadPoolExecutor

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    def _check(url: str) -> Optional[str]:
        try:
            r = requests.get(url, headers=headers, timeout=6, allow_redirects=True)
        except Exception:
            return url
        if r.status_code in (404, 410):
            return None
        return url

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [u for u in pool.map(_check, urls) if u]


def _probe_common_paths(company_url: str, cap: int) -> list[str]:
    """Build candidate URLs by appending common newsroom paths, then validate via HTTP
    so we don't ship 404 URLs to scrapegraph's expensive LLM step.

    Tries the full path list (~30 candidates, parallel httpx checks) and returns
    up to ``cap`` survivors. The full sweep is cheap because validation is
    ThreadPoolExecutor-parallel; capping survivors keeps downstream extraction cost
    bounded.
    """
    return _validate_probe_urls(_build_probe_urls(company_url))[:cap]


def discover_pages(
    company_url: str,
    meter: CostMeter,
    max_urls: Optional[int] = None,
    *,
    url_metadata: Optional[dict] = None,
) -> list[str]:
    """Find pages on the company's site that publish news / blog / insights.

    All three cheap tiers run unconditionally; cap is applied AFTER merging:
      1) Homepage anchor scan — links the company explicitly exposes in its nav.
      2) Sitemap (robots.txt-advertised + conventional paths). Now sorted by
         <lastmod> descending; LEAF article URLs get up to 3 priority slots.
      3) Common-path probe — fixed canonical newsroom paths, HTTP-validated.
      4) LLM via scrapegraphai — last resort only if 1+2+3 all returned nothing.

    Merge order: sitemap-leaves → anchor scan → probe → remaining sitemap → cap.
    Sitemap-leaf URLs are specific recent articles; the rest are listing pages
    where downstream extraction depends on the LLM picking the right items.

    ``url_metadata`` is an optional mutable dict that, when provided, gets populated
    with ``{url_no_trailing_slash: {"lastmod": "<iso>", "source": "sitemap"}}`` for
    every sitemap-discovered URL. Downstream extraction can use this lastmod as a
    fallback published-date when the LLM can't find one on the page.
    """
    cap = max_urls if max_urls is not None else MAX_DISCOVERED_URLS_DEFAULT
    seen: set[str] = set()

    def _dedupe_extend(target: list[str], new_urls) -> None:
        for url in new_urls:
            norm = url.rstrip("/")
            if norm not in seen:
                seen.add(norm)
                target.append(url)

    # All three cheap tiers run ALWAYS. The cap is applied at the end after merging
    # so canonical newsroom paths (Paymentology's /newsroom etc.) get a fair shot
    # even when anchor scan already returned plenty. Previously tier 3 was gated on
    # `len(found) < cap`, so an anchor-scan-heavy homepage starved out the probe.
    from_anchor: list[str] = []
    from_sitemap: list[str] = []
    from_probe: list[str] = []

    # 1. Homepage anchor scan — links the company actually exposes in its nav.
    _dedupe_extend(from_anchor, (url for url, _score in _homepage_anchor_scan(company_url)))

    # 2. Sitemap — same-brand newsy URLs from <loc> entries, with lastmod stashed
    # in url_metadata for downstream fallback dating.
    sitemap_entries = [(u, lm) for u, lm in _try_sitemap_with_lastmod(company_url)
                       if _same_brand(company_url, u)]
    _dedupe_extend(from_sitemap, (u for u, _ in sitemap_entries))
    if url_metadata is not None:
        for url, lastmod in sitemap_entries:
            if lastmod:
                url_metadata.setdefault(url.rstrip("/"), {})["lastmod"] = lastmod
                url_metadata[url.rstrip("/")]["source"] = "sitemap"

    # 3. Common-path probe — canonical paths like /newsroom that aren't always linked
    # from the homepage. Single HTTP pass per URL: status check + listing-score in
    # one go (was previously two fetches per URL — once for 404 check, once for
    # scoring). SPA shells kept so the extractor's Playwright fallback handles them.
    _dedupe_extend(from_probe, _validate_listing_candidates(_build_probe_urls(company_url), min_score=3))

    # Reserve up to 3 slots at the top of the merged result for sitemap-LEAF article
    # URLs (path depth ≥ 2). Sitemaps give us *specific* recent articles (sorted by
    # <lastmod>); anchor scan / probe give us listing pages and hope the LLM picks
    # the right items. For SDR-curated regression tests, leaf-article URLs match
    # the user's expected URLs directly. Listing pages → LLM noise → match swing.
    #
    # Concretely: take the top-3 leaf-article URLs from sitemap (deep paths first),
    # then merge anchor → probe → remaining sitemap, then cap. Recent leafs get
    # a guaranteed seat at the table without starving listing-page discovery.
    sitemap_leaves: list[str] = []
    sitemap_listings: list[str] = []
    for u in from_sitemap:
        segments = [s for s in (urlparse(u).path or "").split("/") if s]
        if len(segments) >= 2:
            sitemap_leaves.append(u)
        else:
            sitemap_listings.append(u)

    PRIORITY_SITEMAP_LEAVES = 3
    priority_leaves = sitemap_leaves[:PRIORITY_SITEMAP_LEAVES]
    leftover_sitemap = sitemap_leaves[PRIORITY_SITEMAP_LEAVES:] + sitemap_listings
    found: list[str] = list(priority_leaves) + list(from_anchor) + list(from_probe) + list(leftover_sitemap)

    # 4. LLM-via-scrapegraph as a last resort — only run if everything above failed
    # to surface anything beyond the homepage. Pays Playwright + LLM cost.
    if not found:
        from scrapegraphai.graphs import SmartScraperGraph  # lazy
        cfg = build_graph_config(company_url)
        meter.charge(
            estimate_call_usd(input_chars=20_000, expected_output_tokens=300),
            label="discover_pages",
        )
        graph = SmartScraperGraph(
            prompt=_DISCOVERY_PROMPT, source=company_url, config=cfg, schema=DiscoveredUrls,
        )
        try:
            result = graph.run() or {}
            raw_urls = result.get("urls") if isinstance(result, dict) else []
            llm_urls: list[str] = []
            for u in raw_urls or []:
                u = str(u)
                if u.startswith("/"):
                    u = urljoin(company_url, u)
                if _same_brand(company_url, u):
                    llm_urls.append(u)
            _dedupe_extend(found, llm_urls)
        except Exception as e:
            logger.warning("discover_pages LLM fallback failed for %s: %s", company_url, e)

    # Always include the homepage so the extractor has a fallback source.
    if company_url not in found and company_url.rstrip("/") not in seen:
        found.insert(0, company_url)

    logger.info(
        "discover_pages: %d URL(s) for %s (anchor=%d, sitemap=%d [leaves=%d], probe=%d)",
        len(found), company_url, len(from_anchor), len(from_sitemap),
        len(sitemap_leaves), len(from_probe),
    )
    return found[:cap]


def extract_company_news(
    urls: list[str],
    meter: CostMeter,
    *,
    url_metadata: Optional[dict] = None,
) -> list[NewsItem]:
    """Direct httpx → Trafilatura → LLM extraction with link-constrained URLs.

    Delegates to ``src.extractor.extract_news_from_pages``. ``url_metadata`` is
    optional ``{url_no_trailing_slash: {"lastmod": "<iso>", ...}}`` populated by
    ``discover_pages`` from sitemap data — used as a fallback published-date when
    the LLM can't extract one from a page.
    """
    from .extractor import extract_news_from_pages  # lazy: avoid pulling httpx/trafilatura/openai at import time
    return extract_news_from_pages(urls, meter, url_metadata=url_metadata)
