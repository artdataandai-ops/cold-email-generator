"""Tests for scripts/scrape_batch.py URL normalisation + comparison logic."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from scrape_batch import _compare, _to_url, normalise_url  # noqa: E402


# ---- normalise_url: the basics (unchanged) ----

def test_normalise_strips_scheme_www_slash_fragment_query():
    cases = [
        "https://www.acme.com/blog/x",
        "http://www.acme.com/blog/x/",
        "https://acme.com/blog/x?utm_source=email",
        "https://acme.com/blog/x#section-2",
        "www.acme.com/blog/x",
    ]
    expected = "acme.com/blog/x"
    for url in cases:
        assert normalise_url(url) == expected, f"{url} → {normalise_url(url)}"


def test_normalise_empty_string():
    assert normalise_url("") == ""


# ---- region-prefix stripping (the new bit) ----

def test_normalise_strips_in_region_prefix():
    """The Stripe sessions-2026 case."""
    canonical = "https://stripe.com/newsroom/news/sessions-2026"
    regional = "https://stripe.com/in/newsroom/news/sessions-2026"
    assert normalise_url(canonical) == normalise_url(regional)
    assert normalise_url(regional) == "stripe.com/newsroom/news/sessions-2026"


def test_normalise_strips_locale_tag_en_gb():
    canonical = "https://example.com/blog/x"
    locale_form = "https://example.com/en-gb/blog/x"
    assert normalise_url(canonical) == normalise_url(locale_form)


def test_normalise_strips_country_codes_us_uk_de_fr():
    base = "https://acme.com/news/2026"
    assert normalise_url("https://acme.com/us/news/2026") == normalise_url(base)
    assert normalise_url("https://acme.com/uk/news/2026") == normalise_url(base)
    assert normalise_url("https://acme.com/de/news/2026") == normalise_url(base)
    assert normalise_url("https://acme.com/fr/news/2026") == normalise_url(base)


def test_normalise_does_not_strip_unknown_two_letter_paths():
    """If /ai/ isn't in the region list, it stays. Avoid eating real product paths."""
    out = normalise_url("https://acme.com/ai/news")
    assert out == "acme.com/ai/news"
    # /qq/ is also not a region — should stay
    out = normalise_url("https://acme.com/qq/news")
    assert out == "acme.com/qq/news"


def test_normalise_does_not_strip_region_when_its_the_only_segment():
    """e.g. stripe.com/in (just the regional homepage) shouldn't collapse to stripe.com.
    Otherwise two unrelated 'homepage' URLs would compare equal."""
    assert normalise_url("https://stripe.com/in") == "stripe.com/in"
    assert normalise_url("https://stripe.com/us") == "stripe.com/us"


def test_normalise_strips_only_one_region_prefix():
    """We don't recursively strip — /in/us/blog stays /us/blog (still strips one)."""
    out = normalise_url("https://acme.com/in/us/blog/x")
    # First strip removes /in, leaving /us/blog/x — we stop there.
    assert out == "acme.com/us/blog/x"


# ---- _compare: end-to-end (with the new normalisation) ----

def test_compare_matches_via_region_prefix():
    """The crucial integration: user curated the canonical URL, system returned
    the regional one. They should match under the new normalisation."""
    expected = ["https://stripe.com/newsroom/news/sessions-2026"]
    actual = [{
        "url": "https://stripe.com/in/newsroom/news/sessions-2026",
        "normalised": normalise_url("https://stripe.com/in/newsroom/news/sessions-2026"),
        "title": "Sessions 2026 highlights",
        "category": "news",
        "published_date": None,
    }]
    cmp = _compare(expected, actual)
    assert cmp["status"] == "PASS"
    assert cmp["matched_count"] == 1
    assert cmp["missed_count"] == 0


def test_compare_still_separates_truly_different_articles():
    """Defence: stripping region doesn't make every URL match every URL."""
    expected = ["https://stripe.com/newsroom/news/sessions-2026"]
    actual = [{
        "url": "https://stripe.com/in/newsroom/news/something-different",
        "normalised": normalise_url("https://stripe.com/in/newsroom/news/something-different"),
        "title": "Other article",
        "category": "news",
        "published_date": None,
    }]
    cmp = _compare(expected, actual)
    assert cmp["status"] == "FAIL"
    assert cmp["matched_count"] == 0


# ---- _to_url helper (existing) ----

def test_to_url_accepts_string():
    assert _to_url("https://acme.com/x") == "https://acme.com/x"


def test_to_url_accepts_dict():
    assert _to_url({"url": "https://acme.com/x"}) == "https://acme.com/x"


def test_to_url_handles_garbage():
    assert _to_url(None) == ""
    assert _to_url(42) == ""
    assert _to_url({}) == ""
