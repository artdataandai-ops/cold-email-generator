from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = PROJECT_ROOT / ".cache"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_LINKEDIN_ACTOR = os.getenv("APIFY_LINKEDIN_ACTOR", "LQQIXN9Othf8f7R5n")
APIFY_LINKEDIN_POST_LIMIT = int(os.getenv("APIFY_LINKEDIN_POST_LIMIT", "2"))
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
RECENCY_DAYS = int(os.getenv("RECENCY_DAYS", "60"))
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
MAX_NEWS_ITEMS = int(os.getenv("MAX_NEWS_ITEMS", "8"))
MAX_DISCOVERED_URLS_DEFAULT = int(os.getenv("MAX_DISCOVERED_URLS", "8"))
DISABLE_WEB_SEARCH_DEFAULT = os.getenv("DISABLE_WEB_SEARCH", "true").lower() in ("true", "1", "yes")
DISABLE_LINKEDIN_DEFAULT = os.getenv("DISABLE_LINKEDIN", "false").lower() in ("true", "1", "yes")
REQUIRE_CONTEXT_FOR_EMAIL_DEFAULT = os.getenv("REQUIRE_CONTEXT_FOR_EMAIL", "false").lower() in ("true", "1", "yes")

# Playwright/loader knobs (all optional). Defaults reproduce scrapegraphai's stock
# behavior so existing setups don't change.
SCRAPEGRAPH_PROXY_SERVER = os.getenv("SCRAPEGRAPH_PROXY_SERVER", "").strip()
SCRAPEGRAPH_PROXY_USERNAME = os.getenv("SCRAPEGRAPH_PROXY_USERNAME", "").strip()
SCRAPEGRAPH_PROXY_PASSWORD = os.getenv("SCRAPEGRAPH_PROXY_PASSWORD", "").strip()
SCRAPEGRAPH_LOAD_STATE = os.getenv("SCRAPEGRAPH_LOAD_STATE", "domcontentloaded").strip()
SCRAPEGRAPH_RETRY_LIMIT = int(os.getenv("SCRAPEGRAPH_RETRY_LIMIT", "2"))
SCRAPEGRAPH_PLAYWRIGHT_TIMEOUT = int(os.getenv("SCRAPEGRAPH_PLAYWRIGHT_TIMEOUT", "60"))

def domain_slug(url: str) -> str:
    host = urlparse(url).hostname or "unknown"
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", host)


def _cache_path_for(url: str) -> str:
    p = CACHE_ROOT / domain_slug(url)
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _llm_block() -> dict:
    block: dict = {"model": LLM_MODEL, "temperature": 0}
    if LLM_MODEL.startswith("openai/") or LLM_MODEL.startswith("gpt"):
        block["api_key"] = OPENAI_API_KEY
    return block


def _embeddings_block() -> dict:
    block: dict = {"model": EMBEDDING_MODEL}
    if EMBEDDING_MODEL.startswith("openai/"):
        block["api_key"] = OPENAI_API_KEY
    return block


def _proxy_block() -> Optional[dict]:
    """Return a Playwright proxy dict, or None if no proxy configured.

    Two modes scrapegraphai supports:
    - "broker" → free-proxy rotation (slow/unreliable; useful as a last resort).
    - explicit "http(s)://host:port" → routes Playwright through your own proxy.
    """
    if not SCRAPEGRAPH_PROXY_SERVER:
        return None
    proxy: dict = {"server": SCRAPEGRAPH_PROXY_SERVER}
    if SCRAPEGRAPH_PROXY_USERNAME:
        proxy["username"] = SCRAPEGRAPH_PROXY_USERNAME
    if SCRAPEGRAPH_PROXY_PASSWORD:
        proxy["password"] = SCRAPEGRAPH_PROXY_PASSWORD
    return proxy


def _loader_kwargs_block() -> dict:
    """Build loader_kwargs that scrapegraphai forwards to its ChromiumLoader.

    Keys we set:
    - load_state: "networkidle" waits for JS to settle (slower, better for SPAs);
      "domcontentloaded" is the scrapegraph default.
    - retry_limit: scrapegraph defaults to 1 (no retries). Bumping to 2 buys one
      retry on transient failures at no cost when the first try succeeds.
    - timeout: per-page Playwright timeout in seconds.
    - proxy: only included if SCRAPEGRAPH_PROXY_SERVER is set.
    """
    kwargs: dict = {
        "load_state": SCRAPEGRAPH_LOAD_STATE,
        "retry_limit": SCRAPEGRAPH_RETRY_LIMIT,
        "timeout": SCRAPEGRAPH_PLAYWRIGHT_TIMEOUT,
    }
    proxy = _proxy_block()
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def build_graph_config(source_url: str) -> dict:
    return {
        "llm": _llm_block(),
        "embeddings": _embeddings_block(),
        "verbose": False,
        "headless": True,
        "cache_path": _cache_path_for(source_url),
        "max_results": SEARCH_MAX_RESULTS,
        "loader_kwargs": _loader_kwargs_block(),
    }


def build_stealth_config(source_url: str) -> dict:
    """Heavier-weight config for sites that won't render under defaults.

    Differences from build_graph_config:
    - headless=False so a real browser window is used (some Cloudflare flavours
      let through visible Chrome but not headless).
    - load_state="networkidle" waits for the page's JS to actually finish.
    Cost: pops a browser window and is slower. Caller's responsibility to gate.
    """
    cfg = build_graph_config(source_url)
    cfg["headless"] = False
    cfg["loader_kwargs"] = {
        **cfg["loader_kwargs"],
        "load_state": "networkidle",
    }
    return cfg
