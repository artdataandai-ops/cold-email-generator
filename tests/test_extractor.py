"""Tests for src/extractor.py — pure logic only (no network, no LLM)."""
from urllib.parse import urlparse

import pytest

from src.extractor import (
    _build_prompt,
    _extract_links,
    _is_known_link,
    _normalize_url,
)


SAMPLE_HTML = """
<html><head><title>Acme Blog</title></head><body>
<nav><a href="/about">About</a><a href="/contact">Contact</a></nav>
<main>
  <article>
    <h2><a href="/blog/series-b-2026">Acme raises Series B</a></h2>
    <p>Posted 2026-04-01</p>
  </article>
  <article>
    <h2><a href="https://news.acme.com/cto-hire">Acme hires new CTO</a></h2>
    <p>Posted 2026-03-15</p>
  </article>
</main>
<footer>
  <a href="javascript:void(0)">Bad link</a>
  <a href="mailto:hi@acme.com">Email us</a>
  <a href="/about">About</a>  <!-- duplicate of nav, must dedupe -->
</footer>
</body></html>
"""


def test_extract_links_resolves_relative_to_absolute():
    links = _extract_links(SAMPLE_HTML, "https://acme.com/blog")
    hrefs = [h for _, h in links]
    assert "https://acme.com/about" in hrefs
    assert "https://acme.com/blog/series-b-2026" in hrefs


def test_extract_links_keeps_cross_domain_absolutes():
    links = _extract_links(SAMPLE_HTML, "https://acme.com/blog")
    hrefs = [h for _, h in links]
    assert "https://news.acme.com/cto-hire" in hrefs


def test_extract_links_drops_dotless_hosts():
    """The Stripe regression: a href like 'in/newsroom/x' under base 'stripe.com/in'
    can be urljoin'd to 'https://in/newsroom/x' — a hostname without a dot.
    Those anchors must not be offered to the LLM as 'allowed URLs'."""
    html = """
    <html><body>
      <a href="/newsroom/news/sessions-2026">Sessions 2026</a>
      <a href="in/newsroom/news/some-launch">Some launch</a>
    </body></html>
    """
    links = _extract_links(html, "https://stripe.com/in")
    hrefs = [h for _, h in links]
    # Real one survives
    assert "https://stripe.com/newsroom/news/sessions-2026" in hrefs
    # Mangled one is rejected (urljoin would parse 'in/...' as host=in)
    assert not any(urlparse(h).hostname == "in" for h in hrefs)


def test_extract_links_drops_javascript_and_mailto():
    links = _extract_links(SAMPLE_HTML, "https://acme.com/")
    hrefs = [h for _, h in links]
    assert not any(h.startswith("javascript:") for h in hrefs)
    assert not any(h.startswith("mailto:") for h in hrefs)


def test_extract_links_dedupes_repeated_urls():
    links = _extract_links(SAMPLE_HTML, "https://acme.com/")
    hrefs = [h for _, h in links]
    # /about appears twice in the HTML but should be in the result only once.
    assert hrefs.count("https://acme.com/about") == 1


def test_extract_links_strips_fragment():
    html = '<a href="https://acme.com/blog#section-2">Section</a>'
    links = _extract_links(html, "https://acme.com/")
    assert links == [("Section", "https://acme.com/blog")]


def test_normalize_url_ignores_www_and_trailing_slash():
    assert _normalize_url("https://www.acme.com/blog/") == _normalize_url("https://acme.com/blog")
    assert _normalize_url("http://acme.com/x") == _normalize_url("https://acme.com/x")


def test_is_known_link_accepts_url_present_on_page():
    links = _extract_links(SAMPLE_HTML, "https://acme.com/blog")
    assert _is_known_link("https://acme.com/blog/series-b-2026", links)
    # Also accepts equivalent forms (www/trailing slash).
    assert _is_known_link("https://www.acme.com/blog/series-b-2026/", links)


def test_is_known_link_rejects_hallucinated_url():
    """The Dialect-style example.com hallucination."""
    links = _extract_links(SAMPLE_HTML, "https://acme.com/blog")
    assert not _is_known_link("https://example.com/dialect-partnership", links)


def test_is_known_link_rejects_empty_url():
    links = _extract_links(SAMPLE_HTML, "https://acme.com/blog")
    assert not _is_known_link("", links)


def test_build_prompt_includes_link_hints():
    links = [("Series B post", "https://acme.com/blog/series-b")]
    prompt = _build_prompt("Acme raised money.", links, "2026-02-01", None)
    assert "Series B post" in prompt
    assert "https://acme.com/blog/series-b" in prompt
    assert "2026-02-01" in prompt


def test_build_prompt_includes_meta_date_when_present():
    prompt = _build_prompt("body", [], "2026-02-01", "2026-04-15")
    assert "2026-04-15" in prompt
    assert "metadata" in prompt.lower()


def test_build_prompt_omits_meta_when_absent():
    prompt = _build_prompt("body", [], "2026-02-01", None)
    assert "metadata" not in prompt.lower()


def test_build_prompt_asks_for_sentiment_classification():
    """On-site articles (the company's own blog/newsroom) tend toward positive
    framing but can still describe partnerships, layoffs (rare), or neutral
    industry analysis. The extractor prompt must ask for sentiment so the
    relevance scorer has a consistent signal across BOTH paths (on-site +
    web-search), not just the third-party path.
    """
    prompt = _build_prompt("body", [], "2026-02-01", None).lower()
    assert "sentiment" in prompt
    assert "positive" in prompt
    assert "neutral" in prompt
    assert "negative" in prompt
    # "Default to neutral when unsure" guardrail.
    assert "unsure" in prompt or "default" in prompt


# ---- Lax LLM response schema: graceful partial-fail handling ----
#
# The user-reported regression: ``OpenAI extract call failed: 1 validation
# error for NewsItemList`` — the LLM returned one item with
# ``https://www.example.com/...`` and the strict NewsItem.url validator
# raised at parse time, killing ALL items in that response (not just the bad
# one). Switching ``_call_llm``'s response_format to ``_LaxNewsItemList``
# (URL is a plain string, no host check) lets the parse succeed; the per-item
# strict validation then runs downstream via ``NewsItem.model_validate`` and
# drops just the bad row.


def test_lax_schema_accepts_placeholder_url_where_strict_rejects():
    """The whole point of the lax schema: it accepts URLs that NewsItem
    rejects, so one bad row doesn't take out the entire LLM response."""
    from datetime import date
    import pytest as _pytest
    from pydantic import ValidationError

    from src.extractor import _LaxNewsItem
    from src.schemas import NewsItem

    bad_url_payload = dict(
        title="Hallucinated headline",
        summary="LLM grabbed a placeholder link from somewhere on the page.",
        published_date=date(2026, 5, 8),
        url="https://www.example.com/fabricated-thredd-currensea-partnership",
        category="news",
    )
    # Strict NewsItem rejects (regression guard — placeholder check still works).
    with _pytest.raises(ValidationError):
        NewsItem.model_validate(bad_url_payload)
    # Lax schema accepts (so the LLM-side parse doesn't fail atomically).
    accepted = _LaxNewsItem.model_validate(bad_url_payload)
    assert accepted.url.endswith("partnership")


def test_lax_schema_mirrors_news_item_field_set():
    """If a field is added to NewsItem and not to _LaxNewsItem, the LLM
    will simply not be asked to populate it — silently degrading
    sentiment/event_significance/etc. coverage. Pin the field set so any
    schema drift is caught here."""
    from src.extractor import _LaxNewsItem
    from src.schemas import NewsItem

    # Every non-private NewsItem field (except the URL validator) should
    # have a counterpart on the lax schema.
    strict_fields = set(NewsItem.model_fields.keys())
    lax_fields = set(_LaxNewsItem.model_fields.keys())
    assert strict_fields == lax_fields, (
        f"_LaxNewsItem field set must mirror NewsItem; "
        f"strict-only={strict_fields - lax_fields}, "
        f"lax-only={lax_fields - strict_fields}"
    )


def test_build_prompt_asks_for_event_significance():
    """Same classification axis as web-search prompt — on-site extraction must
    populate event_significance too. A company's own 'we renamed our legal
    entity' post should be tagged administrative, not major."""
    prompt = _build_prompt("body", [], "2026-02-01", None).lower()
    assert "event_significance" in prompt
    for label in ("major", "notable", "routine", "administrative", "peripheral", "negative"):
        assert label in prompt, f"event_significance class {label!r} missing from prompt"
    # Default-to-notable guardrail.
    assert "default to" in prompt and "notable" in prompt


def test_looks_like_unrendered_spa_detects_react_shell():
    pytest.importorskip("trafilatura")  # _clean_text uses trafilatura
    pytest.importorskip("bs4")
    from src.extractor import _looks_like_unrendered_spa

    react_shell = """
    <!doctype html>
    <html><head><title>App</title></head>
    <body>
      <div id="root"></div>
      <script src="/static/js/main.js"></script>
    </body></html>
    """
    assert _looks_like_unrendered_spa(react_shell) is True


def test_looks_like_unrendered_spa_skips_real_listing_pages():
    pytest.importorskip("trafilatura")
    pytest.importorskip("bs4")
    from src.extractor import _looks_like_unrendered_spa

    real_page = """
    <html><body>
      <main>
        <article><h2><a href="/news/1">First headline goes here</a></h2>
          <p>This article body has plenty of substantive text describing what happened.
          The company announced a major product launch this week and shared details.</p>
        </article>
        <article><h2><a href="/news/2">Second headline</a></h2>
          <p>Another piece of news content with real prose that Trafilatura can extract.</p>
        </article>
        <a href="/a">a</a><a href="/b">b</a><a href="/c">c</a><a href="/d">d</a>
        <a href="/e">e</a><a href="/f">f</a><a href="/g">g</a><a href="/h">h</a>
      </main>
    </body></html>
    """
    assert _looks_like_unrendered_spa(real_page) is False


def test_looks_like_unrendered_spa_skips_pages_without_spa_root():
    pytest.importorskip("trafilatura")
    pytest.importorskip("bs4")
    from src.extractor import _looks_like_unrendered_spa

    # An empty static page is NOT a SPA shell — it's just a bad page.
    static_thin = "<html><body><p>Hello</p></body></html>"
    assert _looks_like_unrendered_spa(static_thin) is False


def test_build_prompt_truncates_long_content():
    # Use a character that won't appear in the prompt template.
    big = "Q" * 50000
    prompt = _build_prompt(big, [], "2026-02-01", None)
    # Body is capped at 12000 chars in _build_prompt.
    assert prompt.count("Q") == 12000


# ---- Trafilatura-dependent tests (skip if not installed in the test env) ----


def test_clean_text_strips_chrome():
    trafilatura = pytest.importorskip("trafilatura")  # noqa: F841
    from src.extractor import _clean_text

    html = """
    <html><body>
    <nav>Home Products About Contact</nav>
    <article>
      <h1>The actual article title</h1>
      <p>This is the article body. It has multiple sentences explaining what we did.
      Here's more detail in the second sentence.</p>
    </article>
    <footer>Copyright 2026 Privacy Terms Cookies</footer>
    </body></html>
    """
    text = _clean_text(html)
    assert "article body" in text.lower()
    # Trafilatura should drop nav + footer chrome.
    assert "Copyright" not in text
    assert "Privacy Terms Cookies" not in text


def test_extract_meta_date_finds_article_published_time():
    trafilatura = pytest.importorskip("trafilatura")  # noqa: F841
    from src.extractor import _extract_meta_date

    html = """
    <html><head>
      <meta property="article:published_time" content="2026-03-15T10:00:00Z" />
    </head><body><article><h1>x</h1><p>body</p></article></body></html>
    """
    d = _extract_meta_date(html)
    assert d == "2026-03-15"


# ---- Sitemap-lastmod fallback (depth metadata for date extraction) ----

def test_lastmod_to_iso_date_truncates_full_timestamps():
    from src.extractor import _lastmod_to_iso_date
    assert _lastmod_to_iso_date("2026-04-22T10:30:00Z") == "2026-04-22"
    assert _lastmod_to_iso_date("2026-04-22T10:30:00+00:00") == "2026-04-22"
    assert _lastmod_to_iso_date("2026-04-22") == "2026-04-22"


def test_lastmod_to_iso_date_rejects_garbage():
    from src.extractor import _lastmod_to_iso_date
    assert _lastmod_to_iso_date(None) is None
    assert _lastmod_to_iso_date("") is None
    assert _lastmod_to_iso_date("not-a-date") is None
    assert _lastmod_to_iso_date("12345") is None
    assert _lastmod_to_iso_date("2026/04/22") is None  # wrong separator


def test_lastmod_to_iso_date_strips_surrounding_whitespace():
    from src.extractor import _lastmod_to_iso_date
    assert _lastmod_to_iso_date("  2026-04-22  ") == "2026-04-22"


# ---- JSON-LD listing extraction (the Hedge Equities fix) ----

# Listing page emits a list of BlogPosting objects. This mirrors what
# hedgeequities.com/blogs actually sends — multiple BlogPosting blobs that
# Trafilatura would strip away as non-prose, leaving the LLM dateless.
SHOPIFY_STYLE_JSONLD = """
<html><head>
<script type="application/ld+json">
[
  {
    "@type": "BlogPosting",
    "headline": "Mutual Fund Advisory Services: Your Complete Investment Guide",
    "description": "Comprehensive guide to mutual fund advisory services for new investors.",
    "datePublished": "2026-05-12",
    "url": "https://acme.com/blogs/post/mutual-fund-advisory-services"
  },
  {
    "@type": "BlogPosting",
    "headline": "Share Market Portfolio Management: A Complete Guide",
    "description": "How to build and manage a share market portfolio.",
    "datePublished": "2026-05-11T08:30:00Z",
    "url": "https://acme.com/blogs/post/share-market-portfolio-management"
  },
  {
    "@type": "BlogPosting",
    "headline": "How to Choose the Best Investment Management Firms in 2026",
    "description": "Picking the right firm for your 2026 investments.",
    "datePublished": "2026-03-19",
    "url": "https://acme.com/blogs/post/how-to-choose"
  }
]
</script>
</head><body><h1>Blog</h1></body></html>
"""


def test_extract_jsonld_items_returns_per_card_dates():
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    items = _extract_jsonld_items(SHOPIFY_STYLE_JSONLD, "https://acme.com/blogs")
    assert len(items) == 3
    by_slug = {it["url"].rsplit("/", 1)[-1]: it for it in items}
    # Per-card date is authoritative; this is the fix Hedge Equities needed.
    assert by_slug["mutual-fund-advisory-services"]["published_date"] == "2026-05-12"
    assert by_slug["share-market-portfolio-management"]["published_date"] == "2026-05-11"
    assert by_slug["how-to-choose"]["published_date"] == "2026-03-19"
    # BlogPosting → category=blog
    assert all(it["category"] == "blog" for it in items)


def test_extract_jsonld_items_handles_graph_wrapper():
    """Yoast/Rank Math wrap everything in a top-level @graph array."""
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "Organization", "name": "Acme"},
        {"@type": "NewsArticle", "headline": "Big news",
         "url": "https://acme.com/news/big", "datePublished": "2026-04-01"}
      ]
    }
    </script></head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://acme.com/news")
    assert len(items) == 1
    assert items[0]["url"] == "https://acme.com/news/big"
    assert items[0]["category"] == "news"


def test_extract_jsonld_items_drops_items_missing_date():
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head><script type="application/ld+json">
    [
      {"@type": "BlogPosting", "headline": "Has date",
       "url": "https://acme.com/a", "datePublished": "2026-05-01"},
      {"@type": "BlogPosting", "headline": "No date",
       "url": "https://acme.com/b"}
    ]
    </script></head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://acme.com/")
    assert len(items) == 1
    assert items[0]["url"] == "https://acme.com/a"


def test_extract_jsonld_items_filters_cross_domain_urls():
    """JSON-LD often embeds publisher logos / related-site URLs. Drop those."""
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head><script type="application/ld+json">
    [
      {"@type": "BlogPosting", "headline": "On-site",
       "url": "https://acme.com/blog/x", "datePublished": "2026-05-01"},
      {"@type": "NewsArticle", "headline": "Cross-site publisher reference",
       "url": "https://otherpublisher.com/syndicated/y",
       "datePublished": "2026-05-02"}
    ]
    </script></head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://www.acme.com/blogs")
    # Cross-domain one filtered; same-eTLD+1 (acme.com vs www.acme.com) kept.
    assert len(items) == 1
    assert items[0]["url"] == "https://acme.com/blog/x"


def test_extract_jsonld_items_resolves_relative_urls():
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head><script type="application/ld+json">
    {"@type": "BlogPosting", "headline": "rel",
     "url": "/blog/post-1", "datePublished": "2026-05-10"}
    </script></head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://acme.com/blogs")
    assert items == [{
        "title": "rel",
        "summary": "rel",  # falls back to title when description missing
        "published_date": "2026-05-10",
        "url": "https://acme.com/blog/post-1",
        "category": "blog",
    }]


def test_extract_jsonld_items_skips_non_article_types():
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head><script type="application/ld+json">
    [
      {"@type": "Organization", "name": "Acme", "url": "https://acme.com"},
      {"@type": "WebSite", "url": "https://acme.com", "name": "Acme"},
      {"@type": "Person", "name": "Author"}
    ]
    </script></head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://acme.com/")
    assert items == []


def test_extract_jsonld_items_dedupes_by_url():
    """Same URL emitted by both the listing and a separate BlogPosting block."""
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "BlogPosting", "headline": "Post A",
     "url": "https://acme.com/blog/a", "datePublished": "2026-05-01"}
    </script>
    <script type="application/ld+json">
    {"@type": "BlogPosting", "headline": "Post A again",
     "url": "https://acme.com/blog/a", "datePublished": "2026-05-01"}
    </script>
    </head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://acme.com/")
    assert len(items) == 1


def test_extract_jsonld_items_tolerates_malformed_json():
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head>
    <script type="application/ld+json">{ this is not valid json</script>
    <script type="application/ld+json">
    {"@type": "BlogPosting", "headline": "Survivor",
     "url": "https://acme.com/blog/s", "datePublished": "2026-05-01"}
    </script>
    </head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://acme.com/")
    assert len(items) == 1
    assert items[0]["title"] == "Survivor"


def test_extract_jsonld_items_handles_url_as_dict():
    """Schema.org allows url to be a node reference like {"@id": "..."}."""
    pytest.importorskip("bs4")
    from src.extractor import _extract_jsonld_items

    html = """
    <html><head><script type="application/ld+json">
    {"@type": "BlogPosting", "headline": "Dict URL",
     "mainEntityOfPage": {"@id": "https://acme.com/blog/dict-url"},
     "datePublished": "2026-05-01"}
    </script></head><body></body></html>
    """
    items = _extract_jsonld_items(html, "https://acme.com/")
    assert len(items) == 1
    assert items[0]["url"] == "https://acme.com/blog/dict-url"
