"""Shared HTTP request headers for httpx calls across the pipeline.

Every plain-HTTP fetch (sitemap, anchor scan, common-path probe, RSS discovery,
extractor's httpx-first path) goes through ``request_headers()`` so we ship a
consistent set of "real browser" headers — User-Agent, Accept, Accept-Language,
and Sec-Fetch-* — instead of just a UA string. A couple of sites that fingerprint
header completeness (not just UA) treat the bare-UA request as "obvious bot."

UA rotation is randomised across a small pool of recent Chrome/Edge/Safari
strings. The pool is intentionally tiny — large UA pools sometimes include
deprecated browsers that themselves get blocked. Add to the pool when a real
target site rejects every entry.
"""
from __future__ import annotations

import random

# Real, current desktop browser User-Agents (May 2026 era).
# Keep this list short and recent — old strings are fingerprintable.
_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


def request_headers(*, accept_xml: bool = False) -> dict[str, str]:
    """Browser-realistic headers for an httpx GET.

    Args:
        accept_xml: when True (e.g. RSS feed fetches), the Accept header advertises
            XML alongside HTML so well-behaved servers send the XML variant.
    """
    accept = (
        "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
        if accept_xml
        else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    )
    # NOTE on Accept-Encoding: do NOT advertise `br` (brotli). The `requests` library
    # decodes gzip + deflate natively but only decodes brotli if the optional
    # `brotli` package is installed — which it isn't in this venv. If we advertise
    # `br` and the server picks brotli, we receive ~10× smaller binary garbage
    # instead of HTML. That broke the Paymentology homepage anchor-scan in the
    # post-W4 batch run (224 KB HTML → 20 KB unreadable bytes). Stick to the two
    # encodings `requests` always decodes.
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
