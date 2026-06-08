"""Direct httpx → Trafilatura → LLM extraction.

Replaces scrapegraphai's SmartScraperMultiGraph for on-site news extraction. The
key wins over the scrapegraph path:

1. Trafilatura strips page chrome (nav/footer/cookie banners) so the LLM sees
   only the article body. This is what removes the example.com hallucination
   class from the Dialect run — the LLM had no chrome noise to get lost in.

2. We extract the page's actual ``<a href>`` link set with BeautifulSoup and
   require every LLM-produced URL to match one of them. Items with fabricated
   URLs are dropped at validation time, not pushed downstream.

3. Trafilatura's metadata extractor gives a deterministic published-date as a
   side channel, so we no longer rely solely on the LLM to guess dates.

4. httpx fetches in ~0.3-1s vs Playwright's 3-8s cold-start. We only fall back
   to Playwright when httpx fails or returns a sub-1KB page (likely a challenge
   page).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

from datetime import date as _date_type  # used in _LaxNewsItem
from pydantic import BaseModel, Field

from .config import LLM_MODEL, OPENAI_API_KEY, RECENCY_DAYS
from .cost_guard import CostMeter, estimate_call_usd
from .http_headers import request_headers
from .schemas import EventSignificance, NewsCategory, NewsItem, NewsSentiment

logger = logging.getLogger(__name__)


# Permissive mirror of NewsItem used as the LLM's structured-output schema.
# NewsItem.url runs strict placeholder/host validation that, when triggered by
# a single bad LLM emission (e.g. ``example.com``), causes the entire
# NewsItemList parse to fail — and we lose every other item in the batch.
# This lax schema accepts ``url`` as a plain string; the strict validation is
# then applied per-item downstream via ``NewsItem.model_validate``, which
# drops the bad item alone and keeps the rest. Same observable behaviour for
# the well-behaved case; graceful degradation for the partial-fail case.
class _LaxNewsItem(BaseModel):
    title: str
    summary: str
    published_date: _date_type
    url: str
    category: NewsCategory = "news"
    sentiment: NewsSentiment = "neutral"
    event_significance: EventSignificance = "notable"


class _LaxNewsItemList(BaseModel):
    items: list[_LaxNewsItem] = Field(default_factory=list)

# Pages shorter than this after httpx are likely Cloudflare challenge stubs
# or empty SPA shells — escalate to Playwright.
_MIN_HTML_BYTES = 1024


def _fetch_with_httpx(url: str, timeout: int = 15) -> Optional[tuple[str, str]]:
    """Return (final_url_after_redirects, html) or None."""
    import httpx  # lazy

    try:
        with httpx.Client(
            headers=request_headers(),
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            resp = client.get(url)
            if resp.status_code == 200 and len(resp.text) >= _MIN_HTML_BYTES:
                return str(resp.url), resp.text
            logger.debug(
                "httpx: %s returned status=%d size=%d (will fall back)",
                url, resp.status_code, len(resp.text),
            )
    except Exception as e:
        logger.debug("httpx fetch failed for %s: %s", url, e)
    return None


# Sentinel mount points commonly emitted by SPA frameworks before JS hydration.
# When the body is dominated by ONE of these and link/content density is low,
# we treat the page as an unrendered shell and retry via Playwright.
_SPA_ROOT_PATTERNS = (
    re.compile(r'<div[^>]+id=["\']?(?:root|app|__next|__nuxt|svelte)\b', re.IGNORECASE),
)


def _looks_like_unrendered_spa(html: str) -> bool:
    """Heuristic: does the HTML look like a JS-only shell whose content hasn't loaded yet?

    Signals (we require all):
      1. Body contains an SPA framework mount point (#root, #app, #__next, …).
      2. Anchor count is very low (< 8).  Real listing pages have dozens.
      3. Visible text (after Trafilatura) is short (< 800 chars). The shell may
         have boilerplate copy but won't have actual article headlines.
    """
    if not any(p.search(html) for p in _SPA_ROOT_PATTERNS):
        return False
    try:
        from bs4 import BeautifulSoup  # lazy
        soup = BeautifulSoup(html, "html.parser")
        if len(soup.find_all("a", href=True)) >= 8:
            return False
    except Exception:
        pass
    try:
        text = _clean_text(html)
    except Exception:
        text = ""
    return len(text) < 800


# Hard wall-clock budget for a single Playwright fetch. ChromiumLoader's
# `timeout` is per-attempt and async_timeout often doesn't release the loop on
# Cloudflare-style "navigating..." pages — we observed a single Thredd URL
# burning 50 minutes of pipeline time. This is the outer kill-switch.
_PLAYWRIGHT_WALL_CLOCK_S = 45


def _fetch_with_playwright(url: str) -> Optional[tuple[str, str]]:
    """Last-resort fetch using scrapegraphai's ChromiumLoader (stealth + retries).

    Wrapped in a thread-with-timeout so a Cloudflare interstitial cannot keep the
    pipeline hostage indefinitely. ``retry_limit=1`` (down from 2) because outer
    retries multiply with the JS-challenge redirect storms we've seen on Thredd.
    """
    import threading

    result: dict = {"out": None}

    def _run():
        try:
            from scrapegraphai.docloaders import ChromiumLoader  # lazy
            # NB: we tried load_state="networkidle" to fix the "page is navigating"
            # error on Thredd, but sites with constant polling (analytics, telemetry,
            # websockets) never reach networkidle and the kill-switch fires on every
            # URL. It also regressed Paymentology, which DID extract /newsroom under
            # the default. So we stick with "domcontentloaded" and accept that
            # navigation-flaky SPAs (Thredd) will occasionally fail with "page is
            # navigating" — better than always failing.
            loader = ChromiumLoader([url], headless=True, retry_limit=1, timeout=25)
            docs = list(loader.lazy_load())
            if docs and docs[0].page_content:
                result["out"] = (url, docs[0].page_content)
        except Exception as e:
            logger.warning("Playwright fallback failed for %s: %s", url, e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=_PLAYWRIGHT_WALL_CLOCK_S)
    if t.is_alive():
        # Thread is daemon — leave it; the join just gives up. Real CPU is
        # cheaply abandoned by the loader's own subprocess teardown later.
        logger.warning(
            "Playwright fetch exceeded %ds wall clock for %s; abandoning",
            _PLAYWRIGHT_WALL_CLOCK_S, url,
        )
        return None
    return result["out"]


def _fetch(url: str) -> Optional[tuple[str, str]]:
    """httpx first, Playwright fallback.

    Falls back to Playwright when:
    - httpx returned non-200 or a sub-1KB body (likely a challenge page), OR
    - httpx returned a JS-framework shell whose content needs to render before
      the page becomes useful (the icicilombard.com class — see ``_looks_like_unrendered_spa``).

    Returns (final_url, html) or None.
    """
    fetched = _fetch_with_httpx(url)
    if fetched and _looks_like_unrendered_spa(fetched[1]):
        logger.info("extractor: %s looks like an unrendered SPA shell; retrying via Playwright", url)
        pw = _fetch_with_playwright(url)
        if pw:
            return pw
    if fetched:
        return fetched
    return _fetch_with_playwright(url)


def _extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """All anchor (text, absolute_url) pairs in the page. Used to constrain LLM output.

    Drops entries whose resolved URL has a non-domain host (e.g. ``https://in/...`` —
    which happens when href is a region-prefixed path ``in/newsroom/x`` and ``urljoin``
    misparses it). Without this guard, the LLM is offered garbage anchors and the
    schema's host-validity check then quietly throws every item away.
    """
    from bs4 import BeautifulSoup  # lazy

    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#", 1)[0]
        # Drop obvious junk: javascript:, mailto:, tel:, and empty anchors.
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        host = (urlparse(href).hostname or "").lower()
        if not host or "." not in host:
            continue  # malformed: hostname must be a real dotted domain
        text = (a.get_text() or "").strip()
        if href in seen:
            continue
        seen.add(href)
        out.append((text, href))
    return out


def _clean_text(html: str) -> str:
    """Trafilatura's main-content extraction. Strips nav/footer/ads."""
    import trafilatura  # lazy

    text = trafilatura.extract(
        html,
        include_links=False,
        include_formatting=False,
        include_comments=False,
        favor_recall=True,  # prefer surfacing more text vs. tighter precision
    )
    return text or ""


def _extract_meta_date(html: str) -> Optional[str]:
    """Deterministic published-date via Trafilatura's metadata extractor.
    Returns ISO date string or None.
    """
    import trafilatura  # lazy

    try:
        meta = trafilatura.extract_metadata(html)
    except Exception:
        return None
    if not meta:
        return None
    raw = getattr(meta, "date", None)
    return raw if raw else None


def _normalize_url(u: str) -> str:
    """Compare URLs ignoring scheme/www/trailing-slash/fragment."""
    p = urlparse(u)
    host = (p.hostname or "").lower().removeprefix("www.")
    path = p.path.rstrip("/")
    return f"{host}{path}"


def _is_known_link(candidate_url: str, page_links: list[tuple[str, str]]) -> bool:
    """True if the LLM's URL matches one of the page's actual ``<a href>`` values."""
    if not candidate_url:
        return False
    target = _normalize_url(candidate_url)
    if not target:
        return False
    return any(_normalize_url(href) == target for _, href in page_links)


def _build_prompt(
    cleaned_text: str,
    links: list[tuple[str, str]],
    cutoff_iso: str,
    meta_date: Optional[str],
) -> str:
    # Cap link hints to keep prompt small. 60 anchors is generous for any news page.
    link_lines = []
    for text_label, href in links[:60]:
        snippet = (text_label[:80] or "(no link text)").replace("\n", " ")
        link_lines.append(f"- {snippet} → {href}")
    link_hints = "\n".join(link_lines) if link_lines else "(no links found)"

    meta_hint = ""
    if meta_date:
        meta_hint = (
            f"\nPublished date from page metadata (use this if extraction is ambiguous): {meta_date}"
        )

    return (
        f"Extract every distinct news, blog, announcement, award, funding, or product-launch "
        f"item described in the page content below that was published on or after {cutoff_iso}.\n\n"
        f"Rules:\n"
        f"- title: exact headline as written on the page.\n"
        f"- summary: your own 2-3 sentence summary.\n"
        f"- published_date: ISO-8601 (YYYY-MM-DD). Skip items where you cannot determine the date.\n"
        f"- url: MUST be an absolute URL chosen from the link list below. If no link in that list "
        f"corresponds to the item, SKIP the item — do not invent a URL.\n"
        f"- category: one of news, blog, award, funding, launch, press, other.\n"
        f"- sentiment: one of positive, neutral, negative. Use \"positive\" for favourable "
        f"events (funding, awards, growth, partnerships, successful launches, key hires); "
        f"\"negative\" for unfavourable events (layoffs, lawsuits, fines, breaches, missed "
        f"earnings, departures under duress, recalls); \"neutral\" for informational "
        f"content without clear polarity. When unsure, default to \"neutral\".\n"
        f"- event_significance: how substantive is this event for a cold-email opener? "
        f"One of: \"major\" (funding/acquisition/marquee partnership/major launch/key hire), "
        f"\"notable\" (incremental product news, smaller partnership, industry recognition), "
        f"\"routine\" (scheduled earnings, conference talk, predictable corporate update), "
        f"\"administrative\" (legal-entity rename, ticker change, regulatory filing — "
        f"internally important but NOT a customer-facing event), "
        f"\"peripheral\" (company is only mentioned in passing within an industry roundup), "
        f"\"negative\" (controversy/lawsuit/layoff/breach regardless of size). When unsure, "
        f"default to \"notable\".\n"
        f"- Skip items dated before {cutoff_iso}.\n"
        f"{meta_hint}\n\n"
        f"Available links on this page (use ONLY these URLs):\n{link_hints}\n\n"
        f"Page content:\n{cleaned_text[:12000]}"
    )


def _call_llm(prompt: str) -> tuple[list[dict], Optional[tuple[int, int]]]:
    """Direct OpenAI call with Pydantic-structured output.

    Returns ``(list_of_raw_item_dicts, usage_or_none)`` where usage is
    ``(prompt_tokens, completion_tokens)`` when the API returns it.
    Returns ``([], None)`` on any failure — the caller logs at the step level.
    """
    from openai import OpenAI  # lazy

    client = OpenAI(api_key=OPENAI_API_KEY)
    model = LLM_MODEL.split("/", 1)[-1] if "/" in LLM_MODEL else LLM_MODEL

    try:
        completion = client.beta.chat.completions.parse(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract structured news items from cleaned webpage text. "
                        "Never invent URLs or dates — only use values present in the provided content."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            # Use the lax schema so one item with a placeholder URL doesn't
            # kill the whole NewsItemList parse — strict validation runs
            # per-item in the caller via ``NewsItem.model_validate`` and
            # drops just the bad row.
            response_format=_LaxNewsItemList,
            temperature=0,
        )
        usage_obj = getattr(completion, "usage", None)
        usage = None
        if usage_obj is not None:
            usage = (
                int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                int(getattr(usage_obj, "completion_tokens", 0) or 0),
            )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            return [], usage
        return [it.model_dump() for it in parsed.items], usage
    except Exception as e:
        logger.warning("OpenAI extract call failed: %s", e)
        return [], None


_JSONLD_ARTICLE_TYPES = frozenset(
    {"BlogPosting", "NewsArticle", "Article", "Report", "TechArticle", "ReportageNewsArticle"}
)


def _walk_jsonld(node, out: list[dict]) -> None:
    """Recursively collect article-shaped JSON-LD objects from a parsed blob.

    Walks every dict-value and list-element. Sites wrap BlogPosting/NewsArticle
    objects under wildly different keys: standard schema.org uses ``@graph`` or
    ``itemListElement``, Yoast uses ``@graph``, and ad-hoc CMS like Shopify
    blog themes use custom keys such as ``"blogPosts"``. Recursing into all
    values rather than a fixed list of keys handles every shape uniformly. The
    type filter below still rejects non-article objects (ImageObject, Person,
    Organization, …) so the broader walk doesn't pollute results.
    """
    if isinstance(node, dict):
        types = node.get("@type")
        type_set: set[str] = set()
        if isinstance(types, str):
            type_set.add(types)
        elif isinstance(types, list):
            type_set.update(t for t in types if isinstance(t, str))
        if type_set & _JSONLD_ARTICLE_TYPES:
            out.append(node)
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_jsonld(value, out)
    elif isinstance(node, list):
        for n in node:
            _walk_jsonld(n, out)


def _extract_jsonld_items(html: str, base_url: str) -> list[dict]:
    """Pull article-shape dicts out of ``<script type="application/ld+json">`` blobs.

    Returns dicts in the same shape the LLM extractor produces. Dates are taken
    from ``datePublished`` (falling back to ``dateCreated``) and authoritatively
    truncated to ``YYYY-MM-DD``. Restricts URLs to the same eTLD+1 as the page
    itself, so a Shopify shop's embedded publisher/related-content JSON-LD doesn't
    leak unrelated URLs into the result.

    On listing pages (Shopify, WordPress, Webflow, Wix, etc.) this typically
    returns one entry per blog card with the right per-card date — the data the
    LLM path couldn't reliably get because Trafilatura strips JSON-LD as
    non-prose.
    """
    from bs4 import BeautifulSoup  # lazy

    soup = BeautifulSoup(html, "html.parser")
    raw_objects: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        blob = tag.string or tag.get_text() or ""
        if not blob.strip():
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        _walk_jsonld(data, raw_objects)

    base_host = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    seen: set[str] = set()
    items: list[dict] = []
    for obj in raw_objects:
        # url: can be a string, or a dict like {"@id": "...", "url": "..."}.
        url_field = obj.get("url") or obj.get("mainEntityOfPage") or obj.get("@id") or ""
        if isinstance(url_field, dict):
            url_field = url_field.get("@id") or url_field.get("url") or ""
        if not isinstance(url_field, str) or not url_field:
            continue
        full_url = urljoin(base_url, url_field).split("#", 1)[0]
        host = (urlparse(full_url).hostname or "").lower().removeprefix("www.")
        if not host or "." not in host:
            continue
        # Same-host (or subdomain) check — prevents publisher logos / related-site
        # references inside an Organization or NewsArticle's `publisher` block
        # from leaking through. Subdomains in either direction are fine.
        if base_host and not (
            host == base_host
            or host.endswith("." + base_host)
            or base_host.endswith("." + host)
        ):
            continue

        date_raw = obj.get("datePublished") or obj.get("dateCreated")
        date_iso = _lastmod_to_iso_date(date_raw) if isinstance(date_raw, str) else None
        if not date_iso:
            continue

        title_field = obj.get("headline") or obj.get("name") or ""
        if isinstance(title_field, list):
            title_field = next((t for t in title_field if isinstance(t, str)), "")
        if not isinstance(title_field, str) or not title_field.strip():
            continue

        summary_field = obj.get("description") or ""
        if isinstance(summary_field, list):
            summary_field = next((s for s in summary_field if isinstance(s, str)), "")
        if not isinstance(summary_field, str):
            summary_field = ""
        summary = summary_field.strip()[:600] or title_field.strip()

        types = obj.get("@type")
        type_str = ""
        if isinstance(types, str):
            type_str = types
        elif isinstance(types, list) and types:
            type_str = next((t for t in types if isinstance(t, str)), "")
        if type_str == "BlogPosting":
            category = "blog"
        elif type_str in {"NewsArticle", "Article", "Report", "TechArticle", "ReportageNewsArticle"}:
            category = "news"
        else:
            category = "other"

        if full_url in seen:
            continue
        seen.add(full_url)
        items.append({
            "title": title_field.strip()[:300],
            "summary": summary,
            "published_date": date_iso,
            "url": full_url,
            "category": category,
        })
    return items


# Threshold for "this page's JSON-LD listing is rich enough — skip the LLM call".
# 3 was chosen empirically: most listing pages emit 10-20 BlogPosting objects;
# single-article pages emit 1. A page with 0-2 JSON-LD items isn't a listing.
_JSONLD_LISTING_MIN_ITEMS = 3


def _lastmod_to_iso_date(value: Optional[str]) -> Optional[str]:
    """Sitemap <lastmod> can be a full ISO timestamp (2026-04-22T10:30:00Z) or a
    bare ISO date (2026-04-22). NewsItem.published_date wants a YYYY-MM-DD string.
    Return None on anything we can't safely truncate."""
    if not value:
        return None
    v = value.strip()
    # Bare YYYY-MM-DD or longer ISO datetimes both start with 10 chars of date.
    if len(v) >= 10 and v[4] == "-" and v[7] == "-":
        date_part = v[:10]
        # Quick well-formedness gate — Pydantic will reject malformed anyway.
        if date_part[:4].isdigit() and date_part[5:7].isdigit() and date_part[8:10].isdigit():
            return date_part
    return None


def extract_news_from_pages(
    urls: list[str],
    meter: CostMeter,
    *,
    recency_days: Optional[int] = None,
    url_metadata: Optional[dict] = None,
) -> list[NewsItem]:
    """Drop-in replacement for the scrapegraphai-based extract_company_news.

    Per URL:
      1. httpx fetch (Playwright fallback if httpx fails or gets ≤ 1KB).
      2. Trafilatura strips chrome; BeautifulSoup gives the full anchor set.
      3. Direct OpenAI call with Pydantic structured output.
      4. Drop any item whose URL isn't one of the page's actual ``<a href>`` values
         (the link-constraint check eliminates the example.com hallucination class).

    ``url_metadata`` provides per-URL hints (e.g. sitemap-declared ``lastmod``)
    used as a fallback published-date when the LLM doesn't extract one. Without
    this, articles on JS-heavy pages where the date isn't visible in cleaned
    text get dropped at the recency filter even when we have a perfectly good
    date in the sitemap.
    """
    if not urls:
        return []
    days = recency_days if recency_days is not None else RECENCY_DAYS
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out: list[NewsItem] = []
    hallucinated = 0
    lastmod_rescues = 0  # for logging — how often sitemap saved the day
    jsonld_pages = 0     # how many URLs took the JSON-LD short-circuit

    # Per-URL work is fully independent: fetch + Trafilatura + BeautifulSoup +
    # OpenAI call. We run them concurrently so total wall-clock time is
    # max(individual) instead of sum. Each thread gets its own OpenAI HTTP
    # request, which the OpenAI SDK is documented as thread-safe.
    #
    # Meter mutations stay on the main thread (after results come back) to
    # keep the CostMeter dataclass single-threaded.
    from concurrent.futures import ThreadPoolExecutor

    def _process(url: str):
        fetched = _fetch(url)
        if not fetched:
            return ("fetch_failed", url, None)
        final_url, html = fetched

        # JSON-LD short-circuit: most modern CMS (Shopify, WordPress, Webflow,
        # Wix, Ghost, Squarespace) emit BlogPosting/NewsArticle objects with
        # authoritative per-card datePublished. When a listing page exposes ≥3
        # such items, skip the LLM entirely — the structured data is more
        # reliable than the LLM's guesses (which Trafilatura defangs by
        # stripping JSON-LD as non-prose anyway).
        jsonld_items = _extract_jsonld_items(html, final_url)
        if len(jsonld_items) >= _JSONLD_LISTING_MIN_ITEMS:
            return ("jsonld", final_url, {"jsonld_items": jsonld_items})

        text = _clean_text(html)
        if len(text) < 200:
            return ("too_thin", final_url, None)
        links = _extract_links(html, final_url)
        meta_date = _extract_meta_date(html)
        prompt = _build_prompt(text, links, cutoff, meta_date)
        estimate = estimate_call_usd(input_chars=len(prompt), expected_output_tokens=600)
        raw_items, usage = _call_llm(prompt)
        return ("ok", final_url, {
            "links": links,
            "estimate": estimate,
            "raw_items": raw_items,
            "usage": usage,
        })

    with ThreadPoolExecutor(max_workers=4) as pool:
        per_url = list(pool.map(_process, urls))

    for status, final_url, payload in per_url:
        if status == "fetch_failed":
            logger.info("extractor: skipping %s (fetch failed)", final_url)
            continue
        if status == "too_thin":
            logger.debug("extractor: %s too thin after cleaning — skipping", final_url)
            continue

        if status == "jsonld":
            # Structured-data path: items came from <script type="application/ld+json">,
            # so URLs are authoritative (no LLM hallucination) and dates are real.
            # No meter charge — we didn't call the LLM. Skip the link-constraint
            # check, which exists to catch fabricated URLs that JSON-LD doesn't
            # produce in the first place.
            jsonld_pages += 1
            for raw in payload["jsonld_items"]:
                try:
                    item = NewsItem.model_validate(raw)
                except Exception as e:
                    logger.debug("extractor: dropping malformed JSON-LD item %r: %s", raw, e)
                    continue
                out.append(item)
            continue

        label = f"extract:{urlparse(final_url).hostname}"
        estimate = payload["estimate"]
        meter.charge(estimate, label=label)

        usage = payload["usage"]
        if usage is not None:
            meter.record_llm_usage(
                prior_estimate_usd=estimate,
                prompt_tokens=usage[0],
                completion_tokens=usage[1],
                model=LLM_MODEL,
                label=label,
            )

        links = payload["links"]
        for raw in payload["raw_items"]:
            # Layer-3 date rescue: if LLM didn't produce a usable published_date,
            # try the sitemap lastmod for THIS item's URL.
            item_url = (raw.get("url") or "").strip()
            if url_metadata and item_url and not raw.get("published_date"):
                meta = url_metadata.get(item_url.rstrip("/"), {})
                fallback = _lastmod_to_iso_date(meta.get("lastmod"))
                if fallback:
                    raw["published_date"] = fallback
                    lastmod_rescues += 1

            try:
                item = NewsItem.model_validate(raw)
            except Exception as e:
                logger.debug("extractor: dropping malformed item %r: %s", raw, e)
                continue
            if not _is_known_link(item.url, links):
                hallucinated += 1
                logger.debug(
                    "extractor: dropping hallucinated URL %s for title %r",
                    item.url, item.title,
                )
                continue
            out.append(item)

    logger.info(
        "extractor: urls=%d items_kept=%d hallucinated_dropped=%d "
        "sitemap_date_rescues=%d jsonld_pages=%d",
        len(urls), len(out), hallucinated, lastmod_rescues, jsonld_pages,
    )
    return out
