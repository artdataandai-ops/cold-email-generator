"""Pure-logic tests for src/search.py helpers (no DDG, no LLM)."""
import sys
import types
from contextlib import contextmanager
from datetime import date

from src.schemas import NewsItem
from src.search import (
    _brand_candidates,
    _build_web_search_prompt,
    _company_name_from_url,
    _DDG_PER_INTENT_CAP,
    _DDG_QUERY_INTENTS,
    _ddg_search,
    _is_relevant_to_company,
)


@contextmanager
def _patch_ddgs(fake_factory):
    """Inject a stub ``ddgs`` module so ``_ddg_search``'s lazy
    ``from ddgs import DDGS`` succeeds in test environments where the real
    package isn't installed (e.g. system pytest runs outside the project venv).
    """
    mod = types.ModuleType("ddgs")
    mod.DDGS = fake_factory
    prev = sys.modules.get("ddgs")
    sys.modules["ddgs"] = mod
    try:
        yield
    finally:
        if prev is None:
            del sys.modules["ddgs"]
        else:
            sys.modules["ddgs"] = prev


@contextmanager
def _patch_search_helpers(items_by_source_url):
    """Stub out the httpx + LLM pipeline ``web_search_company`` now uses
    internally (Lever 2 — same path the on-site extractor was already using).

    Replaces ``src.search._fetch`` (httpx-with-Playwright-fallback) and
    ``src.search._call_llm`` (strict structured-output OpenAI call) so tests
    can verify post-extraction logic — URL hallucination guard, brand-relevance
    filter, etc. — without making real network calls.

    ``items_by_source_url``: dict mapping the DDG source URL → list of raw
    item dicts the LLM should "return" for that URL.
    """
    import src.search as search_mod

    orig_fetch = search_mod._fetch
    orig_clean = search_mod._clean_text
    orig_call_llm = search_mod._call_llm

    # Carry the per-URL canned response through fetch → call_llm via a
    # thread-local so the fakes don't have to inspect the prompt to figure
    # out which URL they're servicing.
    import threading
    _state = threading.local()

    def _fake_fetch(url):
        _state.url = url
        # Return a plausible (final_url, html) — html is irrelevant past the
        # 1KB sanity check inside _fetch.
        return (url, "<html><body>" + ("x" * 1024) + "</body></html>")

    def _fake_clean(html):
        # Has to exceed the 200-char threshold to keep the pipeline going.
        return "fake cleaned text " * 30

    def _fake_call_llm(prompt):
        url = getattr(_state, "url", None)
        items = list(items_by_source_url.get(url, []))
        # _call_llm returns (raw_items, usage_or_None).
        return items, None

    search_mod._fetch = _fake_fetch
    search_mod._clean_text = _fake_clean
    search_mod._call_llm = _fake_call_llm
    try:
        yield
    finally:
        search_mod._fetch = orig_fetch
        search_mod._clean_text = orig_clean
        search_mod._call_llm = orig_call_llm


def test_brand_candidates_simple_domain():
    assert _brand_candidates("https://www.thredd.ai/") == {"thredd"}


def test_brand_candidates_splits_communications_suffix():
    out = _brand_candidates("https://www.dialectcommunications.com/")
    assert "dialectcommunications" in out
    assert "dialect" in out


def test_brand_candidates_splits_group_suffix():
    out = _brand_candidates("https://acmegroup.com/")
    assert "acmegroup" in out
    assert "acme" in out


def test_brand_candidates_no_split_when_stem_too_short():
    # 'aigroup' would split to 'ai' (2 chars) — too aggressive, skip.
    out = _brand_candidates("https://aigroup.com/")
    assert out == {"aigroup"}


def test_company_name_from_url_basic():
    assert _company_name_from_url("https://www.dialectcommunications.com/") == "dialectcommunications"
    assert _company_name_from_url("https://thredd.ai/") == "thredd"


def _item(title: str, summary: str) -> NewsItem:
    return NewsItem(
        title=title,
        summary=summary,
        published_date=date(2026, 5, 1),
        url="https://news.example-real.com/x",
    )


def test_relevance_filter_keeps_brand_in_title():
    brands = _brand_candidates("https://www.dialectcommunications.com/")
    assert _is_relevant_to_company(
        _item("Dialect launches MultiVox", "Generic summary about voice support."),
        brands,
    )


def test_relevance_filter_keeps_brand_in_summary():
    brands = _brand_candidates("https://www.dialectcommunications.com/")
    assert _is_relevant_to_company(
        _item("New voice platform", "Dialect Communications today announced..."),
        brands,
    )


def test_relevance_filter_drops_unrelated_aggregator_item():
    """The Business Wire India article that polluted Dialect's results."""
    brands = _brand_candidates("https://www.dialectcommunications.com/")
    assert not _is_relevant_to_company(
        _item(
            "Business Wire India Announces New Website Launch",
            "The redesigned website will enhance user experience and boost news reach.",
        ),
        brands,
    )


def test_relevance_filter_case_insensitive():
    brands = _brand_candidates("https://thredd.ai/")
    assert _is_relevant_to_company(
        _item("THREDD partners with Visa", "Major partnership announcement."),
        brands,
    )


def test_relevance_filter_matches_compound_brand_with_whitespace():
    """icicilombard.com → needle 'icicilombard'. Articles write 'ICICI Lombard'
    with a space. The compact-haystack check must catch this."""
    brands = _brand_candidates("https://www.icicilombard.com")
    assert _is_relevant_to_company(
        _item(
            "ICICI Lombard reports record Q1 profit",
            "The insurer announced a 24% YoY increase in net profit for Q1.",
        ),
        brands,
    )


def test_relevance_filter_matches_compound_brand_with_hyphen():
    brands = _brand_candidates("https://www.homedepot.com")
    assert _is_relevant_to_company(
        _item("Home-Depot expands into Mexico", "The retailer opens 12 new stores."),
        brands,
    )


def test_relevance_filter_still_rejects_truly_unrelated_text():
    """Compact matching shouldn't make the filter too permissive."""
    brands = _brand_candidates("https://www.icicilombard.com")
    assert not _is_relevant_to_company(
        _item("Q2 holiday recap", "Office party photos and team awards."),
        brands,
    )


# ---- company_name override ----

def test_brand_candidates_includes_explicit_name_tokens():
    """When the user types 'ICICI Lombard', both words should be added as needles."""
    brands = _brand_candidates("https://www.icicilombard.com", company_name="ICICI Lombard")
    assert "icicilombard" in brands           # raw slug stays
    assert "icici lombard" in brands          # full lowercased name
    assert "icici" in brands                  # each token
    assert "lombard" in brands


def test_brand_candidates_explicit_name_helps_unmashed_brand():
    """hedgeequities.com slug doesn't split via known suffixes; an explicit name does."""
    brands = _brand_candidates("https://hedgeequities.com/", company_name="Hedge Equities")
    assert "hedgeequities" in brands
    assert "hedge equities" in brands
    assert "hedge" in brands
    assert "equities" in brands


def test_brand_candidates_ignores_blank_company_name():
    """Whitespace-only name should be treated as not provided."""
    brands = _brand_candidates("https://thredd.ai/", company_name="   ")
    assert brands == {"thredd"}


def test_brand_candidates_no_company_name_falls_back_to_slug():
    """Backward-compat: the old single-arg form still works."""
    assert _brand_candidates("https://thredd.ai/") == {"thredd"}


def test_relevance_filter_matches_via_explicit_name_token():
    """An article that mentions only 'Lombard' (not 'ICICI Lombard' joined) should
    match when the user provided the full name."""
    brands = _brand_candidates("https://www.icicilombard.com", company_name="ICICI Lombard")
    assert _is_relevant_to_company(
        _item("Lombard reports record quarter", "The insurer posted strong results."),
        brands,
    )


def test_relevance_filter_drops_unrelated_even_with_explicit_name():
    """Defence: providing a name shouldn't make the filter accept everything."""
    brands = _brand_candidates("https://www.icicilombard.com", company_name="ICICI Lombard")
    assert not _is_relevant_to_company(
        _item("Generic finance news roundup", "Some other companies in the space."),
        brands,
    )


# ---- LLM prompt structure for web search (entity disambiguation) ----
#
# The deterministic relevance filter above is a safety net. The primary defence
# against false positives like "hedge funds" articles being treated as coverage
# of the company "Hedge Equities" is the LLM extraction prompt itself. These
# tests pin the prompt's key disambiguating elements so they can't silently
# regress (the prompt is plain text — easy to break in passing edits).

def test_web_search_prompt_includes_company_url_as_entity_anchor():
    """The LLM needs to know WHICH 'Hedge Equities' / 'Stripe' / 'Apple' — the
    company URL is the unique anchor that disambiguates from concept/topic
    matches."""
    prompt = _build_web_search_prompt(
        "Hedge Equities", "https://hedgeequities.com/", "2026-04-13"
    )
    assert "https://hedgeequities.com/" in prompt
    assert "Hedge Equities" in prompt


def test_web_search_prompt_contains_anti_topic_confusion_clause():
    """Generic industry topics that share words with the brand must be
    explicitly framed as off-topic — this is the LLM's instruction to use
    world knowledge, not just substring matching."""
    prompt = _build_web_search_prompt(
        "Hedge Equities", "https://hedgeequities.com/", "2026-04-13"
    )
    low = prompt.lower()
    # Specific company at this URL — not industry-topic — language is present.
    assert "specifically" in low
    assert "industry" in low or "asset class" in low
    assert "do not include" in low or "do not extract" in low


def test_web_search_prompt_url_field_points_at_article_not_company():
    """Without this guardrail, the LLM occasionally echoes the company website
    back as the article URL when it gets confused. The prompt must spell out
    that `url` is the article's URL."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13"
    )
    low = prompt.lower()
    assert "article" in low
    # Either "not the company website" or "not <url>" explicit.
    assert ("not the company" in low) or ("not https://acme.com" in low)


def test_web_search_prompt_carries_recency_cutoff():
    """Regression guard: the prompt's date constraint must still be propagated."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13"
    )
    assert "2026-04-13" in prompt


def test_web_search_prompt_includes_meta_date_hint_when_given():
    """Post Lever-2 we hit a regression where 8 unrelated articles all got
    stamped with the same date — the LLM was defaulting to the cutoff when
    given no per-page date signal. The metadata hint (from Trafilatura's
    ``<meta property=\"article:published_time\">`` extraction) is what the
    on-site extractor uses; web_search must use it too for parity."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13", meta_date="2026-05-07"
    )
    assert "2026-05-07" in prompt
    assert "metadata" in prompt.lower()


def test_web_search_prompt_omits_meta_hint_when_no_date_found():
    """No spurious phrasing when Trafilatura couldn't extract a date."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13", meta_date=None
    )
    # The "Metadata-extracted publication date" line must be absent when
    # there's no date to inject.
    assert "Metadata-extracted publication date" not in prompt


def test_web_search_prompt_forbids_defaulting_to_cutoff():
    """The exact regression we just fixed: the LLM was using the cutoff date
    as a fallback when it couldn't determine the real date. The prompt now
    explicitly tells it to SKIP rather than default."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13"
    )
    low = prompt.lower()
    # An explicit "do not default to..." style instruction must be present.
    assert "do not default" in low or "do not use" in low
    # And a SKIP fallback when the date can't be determined.
    assert "skip" in low and ("cannot determine" in low or "cannot be determined" in low)


class _FakeDDG:
    """Stub matching DDGS context-manager protocol; replays per-query results."""

    def __init__(self, results_by_query: dict[str, list[dict]]):
        self._results = results_by_query
        self.queries_seen: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, query: str, max_results: int):
        self.queries_seen.append(query)
        return list(self._results.get(query, []))[:max_results]


# ---- DDG query construction: multi-intent union, no OR-disjunction, no
# site exclusions in the query string itself ----

def test_ddg_search_uses_multiple_intent_templates_not_one_big_OR_query():
    """Empirical (2026-05-14): single-query with OR-disjunction + 11 -site:
    clauses caused DDG to return generic top-news domains instead of relevant
    results. The new design issues one narrow query per intent term."""
    fake = _FakeDDG({})
    with _patch_ddgs(lambda: fake):
        _ddg_search("Acme", "2026-02-13", n=5)

    # Every issued query is a narrow `"Acme" <intent> after:<cutoff>` shape.
    assert fake.queries_seen, "expected at least one DDG query"
    for q in fake.queries_seen:
        assert " OR " not in q, f"query still uses OR disjunction: {q!r}"
        assert "-site:" not in q, f"query still uses site exclusions: {q!r}"
        assert '"Acme"' in q, f"company name should be quoted: {q!r}"
        assert "after:2026-02-13" in q, f"cutoff missing: {q!r}"
    # One query per declared intent (no duplicates, no drops).
    assert len(fake.queries_seen) == len(_DDG_QUERY_INTENTS)


def test_ddg_search_unions_results_across_intents_and_dedupes():
    """Different intent queries surface different relevance buckets. We union
    them so a partnership story (only ranked under `partnership`) isn't dropped
    because `news` returned 10 unrelated profile pages first."""
    shared_dupe = {"href": "https://news-site.com/acme-quarterly-results"}
    fake = _FakeDDG({
        '"Acme" news after:2026-02-13': [
            {"href": "https://news-site.com/acme-revenue-up"},
            shared_dupe,
        ],
        '"Acme" partnership after:2026-02-13': [
            {"href": "https://other-site.com/acme-and-globex-partnership"},
            shared_dupe,  # appears in multiple queries — must dedupe
        ],
        '"Acme" announces after:2026-02-13': [
            {"href": "https://third.com/acme-announces-launch"},
        ],
        '"Acme" launch after:2026-02-13': [
            {"href": "https://fourth.com/acme-launches-platform"},
        ],
    })
    with _patch_ddgs(lambda: fake):
        out = _ddg_search("Acme", "2026-02-13", n=5)

    # Order preserved; the shared duplicate appears exactly once.
    assert out.count("https://news-site.com/acme-quarterly-results") == 1
    # All four unique results show up — coverage gain over single-query design.
    assert "https://news-site.com/acme-revenue-up" in out
    assert "https://other-site.com/acme-and-globex-partnership" in out
    assert "https://third.com/acme-announces-launch" in out
    assert "https://fourth.com/acme-launches-platform" in out


def test_ddg_search_caps_total_urls_for_downstream_budget():
    """One URL costs a Playwright scrape + LLM call. Without an explicit cap,
    4 intents × DDG over-fetch could push 40+ URLs into the scrape stage and
    blow the per-company latency budget."""
    flood = [{"href": f"https://x.com/{i}"} for i in range(50)]
    fake = _FakeDDG({q: flood for q in (
        '"Acme" news after:2026-02-13',
        '"Acme" partnership after:2026-02-13',
        '"Acme" announces after:2026-02-13',
        '"Acme" launch after:2026-02-13',
    )})
    with _patch_ddgs(lambda: fake):
        out = _ddg_search("Acme", "2026-02-13", n=5)
    # n=5 → cap at ~15 total (n*3). Allow some slack but reject runaway.
    assert len(out) <= 20, f"expected total_target cap to hold, got {len(out)}"


def test_ddg_search_round_robins_so_later_intents_can_contribute():
    """If `news` returns 50 results and `partnership` returns the article we
    care about, a non-round-robin strategy would consume the budget under
    `news` and never run `partnership`. This guards against that regression
    (which is exactly how we lost the Thredd/Currensea coverage)."""
    fake = _FakeDDG({
        '"Acme" news after:2026-02-13': [
            {"href": f"https://news.com/{i}"} for i in range(50)
        ],
        '"Acme" partnership after:2026-02-13': [
            {"href": "https://special.com/acme-currensea-partnership"},
        ],
        '"Acme" announces after:2026-02-13': [
            {"href": "https://special.com/acme-announces-thing"},
        ],
        '"Acme" launch after:2026-02-13': [
            {"href": "https://special.com/acme-launches-thing"},
        ],
    })
    with _patch_ddgs(lambda: fake):
        out = _ddg_search("Acme", "2026-02-13", n=5)
    # All three non-`news` intents got to contribute — that's the whole point.
    assert "https://special.com/acme-currensea-partnership" in out
    assert "https://special.com/acme-announces-thing" in out
    assert "https://special.com/acme-launches-thing" in out


def test_ddg_search_partnership_intent_gets_deeper_picks_than_news():
    """The Currensea regression: a partnership-style article surfaces at
    position 7 of the `partnership` query, but the old uniform 4-per-intent
    cap stopped at position 4. The new weighted allocation gives partnership
    a deeper budget (7) at the cost of `announces` / `launch` (2 each), so
    Currensea-class stories at positions 5-7 of partnership are caught."""
    # 8 partnership results — position 7 is the Currensea-style article.
    fake = _FakeDDG({
        '"Acme" news after:2026-02-13': [
            {"href": f"https://news.com/{i}"} for i in range(10)
        ],
        '"Acme" partnership after:2026-02-13': [
            {"href": f"https://partner.com/{i}"} for i in range(6)
        ] + [
            {"href": "https://partner.com/acme-currensea-partnership-renewal"},
            {"href": "https://partner.com/8"},
        ],
        '"Acme" announces after:2026-02-13': [
            {"href": f"https://announces.com/{i}"} for i in range(5)
        ],
        '"Acme" launch after:2026-02-13': [
            {"href": f"https://launch.com/{i}"} for i in range(5)
        ],
    })
    with _patch_ddgs(lambda: fake):
        out = _ddg_search("Acme", "2026-02-13", n=5)
    # The Currensea-style result (position 7 of partnership) is now reachable.
    assert "https://partner.com/acme-currensea-partnership-renewal" in out, (
        "Position-7 partnership result must be caught with the new weighted cap"
    )


def test_ddg_per_intent_caps_sum_matches_total_budget():
    """Sanity guard: if someone bumps total_target without updating the caps
    (or vice versa), the partnership intent could be starved or news could
    get a runaway share. The sum of caps should match a typical-n total
    budget so all intents can be honoured in a normal run."""
    # Default n=5 yields total_target = 15. Per-intent sum should match so
    # every cap can be paid out.
    assert sum(_DDG_PER_INTENT_CAP.values()) == 15
    # And every declared intent has a cap entry — no silent zero allocations.
    for intent in _DDG_QUERY_INTENTS:
        assert intent in _DDG_PER_INTENT_CAP, f"missing per-intent cap for {intent!r}"


def test_ddg_search_filters_blocklisted_hosts_post_fetch():
    """Site exclusions are no longer in the DDG query string. The same filter
    is applied here on the URLs DDG returns."""
    fake = _FakeDDG({
        '"Acme" news after:2026-02-13': [
            {"href": "https://www.businesswire.com/release"},   # blocklisted
            {"href": "https://www.crunchbase.com/company"},      # blocklisted
            {"href": "https://news-site.com/acme-good-article"},  # keep
        ],
        '"Acme" partnership after:2026-02-13': [],
        '"Acme" announces after:2026-02-13': [],
        '"Acme" launch after:2026-02-13': [],
    })
    with _patch_ddgs(lambda: fake):
        out = _ddg_search("Acme", "2026-02-13", n=5)
    assert "https://news-site.com/acme-good-article" in out
    assert not any("businesswire.com" in u for u in out)
    assert not any("crunchbase.com" in u for u in out)


# ---- URL hallucination guard inside web_search_company ----
#
# The LLM occasionally fabricates close-but-wrong URLs when it transcribes
# article references on a scraped page. Observed example: source page was
# `https://thepaypers.com/payments/news/<slug>` but the LLM emitted
# `https://www.thepaypers.com/news/<slug>` (path prefix wrong, slug right).
# We trust the URL we asked SmartScraperGraph to scrape over whatever the LLM
# returns — DDG already gave us the correct URL upstream.

def test_web_search_overrides_llm_url_with_source_url():
    from src.cost_guard import CostMeter
    from src.search import web_search_company

    # DDG returns one correct article URL.
    source_url = "https://thepaypers.com/payments/news/thredd-and-currensea-extend"
    ddg = _FakeDDG({
        '"Thredd" news after:2026-04-14': [{"href": source_url}],
        '"Thredd" partnership after:2026-04-14': [],
        '"Thredd" announces after:2026-04-14': [],
        '"Thredd" launch after:2026-04-14': [],
    })

    # SmartScraperGraph's LLM emits a HALLUCINATED URL on the same domain but
    # wrong path (the exact failure from the user's report).
    hallucinated_url = "https://www.thepaypers.com/news/thredd-and-currensea-extend"
    fake_items = {
        source_url: [
            {
                "title": "Thredd and Currensea extend partnership",
                "summary": "Thredd extends its issuer processing partnership with Currensea by four years to strengthen cross-border travel cards.",
                "published_date": "2026-05-08",
                "url": hallucinated_url,  # WRONG — guard must override
                "category": "news",
            }
        ]
    }

    with _patch_ddgs(lambda: ddg), _patch_search_helpers(fake_items):
        out = web_search_company(
            "https://www.thredd.ai/",
            CostMeter(),
            company_name="Thredd",
            recency_days=30,
        )

    assert len(out) == 1
    # The returned URL must be the DDG/source URL, NOT the LLM's fabrication.
    assert str(out[0].url) == source_url


def test_web_search_overrides_url_even_when_llm_returns_correct_url():
    """When the LLM happens to return the right URL, override is a no-op —
    item still has the source URL. Belt-and-suspenders."""
    from src.cost_guard import CostMeter
    from src.search import web_search_company

    source_url = "https://news-site.com/thredd-good-article"
    ddg = _FakeDDG({
        '"Thredd" news after:2026-04-14': [{"href": source_url}],
        '"Thredd" partnership after:2026-04-14': [],
        '"Thredd" announces after:2026-04-14': [],
        '"Thredd" launch after:2026-04-14': [],
    })
    fake_items = {
        source_url: [{
            "title": "Thredd raises funding",
            "summary": "Thredd announced a successful funding round to accelerate growth across its global payment processing platform.",
            "published_date": "2026-05-01",
            "url": source_url,  # already correct
            "category": "news",
        }]
    }
    with _patch_ddgs(lambda: ddg), _patch_search_helpers(fake_items):
        out = web_search_company(
            "https://www.thredd.ai/",
            CostMeter(),
            company_name="Thredd",
            recency_days=30,
        )
    assert len(out) == 1
    assert str(out[0].url) == source_url


# ---- Sentiment classification instruction in the web-search prompt ----

def test_web_search_prompt_asks_for_sentiment_classification():
    """The LLM must populate `sentiment` on each item. The prompt also has to
    spell out the three labels and what each means; otherwise the LLM gets
    creative ('mixed', 'positive!', 'bullish') and the schema rejects them."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13"
    )
    low = prompt.lower()
    assert "sentiment" in low
    assert "positive" in low
    assert "neutral" in low
    assert "negative" in low
    # The "default to neutral when unsure" guidance prevents over-classification.
    assert "neutral" in low and "unsure" in low


def test_web_search_prompt_sentiment_uses_concrete_examples():
    """Generic 'positive/negative' alone is ambiguous to the LLM. The prompt
    pins down what each polarity covers with examples (layoffs, funding, etc.).
    Without this, classification quality drops sharply."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13"
    ).lower()
    # At least a couple of canonical negative-event examples are listed.
    assert "layoff" in prompt or "fine" in prompt
    # And a few positive-event examples.
    assert "funding" in prompt or "award" in prompt or "partnership" in prompt


def test_web_search_prompt_asks_for_event_significance():
    """Sentiment alone doesn't distinguish a $200M funding round from a
    legal-entity rename — both are positive. event_significance is the missing
    axis that protects against picking an 'administrative' event as an email
    anchor (the Lenskart Solutions Limited rename bug)."""
    prompt = _build_web_search_prompt(
        "Acme", "https://acme.com/", "2026-04-13"
    ).lower()
    assert "event_significance" in prompt
    # All six classes documented inline so the LLM has clear vocabulary.
    for label in ("major", "notable", "routine", "administrative", "peripheral", "negative"):
        assert label in prompt, f"event_significance class {label!r} missing from prompt"
    # Concrete callout that legal-entity renames are administrative — the exact
    # case that produced today's bad Lenskart email.
    assert "rename" in prompt
    # Default-to-notable guardrail so the LLM doesn't over-classify as major.
    assert "default to" in prompt and "notable" in prompt


def test_web_search_prompt_generic_across_companies():
    """The disambiguation clause is not hard-coded to one industry. Every
    company gets the same structural treatment."""
    for name, url in [
        ("Stripe", "https://stripe.com/"),
        ("Tesla", "https://tesla.com/"),
        ("ICICI Lombard", "https://www.icicilombard.com/"),
        ("Hedge Equities", "https://hedgeequities.com/"),
    ]:
        prompt = _build_web_search_prompt(name, url, "2026-04-13")
        assert name in prompt, f"name missing for {name!r}"
        assert url in prompt, f"url missing for {url!r}"
        # The anti-confusion clause structure is universal — not industry-coded.
        assert "specifically" in prompt.lower()
