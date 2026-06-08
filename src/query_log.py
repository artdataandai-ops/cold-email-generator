from __future__ import annotations

import getpass
import json
import platform
import socket
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import EMBEDDING_MODEL, LLM_MODEL, PROJECT_ROOT
from .cost_guard import estimate_tokens

LOGS_DIR = PROJECT_ROOT / "logs"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe_repr(value: Any, *, max_chars: int = 4000) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + f"... [truncated, {len(value)} chars total]"
    if isinstance(value, (list, tuple)):
        return [_safe_repr(v, max_chars=max_chars) for v in value][:50]
    if isinstance(value, dict):
        return {k: _safe_repr(v, max_chars=max_chars) for k, v in list(value.items())[:50]}
    try:
        return _safe_repr(json.loads(json.dumps(value, default=str)), max_chars=max_chars)
    except Exception:
        return repr(value)[:max_chars]


def _scrapegraphai_version() -> Optional[str]:
    try:
        from importlib.metadata import version
        return version("scrapegraphai")
    except Exception:
        return None


@dataclass
class StepRecord:
    step: str
    graph: Optional[str]
    started_at: str
    duration_seconds: float
    status: str
    input: Any
    output: Any
    estimated_tokens_in: int
    estimated_tokens_out: int
    estimated_cost_usd_delta: float
    cumulative_cost_usd: float
    error: Optional[str] = None
    traceback: Optional[str] = None


@dataclass
class QueryLogger:
    request_id: str
    started_at: str
    inputs: dict
    started_perf: float = field(default_factory=time.perf_counter)
    steps: list[StepRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    final: dict = field(default_factory=dict)
    log_path: Optional[Path] = None

    @classmethod
    def start(cls, inputs: dict) -> "QueryLogger":
        return cls(
            request_id=str(uuid.uuid4()),
            started_at=_utcnow_iso(),
            inputs=_safe_repr(inputs),
        )

    @contextmanager
    def step(self, name: str, *, graph: Optional[str] = None, input: Any = None) -> Iterator["StepCtx"]:
        ctx = StepCtx(name=name, graph=graph, input=input)
        ctx._t0 = time.perf_counter()
        ctx._started_at = _utcnow_iso()
        status = "success"
        error: Optional[str] = None
        tb: Optional[str] = None
        try:
            yield ctx
        except Exception as e:
            status = "error"
            error = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()
            raise
        finally:
            duration = time.perf_counter() - ctx._t0
            tokens_in = estimate_tokens(json.dumps(_safe_repr(ctx.input), default=str)) if ctx.input is not None else 0
            tokens_out = estimate_tokens(json.dumps(_safe_repr(ctx.output), default=str)) if ctx.output is not None else 0
            self.steps.append(
                StepRecord(
                    step=name,
                    graph=graph,
                    started_at=ctx._started_at,
                    duration_seconds=round(duration, 4),
                    status=status,
                    input=_safe_repr(ctx.input),
                    output=_safe_repr(ctx.output),
                    estimated_tokens_in=tokens_in,
                    estimated_tokens_out=tokens_out,
                    estimated_cost_usd_delta=round(ctx.cost_delta, 6),
                    cumulative_cost_usd=round(ctx.cumulative_cost, 6),
                    error=error,
                    traceback=tb,
                )
            )

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def finish(self, *, result: Any, email_text: str, meter: Any = None) -> Path:
        ended_at = _utcnow_iso()
        total_seconds = round(time.perf_counter() - self.started_perf, 4)
        self.final = {
            "ended_at": ended_at,
            "total_duration_seconds": total_seconds,
            "result_summary": _summarize_result(result),
            "email_drafted": bool(email_text),
            "email_length_chars": len(email_text or ""),
            "actual_cost_usd": round(getattr(meter, "actual_usd", 0.0) or 0.0, 6) if meter else None,
            "spent_cost_usd": round(getattr(meter, "spent_usd", 0.0) or 0.0, 6) if meter else None,
            "usage_records": [
                {
                    "label": u.label,
                    "model": u.model,
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "estimated_usd": round(u.estimated_usd, 6),
                    "actual_usd": round(u.actual_usd, 6),
                }
                for u in getattr(meter, "usage_records", []) or []
            ] if meter else [],
        }
        return self._write()

    def _write(self) -> Path:
        LOGS_DIR.mkdir(exist_ok=True)
        ts = self.started_at.replace(":", "").replace(".", "").replace("-", "")[:15]
        short_id = self.request_id.split("-")[0]
        path = LOGS_DIR / f"{ts}_{short_id}.json"
        payload = {
            "request_id": self.request_id,
            "started_at": self.started_at,
            "ended_at": self.final.get("ended_at"),
            "total_duration_seconds": self.final.get("total_duration_seconds"),
            "environment": {
                "host": socket.gethostname(),
                "user": getpass.getuser(),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "scrapegraphai_version": _scrapegraphai_version(),
            },
            "models": {
                "llm": LLM_MODEL,
                "embeddings": EMBEDDING_MODEL,
            },
            "inputs": self.inputs,
            "steps": [step.__dict__ for step in self.steps],
            "warnings": self.warnings,
            "result": self.final.get("result_summary"),
            "email": {
                "drafted": self.final.get("email_drafted"),
                "length_chars": self.final.get("email_length_chars"),
            },
            "totals": {
                "estimated_cost_usd": self.steps[-1].cumulative_cost_usd if self.steps else 0.0,
                "actual_cost_usd": self.final.get("actual_cost_usd"),
                "spent_cost_usd": self.final.get("spent_cost_usd"),
                "estimated_tokens_in": sum(s.estimated_tokens_in for s in self.steps),
                "estimated_tokens_out": sum(s.estimated_tokens_out for s in self.steps),
                "step_count": len(self.steps),
                "error_count": sum(1 for s in self.steps if s.status == "error"),
            },
            "usage_records": self.final.get("usage_records", []),
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        self.log_path = path
        return path


@dataclass
class StepCtx:
    name: str
    graph: Optional[str]
    input: Any
    output: Any = None
    cost_delta: float = 0.0
    cumulative_cost: float = 0.0
    _t0: float = 0.0
    _started_at: str = ""

    def set_output(self, value: Any) -> None:
        self.output = value

    def set_cost(self, *, delta: float, cumulative: float) -> None:
        self.cost_delta = delta
        self.cumulative_cost = cumulative


def _summarize_result(result: Any) -> dict:
    if result is None:
        return {}
    bundle = getattr(result, "bundle", result)
    items = getattr(bundle, "items", None) or []
    posts = getattr(bundle, "linkedin_posts", None) or []
    return {
        "company_url": getattr(bundle, "company_url", None),
        "linkedin_url": getattr(bundle, "linkedin_url", None),
        "items_count": len(items),
        "items_preview": [
            {
                "title": getattr(i, "title", None),
                "category": getattr(i, "category", None),
                "published_date": str(getattr(i, "published_date", "")),
                "url": getattr(i, "url", None),
            }
            for i in items[:10]
        ],
        "linkedin_posts_count": len(posts),
        "estimated_cost_usd": getattr(bundle, "estimated_cost_usd", 0.0),
        "warnings": list(getattr(bundle, "warnings", []) or []),
    }
