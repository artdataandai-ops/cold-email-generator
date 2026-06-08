"""Tests for the shared request-headers helper."""
from src.http_headers import _USER_AGENTS, request_headers


def test_request_headers_returns_full_browser_envelope():
    h = request_headers()
    assert "User-Agent" in h and h["User-Agent"].startswith("Mozilla/5.0")
    assert h["Accept-Language"] == "en-US,en;q=0.9"
    assert "text/html" in h["Accept"]
    assert h["Sec-Fetch-Mode"] == "navigate"
    assert h["Upgrade-Insecure-Requests"] == "1"


def test_request_headers_accept_xml_variant():
    """Used by RSS fetches — advertise XML preference."""
    h = request_headers(accept_xml=True)
    assert "atom+xml" in h["Accept"]
    assert "rss+xml" in h["Accept"]
    # HTML still allowed as a fallback so generic gateways don't 406.
    assert "text/html" not in h["Accept"]  # explicitly XML-flavored
    assert "*/*" in h["Accept"]


def test_user_agent_pool_has_multiple_real_browsers():
    """Defence against accidentally shrinking to one UA — defeats rotation."""
    assert len(_USER_AGENTS) >= 3
    for ua in _USER_AGENTS:
        assert ua.startswith("Mozilla/5.0")
        # No deprecated/embarrassing strings
        assert "compatible; bot" not in ua.lower()


def test_request_headers_rotates_user_agent_across_calls(monkeypatch):
    """Over a few calls, we should see more than one UA — confirms random.choice
    is actually picking from the pool."""
    seen: set[str] = set()
    for _ in range(60):
        seen.add(request_headers()["User-Agent"])
    # With 4 UAs in the pool and 60 draws, we should see at least 2 distinct.
    assert len(seen) >= 2
