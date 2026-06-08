from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .config import LLM_MODEL

# Per-1K-token prices: (input_price_usd, output_price_usd).
# Keep this in sync with OpenAI's pricing page when models or prices change.
_PRICE_PER_1K_TOKENS_USD = {
    "openai/gpt-4o-mini": (0.00015, 0.0006),
    "openai/gpt-4o": (0.0025, 0.010),
    "openai/gpt-4.1-mini": (0.0004, 0.0016),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "groq/llama-3.1-70b-versatile": (0.00059, 0.00079),
    "ollama/llama3.1": (0.0, 0.0),
}


def _normalize_model(model: str) -> str:
    """Match how prices are keyed. Real OpenAI usage responses give raw model
    names ("gpt-4o-mini"); our config uses "openai/gpt-4o-mini". Try both."""
    if model in _PRICE_PER_1K_TOKENS_USD:
        return model
    candidate = f"openai/{model}"
    if candidate in _PRICE_PER_1K_TOKENS_USD:
        return candidate
    return model  # unknown — _prices() falls back to a default


def _prices(model: str) -> tuple[float, float]:
    return _PRICE_PER_1K_TOKENS_USD.get(_normalize_model(model), (0.001, 0.003))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_call_usd(input_chars: int, expected_output_tokens: int = 800, model: str = LLM_MODEL) -> float:
    in_tok = max(1, input_chars // 4)
    in_price, out_price = _prices(model)
    return (in_tok / 1000) * in_price + (expected_output_tokens / 1000) * out_price


def cost_from_usage(prompt_tokens: int, completion_tokens: int, model: str = LLM_MODEL) -> float:
    """USD from REAL token counts (as returned by OpenAI's response.usage object).
    Same math as estimate_call_usd but with billed numbers instead of char-based guesses.
    """
    in_price, out_price = _prices(model)
    return (max(0, prompt_tokens) / 1000) * in_price + (max(0, completion_tokens) / 1000) * out_price


@dataclass
class UsageRecord:
    label: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_usd: float
    actual_usd: float


@dataclass
class CostMeter:
    """Tracks LLM cost across a single pipeline run. No cap — purely reporting.

    ``spent_usd`` reflects the running total: estimates for steps that didn't
    capture real token usage, real costs (via ``record_llm_usage``) for steps that did.

    Thread-safety: ``charge`` and ``record_llm_usage`` are guarded by an internal
    lock. The pipeline now runs data-source steps (extract / rss / search /
    linkedin) in parallel after ``discover_pages``, so two threads can land on
    the meter concurrently. ``self.spent_usd += x`` is read-modify-write on a
    float and NOT atomic under the GIL — the lock prevents lost updates.
    """
    spent_usd: float = 0.0
    # Sum of actual costs, computed from real token counts returned by the LLM.
    actual_usd: float = 0.0
    # Per-call record so cost-report scripts can see model breakdown and estimation drift.
    usage_records: list[UsageRecord] = field(default_factory=list)
    # repr/compare excluded so logging the dataclass doesn't print a lock object
    # and equality checks in tests still work.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def charge(self, usd: float, *, label: str = "") -> None:
        """Add an estimated cost to the running total. ``label`` is purely for logging."""
        with self._lock:
            self.spent_usd += usd

    def record_llm_usage(
        self,
        *,
        prior_estimate_usd: float,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        label: str = "",
    ) -> float:
        """Replace ``prior_estimate_usd`` in ``spent_usd`` with the real cost computed from
        token usage. Called AFTER an LLM call returns its ``usage`` object.

        The cap is intentionally not re-enforced here — money is already spent. If actual
        exceeds the estimate, the meter just reflects reality for downstream reporting.
        """
        actual = cost_from_usage(prompt_tokens, completion_tokens, model)
        record = UsageRecord(
            label=label,
            model=_normalize_model(model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_usd=prior_estimate_usd,
            actual_usd=actual,
        )
        with self._lock:
            self.spent_usd += actual - prior_estimate_usd
            self.actual_usd += actual
            self.usage_records.append(record)
        return actual
