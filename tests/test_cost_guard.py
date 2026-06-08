"""Tests for the cost meter, real-token cost calc, and report aggregation logic."""
import pytest

from src.cost_guard import CostMeter, cost_from_usage, estimate_call_usd


# ---- cost_from_usage ----

def test_cost_from_usage_matches_published_gpt4o_mini_rates():
    # gpt-4o-mini: $0.00015 per 1K input, $0.0006 per 1K output.
    # 1000 prompt + 500 completion = 0.00015 + 0.0003 = 0.00045
    cost = cost_from_usage(1000, 500, "openai/gpt-4o-mini")
    assert cost == pytest.approx(0.00045, rel=1e-6)


def test_cost_from_usage_handles_raw_openai_model_name():
    # OpenAI's response.usage uses "gpt-4o-mini" not "openai/gpt-4o-mini".
    # Both should normalize to the same price row.
    a = cost_from_usage(1000, 500, "openai/gpt-4o-mini")
    b = cost_from_usage(1000, 500, "gpt-4o-mini")
    assert a == b > 0


def test_cost_from_usage_zero_tokens_zero_cost():
    assert cost_from_usage(0, 0, "openai/gpt-4o-mini") == 0.0


def test_cost_from_usage_negative_tokens_clamp_to_zero():
    # Defensive: a buggy upstream shouldn't make the meter run backwards.
    assert cost_from_usage(-10, -5, "openai/gpt-4o-mini") == 0.0


def test_cost_from_usage_unknown_model_uses_fallback_rate():
    # Unknown model → fallback (0.001, 0.003) per 1K. 1000 in + 1000 out = 0.004.
    cost = cost_from_usage(1000, 1000, "made-up-model-xyz")
    assert cost == pytest.approx(0.004, rel=1e-6)


# ---- CostMeter.charge ----

def test_charge_increments_spent():
    m = CostMeter()
    m.charge(0.10)
    m.charge(0.05)
    assert m.spent_usd == pytest.approx(0.15)


def test_charge_never_raises():
    """The cap was removed; the meter is now reporting-only."""
    m = CostMeter()
    m.charge(99999.0)  # should be a no-op apart from accumulation
    assert m.spent_usd == pytest.approx(99999.0)


# ---- record_llm_usage ----

def test_record_llm_usage_replaces_estimate_with_actual():
    m = CostMeter()
    estimate = 0.01
    m.charge(estimate, label="x")
    assert m.spent_usd == pytest.approx(0.01)
    # Real call came back: 500 input + 200 output on gpt-4o-mini.
    # actual = (500/1000)*0.00015 + (200/1000)*0.0006 = 0.000075 + 0.00012 = 0.000195
    actual = m.record_llm_usage(
        prior_estimate_usd=estimate,
        prompt_tokens=500,
        completion_tokens=200,
        model="openai/gpt-4o-mini",
        label="x",
    )
    assert actual == pytest.approx(0.000195, rel=1e-4)
    assert m.spent_usd == pytest.approx(0.000195, rel=1e-4)
    assert m.actual_usd == pytest.approx(0.000195, rel=1e-4)


def test_record_llm_usage_increases_when_actual_exceeds_estimate():
    """When our heuristic under-estimates, the true-up should add to spent."""
    m = CostMeter()
    estimate = 0.0001
    m.charge(estimate)
    # 10k tokens of output — way more than our cheap estimate.
    m.record_llm_usage(
        prior_estimate_usd=estimate,
        prompt_tokens=2000,
        completion_tokens=10000,
        model="openai/gpt-4o-mini",
        label="big",
    )
    assert m.spent_usd > estimate
    assert m.actual_usd > 0


def test_record_llm_usage_appends_to_usage_records():
    m = CostMeter()
    m.charge(0.001)
    m.record_llm_usage(
        prior_estimate_usd=0.001,
        prompt_tokens=100,
        completion_tokens=50,
        model="openai/gpt-4o-mini",
        label="step-a",
    )
    m.charge(0.001)
    m.record_llm_usage(
        prior_estimate_usd=0.001,
        prompt_tokens=200,
        completion_tokens=80,
        model="openai/gpt-4o-mini",
        label="step-b",
    )
    assert len(m.usage_records) == 2
    assert m.usage_records[0].label == "step-a"
    assert m.usage_records[1].label == "step-b"
    assert m.usage_records[0].prompt_tokens == 100
    assert m.usage_records[1].completion_tokens == 80


# ---- estimate_call_usd unchanged ----

def test_estimate_call_usd_still_works():
    # Sanity: existing callers shouldn't break.
    out = estimate_call_usd(input_chars=4000, expected_output_tokens=500, model="openai/gpt-4o-mini")
    assert out > 0
