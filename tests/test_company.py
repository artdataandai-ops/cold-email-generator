"""Pure-logic tests for src/company.py helpers (no network, no LLM)."""
from unittest.mock import MagicMock, patch

from src.company import (
    _build_probe_urls,
    _COMMON_NEWS_PATHS,
    _homepage_anchor_scan,
    _NEWS_PATH_RE,
    _registrable_name,
    _same_brand,
    _score_listing_page,
)


def test_registrable_name_strips_www_and_tld():
    assert _registrable_name("www.thredd.com") == "thredd"
    assert _registrable_name("thredd.ai") == "thredd"
    assert _registrable_name("blog.thredd.com") == "thredd"


def test_registrable_name_handles_co_uk():
    assert _registrable_name("acme.co.uk") == "acme"
    assert _registrable_name("news.acme.co.uk") == "acme"


def test_registrable_name_one_part_host():
    assert _registrable_name("localhost") == "localhost"


def test_same_brand_across_tlds():
    assert _same_brand("https://thredd.com/", "https://thredd.ai/resources/foo")
    assert _same_brand("https://www.thredd.com/", "https://thredd.io/blog/x")


def test_same_brand_subdomain():
    assert _same_brand("https://thredd.com/", "https://blog.thredd.com/news/x")
    assert _same_brand("https://thredd.com/", "https://news.thredd.com/2026/launch")


def test_same_brand_rejects_unrelated():
    assert not _same_brand("https://thredd.com/", "https://socialnews.xyz/about-thredd")
    assert not _same_brand("https://thredd.com/", "https://tracxn.com/d/companies/thredd")


def test_same_brand_handles_empty_candidate():
    assert not _same_brand("https://thredd.com/", "")
    assert not _same_brand("https://thredd.com/", "/relative/path")


def test_news_path_re_matches_common_paths():
    paths_yes = [
        "/news/2026/launch",
        "/blog/post-title",
        "/press/releases",
        "/insights/industry",
        "/resources/whitepaper",
        "/articles/the-future",
        "/case-studies/acme",
        "/stories/customer-success",
        "/whitepapers/2026-report",
        "/library/knowledge-base",
        "/perspectives/cto-corner",
        "/Newsroom/index",  # case-insensitive
    ]
    for p in paths_yes:
        assert _NEWS_PATH_RE.search(p), f"expected match: {p}"


def test_news_path_re_rejects_non_news_paths():
    paths_no = [
        "/about",
        "/pricing",
        "/products/foo",
        "/careers",
        "/contact",
        "/login",
        "/api/v1",
    ]
    for p in paths_no:
        assert not _NEWS_PATH_RE.search(p), f"expected NO match: {p}"


def test_build_probe_urls_builds_from_base():
    out = _build_probe_urls("https://www.thredd.ai/some/deep/page", cap=4)
    assert out == [
        "https://www.thredd.ai/news",
        "https://www.thredd.ai/blog",
        "https://www.thredd.ai/press",
        "https://www.thredd.ai/newsroom",
    ]


def test_build_probe_urls_respects_cap():
    out = _build_probe_urls("https://acme.com/", cap=2)
    assert len(out) == 2
    assert out == ["https://acme.com/news", "https://acme.com/blog"]


def test_build_probe_urls_zero_or_negative_cap():
    assert _build_probe_urls("https://acme.com/", cap=0) == []
    assert _build_probe_urls("https://acme.com/", cap=-3) == []


def test_build_probe_urls_covers_resources():
    # The thredd.ai bug that triggered this fallback would have surfaced
    # /resources/industry-insights/... — verify the probe at least visits /resources.
    out = _build_probe_urls("https://www.thredd.ai/", cap=len(_COMMON_NEWS_PATHS))
    assert "https://www.thredd.ai/resources" in out


def test_build_probe_urls_includes_two_segment_newsroom_paths():
    """The Thredd failure: expected URLs lived on /company/press-releases/, which
    wasn't in the probe list. Verify it (and related two-segment paths) is now there."""
    out = _build_probe_urls("https://www.thredd.ai/", cap=len(_COMMON_NEWS_PATHS))
    assert "https://www.thredd.ai/company/press-releases" in out
    assert "https://www.thredd.ai/media-center" in out
    assert "https://www.thredd.ai/about/news" in out


def test_company_press_releases_within_first_eight_probes():
    """Critical: /company/press-releases must be in the top-8 probes so it survives
    the default cap=8 even when anchor scan + sitemap are empty (e.g. Thredd
    when Cloudflare 403s the homepage). Position-sensitive — earlier was at #15."""
    out = _build_probe_urls("https://www.thredd.ai/", cap=8)
    assert "https://www.thredd.ai/company/press-releases" in out
    # And the other canonical compound newsroom paths should be reachable too
    full = _build_probe_urls("https://www.thredd.ai/", cap=len(_COMMON_NEWS_PATHS))
    top8 = full[:8]
    assert any("press-releases" in u for u in top8), top8
    assert any("newsroom" in u for u in top8), top8


# ---- Sitemap parsing (W7 + W8) ----

def test_parse_sitemap_locs_extracts_loc_and_lastmod():
    """Standard urlset with lastmod entries."""
    from src.company import _parse_sitemap_locs
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://acme.com/newsroom/article-a</loc>
        <lastmod>2026-05-10</lastmod>
      </url>
      <url>
        <loc>https://acme.com/newsroom/article-b</loc>
        <lastmod>2026-04-20</lastmod>
      </url>
      <url>
        <loc>https://acme.com/about</loc>
      </url>
    </urlset>
    """
    out = _parse_sitemap_locs(xml)
    assert len(out) == 3
    assert ("https://acme.com/newsroom/article-a", "2026-05-10") in out
    assert ("https://acme.com/newsroom/article-b", "2026-04-20") in out
    assert ("https://acme.com/about", None) in out


def test_parse_sitemap_locs_handles_sitemap_index():
    """sitemapindex with <sitemap> children pointing to child sitemaps."""
    from src.company import _parse_sitemap_locs
    xml = """<sitemapindex>
      <sitemap><loc>https://acme.com/sitemap-news.xml</loc></sitemap>
      <sitemap><loc>https://acme.com/sitemap-blog.xml</loc><lastmod>2026-05-01</lastmod></sitemap>
    </sitemapindex>
    """
    out = _parse_sitemap_locs(xml)
    assert len(out) == 2
    assert ("https://acme.com/sitemap-news.xml", None) in out


def test_parse_sitemap_locs_tolerant_to_malformed_xml():
    """Real-world sitemaps often have unescaped & or stray whitespace.
    The regex parser should still extract what's there."""
    from src.company import _parse_sitemap_locs
    xml = "<urlset><url>\n<loc>https://x.com/a&b</loc>\n</url></urlset>"
    out = _parse_sitemap_locs(xml)
    # Regex is more permissive than strict XML parsing — we don't strictly
    # require this to return [(loc, None)], but it should NOT raise.
    # The point is: never crash on real-world sitemaps.
    assert isinstance(out, list)


def test_sitemap_paths_from_robots_extracts_sitemap_directive(monkeypatch):
    """Thredd's robots.txt: 'Sitemap: https://www.thredd.ai/sitemap' (no .xml)."""
    from src.company import _sitemap_paths_from_robots
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = 200
    resp.text = (
        "User-agent: *\n"
        "Sitemap: https://www.thredd.ai/sitemap\n"
        "Disallow: /*?query=*\n"
        "sitemap:    https://www.thredd.ai/news-sitemap.xml\n"  # case-insensitive
    )
    monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

    out = _sitemap_paths_from_robots("https://www.thredd.ai", {})
    assert "https://www.thredd.ai/sitemap" in out
    assert "https://www.thredd.ai/news-sitemap.xml" in out


def test_sitemap_paths_from_robots_empty_on_404(monkeypatch):
    from src.company import _sitemap_paths_from_robots
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = 404
    resp.text = ""
    monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

    assert _sitemap_paths_from_robots("https://x.com", {}) == []


def test_sitemap_paths_from_robots_no_sitemap_directive(monkeypatch):
    from src.company import _sitemap_paths_from_robots
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = 200
    resp.text = "User-agent: *\nDisallow: /private/\n"
    monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

    assert _sitemap_paths_from_robots("https://x.com", {}) == []


def test_build_probe_urls_includes_region_prefixed_paths():
    """The Stripe failure had /in/newsroom URLs as the real path. Verify the
    region-prefixed variants are now probed for any company."""
    out = _build_probe_urls("https://stripe.com/", cap=len(_COMMON_NEWS_PATHS))
    assert "https://stripe.com/in/newsroom" in out
    assert "https://stripe.com/en/newsroom" in out


def test_build_probe_urls_full_list_when_cap_is_none():
    """When cap is omitted, the script wants ALL probe URLs to be HTTP-checked
    before truncating to survivors. Confirm the helper returns the full set."""
    out = _build_probe_urls("https://acme.com/")
    assert len(out) == len(_COMMON_NEWS_PATHS)


# ---- HTML listing-page scoring ----

def test_score_listing_page_rewards_news_signals():
    html = """
    <html><body>
      <main>
        <article><h2>First post</h2><p>Posted March 15, 2026</p></article>
        <article><h2>Second post</h2><p>Posted February 1, 2026</p></article>
        <article><h2>Third post</h2><p>Posted 2026-01-10</p></article>
        <a href="/x">x</a><a href="/y">y</a><a href="/z">z</a>
      </main>
    </body></html>
    """
    # 3 articles (6) + 3 h2 (3) + 3 dates (6) = 15. Link bonus only fires above 15.
    assert _score_listing_page(html) >= 10


def test_score_listing_page_low_for_marketing_landing():
    html = """
    <html><body>
      <header>Acme Inc</header>
      <section><h1>Welcome to our product</h1><p>It's great.</p></section>
      <a href="/about">About</a>
    </body></html>
    """
    assert _score_listing_page(html) < 3


# ---- Homepage anchor scan ----

_HOMEPAGE_WITH_NEWS_LINKS = """
<html><body>
  <nav>
    <a href="/about">About</a>
    <a href="/products">Products</a>
    <a href="/newsroom">Newsroom</a>
    <a href="/blog">Blog</a>
  </nav>
  <main>
    <a href="https://acme.com/whats-new">What's new</a>
    <a href="/case-studies/big-customer">Case study: Big Customer Inc</a>
  </main>
  <footer>
    <a href="/press-releases">Press releases</a>
    <a href="javascript:void(0)">Bad</a>
    <a href="mailto:hi@acme.com">Email</a>
  </footer>
</body></html>
"""


def _mock_get(text: str, status: int = 200, url: str = "https://acme.com/"):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.url = url
    return resp


def test_anchor_scan_finds_news_keywords_in_href():
    with patch("requests.get", return_value=_mock_get(_HOMEPAGE_WITH_NEWS_LINKS)):
        results = _homepage_anchor_scan("https://acme.com/")
    urls = [u for u, _ in results]
    assert "https://acme.com/newsroom" in urls
    assert "https://acme.com/blog" in urls
    assert "https://acme.com/press-releases" in urls


def test_anchor_scan_drops_unrelated_links_and_junk():
    with patch("requests.get", return_value=_mock_get(_HOMEPAGE_WITH_NEWS_LINKS)):
        results = _homepage_anchor_scan("https://acme.com/")
    urls = [u for u, _ in results]
    assert "https://acme.com/about" not in urls
    assert "https://acme.com/products" not in urls
    assert not any(u.startswith("javascript:") for u in urls)
    assert not any(u.startswith("mailto:") for u in urls)


def test_anchor_scan_drops_cross_brand_links():
    """Links to unrelated domains (different brand) must not pollute results."""
    html = """
    <html><body>
      <a href="https://different-brand.com/blog">External blog</a>
      <a href="/blog">Own blog</a>
    </body></html>
    """
    with patch("requests.get", return_value=_mock_get(html)):
        results = _homepage_anchor_scan("https://acme.com/")
    urls = [u for u, _ in results]
    assert "https://acme.com/blog" in urls
    assert "https://different-brand.com/blog" not in urls


def test_anchor_scan_returns_empty_on_http_failure():
    with patch("requests.get", side_effect=Exception("DNS error")):
        results = _homepage_anchor_scan("https://acme.com/")
    assert results == []


def test_anchor_scan_returns_empty_on_non_200():
    with patch("requests.get", return_value=_mock_get("blocked", status=403)):
        results = _homepage_anchor_scan("https://cloudflare-protected.com/")
    assert results == []


def test_anchor_scan_path_match_outranks_text_only():
    """A URL with the keyword in the PATH should score higher."""
    html = """
    <html><body>
      <a href="/team">News from our CEO</a>
      <a href="/blog">Blog</a>
    </body></html>
    """
    with patch("requests.get", return_value=_mock_get(html)):
        results = _homepage_anchor_scan("https://acme.com/")
    # /blog (path match) should outscore /team (text-only match).
    assert results[0][0] == "https://acme.com/blog"


# ---- SPA shell detection (so _validate_listing_candidates keeps SPA newsrooms) ----

def test_looks_like_spa_shell_react_shell():
    from src.company import _looks_like_spa_shell
    react_shell = """
    <!doctype html>
    <html><head><title>News</title></head>
    <body>
      <div id="root"></div>
      <script src="/static/js/main.js"></script>
    </body></html>
    """
    assert _looks_like_spa_shell(react_shell) is True


def test_looks_like_spa_shell_rejects_real_listing_with_articles():
    from src.company import _looks_like_spa_shell
    # 60KB of static HTML with real <article> tags is not an SPA shell.
    real_page = "<html><body>" + ("<article><h2>Headline</h2><p>Body text...</p></article>" * 200) + "</body></html>"
    assert _looks_like_spa_shell(real_page) is False


def test_looks_like_spa_shell_rejects_static_page_without_mount():
    """A plain static page without a #root/#app div should not be flagged."""
    from src.company import _looks_like_spa_shell
    static = "<html><body><p>Hello</p></body></html>"
    assert _looks_like_spa_shell(static) is False


def test_looks_like_spa_shell_rejects_empty_html():
    from src.company import _looks_like_spa_shell
    assert _looks_like_spa_shell("") is False
    assert _looks_like_spa_shell(None) is False  # defensive
