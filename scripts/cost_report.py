"""Aggregate cost across pipeline runs by scanning logs/*.json.

Usage:
    python scripts/cost_report.py [--since 7d] [--logs-dir logs/]

Prefers the *actual* cost (from OpenAI usage headers) when present in a log,
falls back to the estimated cost otherwise. Each log is one pipeline run.

Examples:
    python scripts/cost_report.py                  # last 7 days
    python scripts/cost_report.py --since 30d      # last 30 days
    python scripts/cost_report.py --since all      # everything in logs/
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def _parse_since(s: str) -> Optional[datetime]:
    s = s.strip().lower()
    if s in ("all", "*", ""):
        return None
    if s.endswith("d"):
        days = int(s[:-1])
        return datetime.now(timezone.utc) - timedelta(days=days)
    if s.endswith("h"):
        hours = int(s[:-1])
        return datetime.now(timezone.utc) - timedelta(hours=hours)
    # ISO date / datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise SystemExit(f"Bad --since value {s!r}. Use e.g. 7d, 24h, 2026-05-01, or 'all'.")


def _parse_started(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _load_log(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cost_of(log: dict) -> tuple[float, str]:
    """Pick the best cost available. Returns (cost, source) where source is one of
    'actual', 'spent', 'estimated' so we can show provenance.
    """
    totals = log.get("totals") or {}
    if (v := totals.get("actual_cost_usd")) not in (None, 0, 0.0):
        return float(v), "actual"
    if (v := totals.get("spent_cost_usd")) not in (None, 0, 0.0):
        return float(v), "spent"
    return float(totals.get("estimated_cost_usd") or 0.0), "estimated"


def _format_money(usd: float) -> str:
    return f"${usd:,.4f}" if usd < 1 else f"${usd:,.2f}"


def _format_row(left: str, right: str, width: int = 38) -> str:
    return left.ljust(width) + right


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate pipeline cost from logs/")
    parser.add_argument("--since", default="7d", help="Window (e.g. 7d, 24h, 2026-05-01, all). Default 7d.")
    parser.add_argument(
        "--logs-dir",
        default=str(Path(__file__).resolve().parent.parent / "logs"),
        help="Directory of JSON logs.",
    )
    parser.add_argument("--top", type=int, default=5, help="How many most-expensive runs to show.")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    if not logs_dir.is_dir():
        print(f"No logs dir at {logs_dir}", file=sys.stderr)
        return 1

    cutoff = _parse_since(args.since)

    # Collect
    runs: list[dict] = []
    for p in sorted(logs_dir.glob("*.json")):
        log = _load_log(p)
        if not log:
            continue
        started = _parse_started(log.get("started_at"))
        if cutoff and started and started < cutoff:
            continue
        cost, source = _cost_of(log)
        company = (log.get("inputs") or {}).get("company_url") or "(unknown)"
        sender = (log.get("inputs") or {}).get("sender_profile_name") or "(none)"
        steps = log.get("steps") or []
        runs.append({
            "path": p,
            "started": started,
            "cost": cost,
            "source": source,
            "company": company,
            "sender": sender,
            "steps": steps,
            "usage_records": log.get("usage_records") or [],
        })

    if not runs:
        print(f"No runs found in {logs_dir} for window {args.since}.")
        return 0

    total_cost = sum(r["cost"] for r in runs)
    total_runs = len(runs)
    actual_share = sum(1 for r in runs if r["source"] == "actual") / total_runs

    # Daily total
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        key = r["started"].date().isoformat() if r["started"] else "unknown"
        by_day[key].append(r)

    print()
    print("=" * 72)
    print(f" COST REPORT — window: {args.since}    runs: {total_runs}    total: {_format_money(total_cost)}")
    print(f" cost source: {actual_share:.0%} actual / {1 - actual_share:.0%} estimated")
    print("=" * 72)
    print()

    print("DAILY TOTAL")
    print("-" * 72)
    for day in sorted(by_day.keys()):
        rs = by_day[day]
        day_cost = sum(r["cost"] for r in rs)
        print(_format_row(day, f"{_format_money(day_cost)}  ({len(rs)} runs)"))
    avg = total_cost / total_runs
    print(_format_row("Total:", f"{_format_money(total_cost)}  ({total_runs} runs, avg {_format_money(avg)}/run)"))
    print()

    # Top N most-expensive runs
    print(f"TOP {args.top} MOST-EXPENSIVE RUNS")
    print("-" * 72)
    for r in sorted(runs, key=lambda r: r["cost"], reverse=True)[: args.top]:
        when = r["started"].strftime("%Y-%m-%d %H:%M") if r["started"] else "unknown"
        company = r["company"][:38].ljust(38)
        marker = "" if r["source"] == "actual" else f"  ({r['source']})"
        print(f"{_format_money(r['cost']):>10}  {when}  {company}{marker}")
    print()

    # Cost by step
    step_cost: dict[str, float] = defaultdict(float)
    step_count: dict[str, int] = defaultdict(int)
    for r in runs:
        for s in r["steps"]:
            name = s.get("step") or "(unknown)"
            delta = float(s.get("estimated_cost_usd_delta") or 0.0)
            if delta > 0:
                step_cost[name] += delta
                step_count[name] += 1
    if step_cost:
        total_step = sum(step_cost.values())
        print("COST BY STEP  (estimated per-step deltas)")
        print("-" * 72)
        for name in sorted(step_cost, key=step_cost.get, reverse=True):
            pct = (step_cost[name] / total_step * 100) if total_step else 0
            print(_format_row(f"{name}  ({step_count[name]}x)", f"{_format_money(step_cost[name])}  ({pct:.0f}%)"))
        print()

    # Cost by sender profile
    by_sender: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_sender[r["sender"]].append(r)
    if len(by_sender) > 1:
        print("COST BY SENDER PROFILE")
        print("-" * 72)
        for name in sorted(by_sender, key=lambda n: sum(r["cost"] for r in by_sender[n]), reverse=True):
            rs = by_sender[name]
            print(_format_row(name, f"{_format_money(sum(r['cost'] for r in rs))}  ({len(rs)} runs)"))
        print()

    # Cost by model — only available where we have actual usage records.
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"cost": 0.0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    for r in runs:
        for u in r["usage_records"]:
            m = u.get("model") or "unknown"
            by_model[m]["cost"] += float(u.get("actual_usd") or 0.0)
            by_model[m]["calls"] += 1
            by_model[m]["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
            by_model[m]["completion_tokens"] += int(u.get("completion_tokens") or 0)
    if by_model:
        print("ACTUAL USAGE BY MODEL  (from OpenAI response headers)")
        print("-" * 72)
        for m, stats in sorted(by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True):
            tok_in = stats["prompt_tokens"]
            tok_out = stats["completion_tokens"]
            print(_format_row(
                f"{m}  ({stats['calls']} calls)",
                f"{_format_money(stats['cost'])}   in={tok_in:,}  out={tok_out:,}",
            ))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
