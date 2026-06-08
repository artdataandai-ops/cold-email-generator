"""Run the research pipeline against curated fixtures and compare against ground truth.

Fixture file: tests/fixtures/companies.json
Each company optionally has an ``expected_items`` array of {url} entries the SDR
manually curated. The script runs the pipeline, then compares system-found URLs
against the curated set (strict, after URL normalisation).

Per-company status:
    SKIP     no expected_items - just runs, no comparison
    PASS     matched == expected, missed == 0
    PARTIAL  matched > 0 and missed > 0
    FAIL     matched == 0 and expected > 0  (the silent-fail case)

Two outputs every run:
    * Human-readable summary table on stdout
    * Structured JSON report at output/scrape_eval/<timestamp>.json (or --save path)

Exit code: 0 if no FAIL rows, 1 otherwise.

Usage:
    python scripts/scrape_batch.py                          # full fixture, web search ON
    python scripts/scrape_batch.py --no-web-search          # cheaper / faster
    python scripts/scrape_batch.py --save my_report.json    # custom report path
    python scripts/scrape_batch.py --fixture other.json     # custom fixture file
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _import_pipeline():
    from src.pipeline import research_pipeline
    return research_pipeline


# -------- URL normalisation (the heart of the comparator) --------

_TRAILING_SLASH_RE = re.compile(r"/+$")

# Region/locale prefixes that companies use under their root path to localise the
# SAME article. Examples:
#   stripe.com/newsroom/news/sessions-2026   (canonical)
#   stripe.com/in/newsroom/news/sessions-2026  (India region — same article)
#   stripe.com/en-gb/newsroom/news/sessions-2026  (UK locale — same article)
# We strip one leading region segment so both forms collapse to the same key.
# Keep this list conservative — bare 2-letter strings collide with real product
# paths if we go too wide (e.g. /ai/news could be a product page, not a region).
_REGION_PREFIXES = frozenset({
    # 2-letter country/locale codes commonly used for region routing
    "in", "us", "uk", "ca", "au", "jp", "cn", "kr", "sg", "hk", "tw", "mx",
    "br", "de", "fr", "es", "it", "nl", "pl", "ru", "tr", "ae", "sa", "za",
    "eu", "ph", "id", "my", "th", "vn", "en",
    # 5-char locale tags (BCP-47 style)
    "en-us", "en-gb", "en-in", "en-au", "en-ca", "de-de", "fr-fr", "es-es",
    "it-it", "ja-jp", "zh-cn", "zh-tw", "pt-br", "es-mx",
})


def normalise_url(url: str) -> str:
    """Reduce a URL to a comparable form: lowercase, no scheme/www, no fragment,
    no query string, no trailing slash, no leading region-prefix segment.

    Region stripping is the key bit — without it, stripe.com/in/newsroom/news/x
    and stripe.com/newsroom/news/x look like different URLs, but they're the
    SAME article served under a region prefix.
    """
    if not url:
        return ""
    s = url.strip().lower()
    parsed = urlparse(s if "://" in s else f"https://{s}")
    host = (parsed.hostname or "").removeprefix("www.")
    path = _TRAILING_SLASH_RE.sub("", parsed.path or "")
    # Strip one leading region/locale segment if present.
    if path.startswith("/"):
        segments = path[1:].split("/", 1)
        if segments and segments[0] in _REGION_PREFIXES and len(segments) > 1:
            path = "/" + segments[1]
    return f"{host}{path}"


# -------- Fixture loading --------

def _load_fixture(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"Fixture file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("companies", [])


# -------- Per-company run + compare --------

def _run_one(
    company: dict,
    *,
    disable_web_search: bool,
    disable_linkedin: bool,
    require_context_for_email: bool,
    recency_days: Optional[int] = None,
) -> dict:
    research_pipeline = _import_pipeline()
    started = time.perf_counter()
    try:
        result = research_pipeline(
            company_url=company["url"],
            linkedin_url=None,
            disable_web_search=disable_web_search,
            disable_linkedin=disable_linkedin,
            require_context_for_email=require_context_for_email,
            recency_days=recency_days,
        )
        bundle = result.bundle
        actual_items = bundle.items or []
        actual_record = [
            {
                "url": str(it.url),
                "normalised": normalise_url(str(it.url)),
                "title": it.title,
                "category": it.category,
                # Sentiment + event_significance are informational only in
                # Phase 2a — surfaced here for manual inspection of LLM
                # classifications before any weight is applied in
                # src/relevance.py / src/email_writer.py.
                "sentiment": getattr(it, "sentiment", "neutral"),
                "event_significance": getattr(it, "event_significance", "notable"),
                "published_date": it.published_date.isoformat() if it.published_date else None,
            }
            for it in actual_items
        ]
        return {
            "ok": True,
            "actual_items": actual_record,
            "news_status": bundle.news_status,
            "news_status_detail": bundle.news_status_detail,
            "estimated_cost_usd": round(bundle.estimated_cost_usd or 0.0, 4),
            "warnings": list(bundle.warnings or []),
            "log_path": str(result.log_path) if result.log_path else None,
            "duration_s": round(time.perf_counter() - started, 1),
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False,
            "actual_items": [],
            "news_status": "error",
            "news_status_detail": None,
            "estimated_cost_usd": 0.0,
            "warnings": [],
            "log_path": None,
            "duration_s": round(time.perf_counter() - started, 1),
            "error": f"{type(e).__name__}: {e}",
        }


def _to_url(item) -> str:
    """Accept either a bare URL string OR a {url: '...'} dict — both are valid forms
    of expected_items in companies.json."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return (item.get("url") or "").strip()
    return ""


def _compare(expected: list, actual_items: list[dict]) -> dict:
    """Strict normalised-URL comparison. Returns the per-company comparison sub-report."""
    expected_normalised: dict[str, str] = {}
    for e in expected:
        raw = _to_url(e)
        if raw:
            expected_normalised[normalise_url(raw)] = raw
    actual_by_norm = {it["normalised"]: it for it in actual_items if it["normalised"]}

    matched_keys = expected_normalised.keys() & actual_by_norm.keys()
    missed_keys = expected_normalised.keys() - actual_by_norm.keys()
    surplus_keys = actual_by_norm.keys() - expected_normalised.keys()

    matched = [
        {
            "url": actual_by_norm[k]["url"],
            "normalised": k,
            "title": actual_by_norm[k].get("title"),
            "category": actual_by_norm[k].get("category"),
            "sentiment": actual_by_norm[k].get("sentiment"),
            "event_significance": actual_by_norm[k].get("event_significance"),
            "expected_url": expected_normalised[k],
        }
        for k in sorted(matched_keys)
    ]
    missed = [
        {
            "url": expected_normalised[k],
            "normalised": k,
        }
        for k in sorted(missed_keys)
    ]
    surplus = [
        {
            "url": actual_by_norm[k]["url"],
            "normalised": k,
            "title": actual_by_norm[k].get("title"),
            "category": actual_by_norm[k].get("category"),
            "sentiment": actual_by_norm[k].get("sentiment"),
            "event_significance": actual_by_norm[k].get("event_significance"),
        }
        for k in sorted(surplus_keys)
    ]

    if not expected:
        status = "SKIP"
    elif not matched:
        status = "FAIL"
    elif missed:
        status = "PARTIAL"
    else:
        status = "PASS"

    return {
        "status": status,
        "expected_count": len(expected_normalised),
        "actual_count": len(actual_by_norm),
        "matched_count": len(matched),
        "missed_count": len(missed),
        "surplus_count": len(surplus),
        "matched": matched,
        "missed": missed,
        "surplus": surplus,
    }


# -------- Rendering --------

def _short(s: Optional[str], n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "..."


def _print_table(rows: list[dict]) -> None:
    print()
    print("=" * 110)
    print(f"{'SCENARIO':<22} {'URL':<38} {'STATUS':<8} {'MATCH':>6} {'MISS':>5} {'SURPLUS':>8} {'COST':>9} {'TIME':>7}")
    print("-" * 110)
    for r in rows:
        cmp = r["comparison"]
        match_str = f"{cmp['matched_count']}/{cmp['expected_count']}" if cmp["status"] != "SKIP" else "-"
        miss_str = str(cmp["missed_count"]) if cmp["status"] != "SKIP" else "-"
        print(
            f"{_short(r['scenario'], 22):<22} "
            f"{_short(r['url'], 38):<38} "
            f"{cmp['status']:<8} "
            f"{match_str:>6} "
            f"{miss_str:>5} "
            f"{cmp['surplus_count']:>8} "
            f"${r['estimated_cost_usd']:>7.4f} "
            f"{r['duration_s']:>5.1f}s"
        )
    print("=" * 110)


def _print_summary(summary: dict) -> None:
    print(
        f"TOTAL: {summary['total_companies']} runs | "
        f"PASS {summary['pass']} | PARTIAL {summary['partial']} | "
        f"FAIL {summary['fail']} | SKIP {summary['skip']}    "
        f"missed={summary['total_missed']} surplus={summary['total_surplus']} "
        f"cost=${summary['total_cost_usd']:.4f}"
    )


def _print_misses(rows: list[dict]) -> None:
    """Spotlight the URLs we expected but didn't find. The whole point of this script."""
    miss_rows = [r for r in rows if r["comparison"]["missed_count"] > 0]
    if not miss_rows:
        return
    print()
    print("MISSED ITEMS  (you curated these, the system didn't return them)")
    print("-" * 110)
    for r in miss_rows:
        print(f"\n  {r['scenario']}  -  {r['url']}")
        for m in r["comparison"]["missed"]:
            print(f"    - {m['url']}")


# -------- Main --------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--fixture",
        default=str(REPO_ROOT / "tests" / "fixtures" / "companies.json"),
        help="Path to the companies fixture JSON.",
    )
    p.add_argument("--no-web-search", action="store_true", help="Disable web search step (cheaper).")
    p.add_argument(
        "--draft-email",
        action="store_true",
        help="Also run the email drafter (default: skip — this script is for scraping checks only).",
    )
    p.add_argument(
        "--recency-days",
        type=int,
        default=None,
        help=(
            "Override the recency window (days). If omitted, uses the saved app default "
            "(typically 90). Set this to MATCH whatever you'd use in the UI for the "
            "same prospect, OR widen it for testing so curated older articles aren't "
            "silently filtered out as 'too old'."
        ),
    )
    p.add_argument("--save", help="Path for the JSON report. Defaults to output/scrape_eval/<timestamp>.json.")
    p.add_argument(
        "--filter",
        default=None,
        metavar="SUBSTR",
        help=(
            "Run only fixtures whose URL or scenario contains this case-insensitive "
            "substring (e.g. --filter thredd). Useful for cheap iteration during fixes — "
            "avoids running the whole batch (and racking up tokens) for every change."
        ),
    )
    args = p.parse_args()

    fixture_path = Path(args.fixture)
    companies = _load_fixture(fixture_path)
    if not companies:
        print(f"No companies in {fixture_path}", file=sys.stderr)
        return 1

    if args.filter:
        needle = args.filter.lower()
        companies = [
            c for c in companies
            if needle in (c.get("url") or "").lower()
            or needle in (c.get("scenario") or "").lower()
        ]
        if not companies:
            print(f"No companies match --filter {args.filter!r}", file=sys.stderr)
            return 1

    ran_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    recency_label = f"{args.recency_days}d" if args.recency_days is not None else "saved-default"
    print(
        f"Running pipeline against {len(companies)} companies "
        f"(web_search={'OFF' if args.no_web_search else 'ON'}, recency={recency_label})"
    )

    rows: list[dict] = []
    for c in companies:
        print(f"  -> {c['url']}", flush=True)
        run = _run_one(
            c,
            disable_web_search=args.no_web_search,
            disable_linkedin=True,
            # Skip the email draft by default — this script is for scraping checks.
            # Set --draft-email to opt in.
            require_context_for_email=not args.draft_email,
            recency_days=args.recency_days,
        )
        cmp = _compare(c.get("expected_items", []) or [], run["actual_items"])
        row = {
            "url": c["url"],
            "scenario": c.get("scenario", ""),
            "notes": c.get("notes", ""),
            **{k: run[k] for k in ("news_status", "news_status_detail", "estimated_cost_usd",
                                   "warnings", "log_path", "duration_s", "error")},
            "comparison": cmp,
            "actual_items": run["actual_items"],
        }
        rows.append(row)
        marker = {"PASS": "+", "PARTIAL": "~", "FAIL": "X", "SKIP": "."}[cmp["status"]]
        print(
            f"    {marker} {cmp['status']:<7}  matched={cmp['matched_count']}/{cmp['expected_count']}  "
            f"missed={cmp['missed_count']}  surplus={cmp['surplus_count']}  "
            f"${run['estimated_cost_usd']:.4f}  {run['duration_s']:.1f}s"
        )

    summary = {
        "total_companies": len(rows),
        "pass":    sum(1 for r in rows if r["comparison"]["status"] == "PASS"),
        "partial": sum(1 for r in rows if r["comparison"]["status"] == "PARTIAL"),
        "fail":    sum(1 for r in rows if r["comparison"]["status"] == "FAIL"),
        "skip":    sum(1 for r in rows if r["comparison"]["status"] == "SKIP"),
        "total_cost_usd": round(sum(r["estimated_cost_usd"] for r in rows), 4),
        "total_missed":   sum(r["comparison"]["missed_count"] for r in rows),
        "total_surplus":  sum(r["comparison"]["surplus_count"] for r in rows),
    }

    _print_table(rows)
    _print_summary(summary)
    _print_misses(rows)

    # Write JSON report
    if args.save:
        report_path = Path(args.save)
    else:
        stamp = ran_at.replace(":", "").replace("-", "")
        report_path = REPO_ROOT / "output" / "scrape_eval" / f"{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "ran_at": ran_at,
        "fixture_path": str(fixture_path),
        "web_search_enabled": not args.no_web_search,
        "recency_days": args.recency_days,  # None = pipeline used saved DB default
        "companies": rows,
        "summary": summary,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON report -> {report_path}")

    return 0 if summary["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
