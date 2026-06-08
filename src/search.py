from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlparse

from .config import LLM_MODEL, RECENCY_DAYS, SEARCH_MAX_RESULTS
from .cost_guard import CostMeter, estimate_call_usd
# Reuse the on-site extractor's httpx-first fetcher, Trafilatura cleaner, and
# strict-structured-output LLM call. Module-level imports (rather than the old
# lazy-inside-function pattern) so tests can monkeypatch these names directly
# via ``src.search._fetch`` / ``src.search._call_llm`` / etc.
from .extractor import _call_llm, _clean_text, _extract_meta_date, _fetch
from .schemas import NewsItem

logger = logging.getLogger(__name__)

_BLOCKED_DOMAINS = (
    # Press-release aggregators (low-signal, HTTP/2 issues with Playwright)
    "businesswire.com",
    "prnewswire.com",
    "globenewswire.com",
    "einpresswire.com",
    "accesswire.com",
    # SEO/aggregator farms with anti-scraper protection
    "socialnews.xyz",
    "tracxn.com",
    "crunchbase.com",
    "rocketreach.co",
    "zoominfo.com",
    "owler.com",
)


def _company_name_from_url(company_url: str) -> str:
    host = urlparse(company_url).hostname or company_url
    host = host.removeprefix("www.")
    return host.split(".")[0]


# Stripped from the end of a domain slug to recover a likely brand head, e.g.
# "dialectcommunications" -> "dialect". Order matters; longer matches come first.
_GENERIC_DOMAIN_SUFFIXES = (
    "communications", "international", "consulting", "solutions", "industries",
    "technologies", "enterprises", "investments", "ventures", "partners",
    "holdings", "services", "software", "systems", "digital", "studios",
    "global", "group", "media", "labs", "tech",
)


def _brand_candidates(company_url: str, company_name: Optional[str] = None) -> set[str]:
    """Lowercase needles for checking whether an article is about this company.

    Always includes the raw domain slug. Also splits off recognized "company-suffix"
    words so 'dialectcommunications' yields {'dialectcommunications', 'dialect'}.

    When ``company_name`` is provided (e.g. user typed "Hedge Equities" in the UI for
    ``hedgeequities.com``), its lowercase full form and each significant word token are
    added as additional needles. This matters for compound-brand companies where the
    URL slug is mashed and rarely appears in articles verbatim.
    """
    slug = _company_name_from_url(company_url).lower()
    out = {slug}
    for suffix in _GENERIC_DOMAIN_SUFFIXES:
        if slug.endswith(suffix) and len(slug) > len(suffix) + 2:
            out.add(slug[: -len(suffix)])
            break
    if company_name:
        normalised = company_name.strip().lower()
        if normalised:
            out.add(normalised)
            # Each meaningful word token (3+ chars, alpha-ish) as a separate needle —
            # an article that mentions just "Lombard" or just "Hedge" still counts.
            for token in re.findall(r"[a-z][a-z\-]{2,}", normalised):
                out.add(token)
    return out


_NON_WORD_RE = re.compile(r"[\s\-_.]+")


def _is_relevant_to_company(item: NewsItem, brands: set[str]) -> bool:
    """True if any brand needle appears in the item's title or summary.

    The match is whitespace/punctuation-tolerant: compound-brand domains like
    ``icicilombard.com`` produce the needle ``icicilombard``, but real articles
    write the brand as ``"ICICI Lombard"``. We also check a compacted form of the
    haystack with whitespace, hyphens, dots, and underscores stripped, so
    ``icicilombard`` ⊆ ``icici lombard``.
    """
    hay = f"{item.title} {item.summary}".lower()
    hay_compact = _NON_WORD_RE.sub("", hay)
    return any(b in hay or b in hay_compact for b in brands)


def _host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def _is_blocked(url: str) -> bool:
    host = _host_of(url)
    return any(host == b or host.endswith("." + b) for b in _BLOCKED_DOMAINS)


def _build_web_search_prompt(
    name: str,
    company_url: str,
    cutoff_iso: str,
    meta_date: Optional[str] = None,
) -> str:
    """Prompt sent to the LLM for each third-party search result page.

    The earlier version told the LLM only the company NAME, which led to a
    semantic confusion class: on a hedgeweek.com page full of "hedge funds"
    content, the LLM would extract items as if they were about a specific
    company called "Hedge Equities". Generic industry articles passed through
    because the LLM had no anchor to a specific entity.

    Three additions fix this without changing any downstream code:

    1. ``company_url`` is included so the LLM is anchored to one specific
       entity at a specific website, not a generic concept that shares words
       with the company name.
    2. An explicit "do not include generic industry topics" clause with
       concrete framing (industry-topic vs specific-company-at-this-URL) lets
       the LLM use world knowledge to disambiguate — which is exactly the kind
       of judgment string-matching can't do.
    3. ``meta_date`` carries Trafilatura's parse of the page's actual
       ``<meta property="article:published_time">`` / JSON-LD ``datePublished``
       and is passed to the LLM as a metadata hint. Without this the LLM
       defaults to the cutoff date when it can't determine the real date —
       causing the "8 articles all stamped 2026-04-14" pattern we hit after
       Lever-2 dropped SmartScraperGraph's implicit metadata access.

    The ``url`` field in each item is also clarified to be the *article's*
    absolute URL, not ``company_url`` — preventing the LLM from echoing the
    company website back as an item URL.
    """
    meta_hint = ""
    if meta_date:
        meta_hint = (
            f"\nMetadata-extracted publication date for this page "
            f"(use this if extraction is ambiguous): {meta_date}\n"
        )
    return (
        f'Extract recent news items specifically about the company "{name}" '
        f"(official website: {company_url}) from this page.\n\n"
        f"IMPORTANT — only include items whose subject is THIS specific company "
        f"at {company_url}. Do NOT include articles about generic industry "
        f"topics, asset classes, or similarly-named entities that merely share "
        f'words with the company name. For instance, an article about "hedge '
        f'funds" as an asset class is NOT about a specific company called '
        f'"Hedge Equities"; an article about "apple" the fruit is NOT about '
        f"Apple Inc.\n\n"
        f"For each item, return:\n"
        f"- title: the article headline as written on the page\n"
        f"- summary: your 2-3 sentence summary of the article\n"
        f"- published_date: ISO-8601 (YYYY-MM-DD). Use the article's ACTUAL "
        f"publication date as written on the page (look for byline dates, "
        f'"Posted on", "Published on", or the metadata hint below). Do NOT '
        f"default to the cutoff date ({cutoff_iso}) or today's date when "
        f"unsure — SKIP items where you cannot determine the real publication "
        f"date. Skip items dated before {cutoff_iso}."
        f"{meta_hint}"
        f"\n"
        f"- url: the ABSOLUTE URL of the article itself (NOT the company "
        f"website {company_url})\n"
        f"- category: one of news, award, funding, launch, press, blog, other\n"
        f"- sentiment: one of positive, neutral, negative. Use \"positive\" for "
        f"favourable events (funding raised, awards, growth, new partnerships, "
        f"successful launches, key hires); \"negative\" for unfavourable events "
        f"(layoffs, lawsuits, regulatory fines, security breaches, missed "
        f"earnings, executive departures under duress, product recalls); "
        f"\"neutral\" for informational coverage without clear polarity "
        f"(industry roundup mentioning the company, opinion pieces, factual "
        f"announcements that are neither good nor bad news). When unsure, "
        f"default to \"neutral\".\n"
        f"- event_significance: how substantive is this for a cold-email "
        f"opener? One of:\n"
        f"  * \"major\" = funding round, acquisition, marquee partnership, "
        f"major product launch, key executive hire, major customer win.\n"
        f"  * \"notable\" = incremental product news, smaller partnership, "
        f"industry award or recognition.\n"
        f"  * \"routine\" = scheduled earnings call, conference talk, "
        f"executive interview, predictable corporate update.\n"
        f"  * \"administrative\" = legal-entity rename, ticker change, "
        f"board reshuffle, regulatory filing — internally important but NOT "
        f"a customer-facing event. Example: \"Acme Solutions Pvt Ltd renamed "
        f"to Acme Solutions Limited\" is administrative, NOT a rebrand.\n"
        f"  * \"peripheral\" = the company is mentioned in passing within an "
        f"industry roundup, opinion column, or market analysis — not the "
        f"subject of the piece.\n"
        f"  * \"negative\" = controversy, lawsuit, layoff, breach, scandal, "
        f"or any unfavourable event (regardless of size).\n"
        f"  When unsure, default to \"notable\".\n\n"
        f"Skip items whose date you cannot determine. If this page has no "
        f'articles specifically about "{name}", return an empty items list. '
        f"Output JSON: {{items: [...]}}."
    )


# Intent terms used to build narrow DDG queries. Each surfaces a different
# DDG relevance bucket — partnerships rank under `partnership`, launches under
# `launch`, etc. Empirically (2026-05-14) DDG handles `(news OR funding OR
# award OR launch OR partnership)` very poorly — it collapses the disjunction
# and falls back to popularity, returning generic top news domains (TMZ,
# local-news affiliates) that are unrelated to the search subject. Multiple
# narrow queries are materially better at the same cost (5 cheap searches >
# 1 search that returns garbage).
#
# `funding` was tested and dropped — its results for most companies are
# profile-aggregator pages (Crunchbase, PitchBook, Tracxn) that we already
# blocklist, so it contributes almost no useful coverage.
_DDG_QUERY_INTENTS = ("news", "partnership", "announces", "launch")

# Per-intent depth caps. NOT uniform — partnership-style stories (the most
# valuable cold-email anchors: Currensea/Thredd, Clique/Visa, Cross River
# expansions) consistently surface at positions 5-7 of DDG's "partnership"
# query rather than the top 3. With an equal 4-per-intent allocation the
# Currensea article (position 7) was buried below the cap on every run.
# Allocating deeper depth to ``partnership`` and trimming ``announces``/
# ``launch`` (which mostly duplicate `news` results) catches the
# Currensea-class story while keeping total scrape budget at 15.
#
# Total = sum(values) = 15 — same as before, just redistributed.
# Verified empirically: 2026-05-14 DDG probe showed Currensea at position 7
# of the partnership query.
_DDG_PER_INTENT_CAP = {
    "news": 4,
    "partnership": 7,
    "announces": 2,
    "launch": 2,
}


def _ddg_search(name: str, cutoff_iso: str, n: int) -> list[str]:
    """Union of narrow per-intent DuckDuckGo queries for ``name``.

    Returns up to ~``n * 4`` deduplicated, blocklist-filtered URLs across
    multiple intent-specific queries (see ``_DDG_QUERY_INTENTS``). This is a
    deliberate change from the previous single-query design, which constructed
    one wide query with a 5-term OR disjunction and 11 ``-site:`` exclusions —
    DDG handled that input so poorly it returned generic top news domains
    unrelated to the brand.

    Site exclusions are NOT included in the DDG query anymore. The same
    blocklist is already applied here via ``_is_blocked`` after each result,
    and shorter queries materially improve DDG's relevance ranking.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning("ddgs not installed; cannot run web search.")
        return []

    seen: set[str] = set()
    urls: list[str] = []
    # Total scrape budget. Each kept URL becomes one httpx (+ optional
    # Playwright fallback) + one LLM call downstream. Sum of
    # ``_DDG_PER_INTENT_CAP.values()`` should equal this so all caps can be
    # honoured in a typical run. The ``n`` parameter scales the budget for
    # callers that explicitly want more results — most callers use n=5
    # (= 15 total URLs, matches the cap sum).
    total_target = max(n, 5) * 3

    for intent in _DDG_QUERY_INTENTS:
        if len(urls) >= total_target:
            break
        # Use the per-intent cap if known, fall back to a sensible default
        # (3) so adding a new intent to ``_DDG_QUERY_INTENTS`` doesn't break
        # this loop silently — better to under-allocate than to crash.
        per_intent_cap = _DDG_PER_INTENT_CAP.get(intent, 3)
        query = f'"{name}" {intent} after:{cutoff_iso}'
        intent_added = 0
        try:
            with DDGS() as ddg:
                # Over-fetch within the query (max_results=10) so DDG has
                # room to return depth past its first few "company profile"
                # / aggregator links. The per-intent cap then truncates to
                # the share that intent gets.
                for result in ddg.text(query, max_results=10):
                    url = result.get("href") or result.get("link") or ""
                    if not url or _is_blocked(url) or url in seen:
                        continue
                    seen.add(url)
                    urls.append(url)
                    intent_added += 1
                    if intent_added >= per_intent_cap or len(urls) >= total_target:
                        break
        except Exception as e:
            logger.warning("ddgs query %r failed: %s", query, e)
            continue

    return urls


def web_search_company(
    company_url: str,
    meter: CostMeter,
    *,
    max_results: Optional[int] = None,
    recency_days: Optional[int] = None,
    company_name: Optional[str] = None,
) -> list[NewsItem]:
    """Find recent third-party coverage of the company.

    Strategy: do the DDG search ourselves (one network call, no LLM), filter the
    URL list against the blocklist, then run a separate SmartScraperGraph per
    survivor inside its own try/except so one hostile page can never kill the
    whole step. This replaces ScrapeGraphAI's SearchGraph, which fails-fast on
    the first bad URL.

    ``company_name`` is an optional proper-name override. Without it we fall back to
    the URL slug (e.g. ``icicilombard``), which is fine for single-word brands but
    almost never matches the phrasing in real articles for compound brands like
    "ICICI Lombard" or "Home Depot". Passing it explicitly makes the DDG phrase
    query much more accurate.
    """
    name = (company_name or "").strip() or _company_name_from_url(company_url)
    days = recency_days if recency_days is not None else RECENCY_DAYS
    # Use the caller's actual recency window for the DDG `after:` filter.
    # Earlier code applied a 90-day floor here, which broke narrow caps: a
    # 30-day request asked DDG for 90 days of results, DDG ranks by relevance
    # (older "established" articles bubble up), so 14 results came back all
    # 60–80 days old and the downstream 30-day filter dropped every one,
    # producing zero items. Honouring the user's window makes DDG return
    # genuinely recent articles that survive downstream filtering.
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    n_results = max_results if max_results is not None else SEARCH_MAX_RESULTS
    if n_results <= 0:
        return []

    # _ddg_search builds and unions multiple narrow per-intent queries internally
    # (e.g. `"name" partnership after:cutoff`). See the helper's docstring for
    # why this beats a single wide-disjunction query.
    urls = _ddg_search(name, cutoff, n_results)
    if not urls:
        logger.info("web_search_company: no usable URLs after blocklist filter for %s", name)
        return []

    # Lever-2 refactor: scrape each URL via the same httpx-first path the
    # on-site extractor has been using successfully — direct httpx, fall back
    # to Playwright ONLY when httpx returns a too-small body or an unrendered
    # SPA shell. Then call OpenAI with strict structured output. This replaces
    # the previous SmartScraperGraph-per-URL design (which spun up Playwright
    # for every URL whether or not the page needed JS, costing 5-7s per scrape
    # vs ~0.5-1s for httpx). Three wins:
    #   1. Wall clock: typical third-party news sites (pymnts, financialit,
    #      crowdfundinsider, yahoo finance) render fine on plain HTTP, so 15
    #      URLs drop from ~50-66s to ~12-15s.
    #   2. No regression on hostile sites: ``_fetch`` still falls back to
    #      Playwright when httpx is blocked or the page is a SPA shell.
    #   3. Strict structured output eliminates the OUTPUT_PARSING_FAILURE
    #      class (e.g. the ``Unacademy\'s`` invalid-JSON-escape bug we saw —
    #      SmartScraperGraph's loose JSON parsing can't survive that, but
    #      ``client.beta.chat.completions.parse`` with ``response_format`` can.
    from concurrent.futures import ThreadPoolExecutor

    # The base prompt (without a per-URL metadata hint) is built once. Each
    # _scrape() call rebuilds with the page's actual meta date — fixes the
    # "everything stamped at the cutoff date" failure we saw post-Lever-2,
    # where the LLM had no per-page date signal and used the cutoff as default.
    brands = _brand_candidates(company_url, company_name=company_name)
    out: list[NewsItem] = []
    failed_hosts: list[str] = []
    off_topic_dropped = 0

    # Per-URL work is fully independent. 4 workers keeps RAM bounded when the
    # Playwright fallback fires; httpx itself is fine with many more.
    def _scrape(url: str):
        try:
            fetched = _fetch(url)
            if not fetched:
                return ("fetch_failed", url, None)
            final_url, html = fetched
            text = _clean_text(html)
            if len(text) < 200:
                return ("too_thin", final_url, None)
            meta_date = _extract_meta_date(html)
            # Build the prompt per-URL with this page's specific metadata-date
            # hint so the LLM can't default-stamp items with the cutoff date.
            scoped_prompt = _build_web_search_prompt(
                name, company_url, cutoff, meta_date=meta_date,
            )
            full_prompt = f"{scoped_prompt}\n\nPage content:\n{text[:12000]}"
            estimate = estimate_call_usd(
                input_chars=len(full_prompt), expected_output_tokens=400
            )
            raw_items, usage = _call_llm(full_prompt)
            return ("ok", final_url, {
                "estimate": estimate,
                "raw_items": raw_items,
                "usage": usage,
                "source_url": url,  # original DDG URL — used as authoritative item URL
            })
        except Exception as e:
            return ("error", url, e)

    with ThreadPoolExecutor(max_workers=4) as pool:
        scraped = list(pool.map(_scrape, urls))

    for status, final_url, payload in scraped:
        host = _host_of(final_url) or final_url
        if status == "fetch_failed":
            logger.info("web_search: fetch failed for %s", host)
            failed_hosts.append(host)
            continue
        if status == "too_thin":
            logger.debug("web_search: %s too thin after cleaning — skipping", host)
            continue
        if status == "error":
            err = payload  # exception object
            logger.info("web_search: %s on %s", type(err).__name__, host)
            failed_hosts.append(host)
            continue

        label = f"web_search:{host}"
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

        source_url = payload["source_url"]
        for raw in payload["raw_items"]:
            # URL hallucination guard: the LLM occasionally fabricates
            # close-but-wrong URLs (e.g. ``thepaypers.com/news/<slug>`` instead
            # of ``thepaypers.com/payments/news/<slug>``). The truthful URL is
            # ALWAYS the page we asked to scrape — override the LLM's claim
            # with the DDG-supplied source URL.
            raw["url"] = source_url
            try:
                item = NewsItem.model_validate(raw)
            except Exception as ex:
                logger.debug("dropping malformed search item %r: %s", raw, ex)
                continue
            # LLM sometimes extracts whatever's on an aggregator page even when
            # we told it to skip if the company isn't mentioned. Verify here.
            if not _is_relevant_to_company(item, brands):
                off_topic_dropped += 1
                logger.debug(
                    "web_search: dropping off-topic item %r (host=%s)", item.title, host,
                )
                continue
            out.append(item)

    logger.info(
        "web_search_company for %s: urls_tried=%d urls_failed=%d off_topic_dropped=%d items_kept=%d",
        name, len(urls), len(failed_hosts), off_topic_dropped, len(out),
    )
    return out
