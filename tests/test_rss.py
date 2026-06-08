"""Pure-logic tests for src/rss.py (no network)."""
from src.rss import _base_url, _looks_like_feed, _strip_html, parse_rss_feed


RSS_2_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Co Blog</title>
    <link>https://example-co.com/blog</link>
    <item>
      <title>New product launch: WidgetPro</title>
      <link>https://example-co.com/blog/widgetpro</link>
      <pubDate>Mon, 15 Apr 2026 09:00:00 +0000</pubDate>
      <description>&lt;p&gt;We launched WidgetPro today.&lt;/p&gt; It's great.</description>
    </item>
    <item>
      <title>Q1 partnership news</title>
      <link>https://example-co.com/blog/q1-partnership</link>
      <pubDate>Tue, 02 Apr 2026 10:30:00 +0000</pubDate>
      <description>Quarter one partnership round-up.</description>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Acme Inc Updates</title>
  <link href="https://acme.example/" rel="alternate"/>
  <id>tag:acme.example,2026:/feed</id>
  <entry>
    <title>Series B announcement</title>
    <link href="https://acme.example/news/series-b" rel="alternate"/>
    <link href="https://acme.example/edit/series-b" rel="edit"/>
    <published>2026-05-01T08:00:00Z</published>
    <updated>2026-05-02T08:00:00Z</updated>
    <summary>Raised $40M in Series B.</summary>
  </entry>
  <entry>
    <title>New CTO joins</title>
    <link href="https://acme.example/news/cto"/>
    <published>2026-04-20T08:00:00Z</published>
    <summary>Welcome to the team.</summary>
  </entry>
</feed>
"""


def test_parse_rss_2_extracts_all_items():
    items = parse_rss_feed(RSS_2_SAMPLE, "https://example-co.com/feed")
    assert len(items) == 2
    assert items[0]["title"] == "New product launch: WidgetPro"
    assert items[0]["url"] == "https://example-co.com/blog/widgetpro"
    assert "WidgetPro" in items[0]["summary"]
    # HTML tags in <description> get stripped.
    assert "<p>" not in items[0]["summary"]


def test_parse_atom_prefers_alternate_link():
    items = parse_rss_feed(ATOM_SAMPLE, "https://acme.example/feed.xml")
    assert len(items) == 2
    # Should pick the rel=alternate link, not rel=edit.
    assert items[0]["url"] == "https://acme.example/news/series-b"
    assert items[1]["url"] == "https://acme.example/news/cto"


def test_parse_atom_uses_published_over_updated():
    items = parse_rss_feed(ATOM_SAMPLE, "https://acme.example/feed.xml")
    # _find_child_text returns the first matching tag — published, then updated.
    assert items[0]["date"] == "2026-05-01T08:00:00Z"


def test_parse_rejects_malformed_xml():
    assert parse_rss_feed("<not valid xml", "x") == []


def test_parse_rejects_unknown_root():
    assert parse_rss_feed("<?xml version='1.0'?><other><item/></other>", "x") == []


def test_strip_html_collapses_whitespace_and_tags():
    out = _strip_html("<p>Hello   <b>world</b>\n\nthere</p>")
    assert out == "Hello world there"


def test_base_url_normalizes_input():
    assert _base_url("https://www.example.com/blog/x") == "https://www.example.com"
    assert _base_url("http://example.com") == "http://example.com"
    # Bare host
    assert _base_url("example.com").endswith("example.com")


def test_looks_like_feed_content_type():
    assert _looks_like_feed("anything", "application/rss+xml")
    assert _looks_like_feed("anything", "text/xml; charset=utf-8")
    assert _looks_like_feed("anything", "application/atom+xml")


def test_looks_like_feed_body_sniff():
    assert _looks_like_feed("<?xml version='1.0'?><rss>", "text/html")
    assert _looks_like_feed("  <feed xmlns='...'>", "")
    assert not _looks_like_feed("<html>just a 404 page</html>", "text/html")
