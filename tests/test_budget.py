"""Tests for budget.py (owner: stream A2).

Determinism: all timing is driven by injected fake clocks; no wall-clock
dependence beyond the case-clock accounting under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coverageflow import budget


class FakeClock:
    """Manually advanced seconds clock."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


LIMITS = {"rounds_max": 4, "retries_max": 3, "case_minutes": 45,
          "role_tokens_max": 60000, "approval_hours": 48}


def test_case_clock_breach_reports_precise_reason():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    clk.advance(45 * 60 + 1)
    ok, reason = b.check_dispatch(role="extractor")
    assert not ok
    assert reason["code"] == "case_clock"
    assert "45" in reason["message"]
    assert reason["elapsed_minutes"] > 45


def test_case_clock_exactly_at_limit_passes_then_fails():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    clk.advance(45 * 60 - 1)
    ok, _ = b.check_dispatch(role="extractor")
    assert ok
    clk.advance(2)
    ok, reason = b.check_dispatch(role="extractor")
    assert not ok and reason["code"] == "case_clock"


def test_role_token_cap_breach_when_role_at_cap():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    b.add_tokens("extractor", tokens_in=30000, tokens_out=30500)
    assert b.used_tokens("extractor") == 60500
    ok, reason = b.check_dispatch(role="extractor")
    assert not ok and reason["code"] == "role_tokens"


def test_role_token_cap_breach_message():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    b.add_tokens("extractor", tokens_in=30000, tokens_out=30000)
    ok, reason = b.check_dispatch(role="extractor")
    assert not ok
    assert reason["code"] == "role_tokens"
    assert reason["role"] == "extractor"
    assert reason["tokens"] == 60000
    assert reason["cap"] == 60000


def test_round_budget_check():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    b.note_round()
    b.note_round()
    b.note_round()
    assert b.used_rounds() == 3
    ok, _ = b.check_new_round()
    assert ok
    b.note_round()  # 4
    ok, reason = b.check_new_round()
    assert not ok
    assert reason["code"] == "rounds"
    assert reason["rounds"] == 4
    assert reason["max"] == 4


def test_clock_stops_during_approval_pause():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    clk.advance(600.0)
    elapsed_before = b.used.get("elapsed_minutes", 0.0)
    b.pause_clock()  # entering PENDING_APPROVAL
    clk.advance(48 * 3600)  # approval wait — clock must NOT run
    b.resume_clock()
    assert abs(b.used.get("elapsed_minutes", 0.0) - (elapsed_before + 10.0)) < 1e-6


def test_limit_parsing_from_mandate_and_defaults():
    b = budget.BudgetTracker(limits={})
    assert b.limits.rounds_max == 4
    assert b.limits.retries_max == 3
    assert b.limits.case_minutes == 120   # two ~25-minute live rounds plus headroom
    # the default has to afford the rounds budget it sits next to: a 4-round
    # case re-dispatches each role up to 4 times
    assert b.limits.role_tokens_max == 1200000
    assert b.limits.approval_hours == 48


def test_used_roundtrip_into_state_dict():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    b.add_tokens("extractor", 10, 20)
    b.note_round()
    d = b.used_state()
    assert d["rounds"] == 1
    assert d["tokens_by_role"]["extractor"] == 30
    b2 = budget.BudgetTracker(limits=LIMITS, used=d, clock=clk)
    b2.resume_clock()
    assert b2.used_tokens("extractor") == 30
    assert b2.used_rounds() == 1


def test_exhausted_never_grants_acceptance_helper():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    clk.advance(45 * 60 + 5)
    assert b.is_exhausted()
    ok, _ = b.check_acceptance()
    assert not ok  # exhaustion NEVER grants acceptance


def test_acceptance_is_granted_on_the_last_allowed_round():
    """Converging on the final round is success, not exhaustion."""
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    for _ in range(LIMITS["rounds_max"]):
        b.note_round()
    assert b.used_rounds() == LIMITS["rounds_max"]
    ok, reason = b.check_acceptance()
    assert ok, reason


def test_acceptance_refuses_a_rounds_overrun():
    clk = FakeClock()
    b = budget.BudgetTracker(limits=LIMITS, clock=clk)
    b.start_clock()
    for _ in range(LIMITS["rounds_max"] + 1):
        b.note_round()
    ok, reason = b.check_acceptance()
    assert not ok
    assert reason["code"] == "rounds"


def test_reset_tokens_clears_only_that_role():
    """A repaired block must not die of the tokens the broken attempt burned."""
    b = budget.BudgetTracker(limits=LIMITS, clock=FakeClock())
    b.add_tokens("model", 40000, 40000)
    b.add_tokens("extractor", 1000, 1000)
    b.reset_tokens("model")
    assert b.used_tokens("model") == 0
    assert b.used_tokens("extractor") == 2000


def test_grant_raises_caps_and_restarts_the_exhausted_role():
    b = budget.BudgetTracker(limits=LIMITS, clock=FakeClock())
    b.add_tokens("model", 40000, 34011)
    ok, reason = b.check_dispatch("model")
    assert not ok and reason["code"] == "role_tokens"

    record = b.grant(role="model", by="human")
    assert record["tokens_at_grant"] == {"model": 74011}
    assert record["role_tokens_max_after"] == 120000
    assert b.limits.role_tokens_max == 120000
    assert record["rounds_max_after"] == 8
    assert b.limits.rounds_max == 8
    assert b.used_tokens("model") == 0
    ok, reason = b.check_dispatch("model")
    assert ok and reason is None
    assert b.used["grants"] == [record]


def test_grant_survives_a_state_round_trip():
    b = budget.BudgetTracker(limits=LIMITS, clock=FakeClock())
    b.add_tokens("model", 40000, 34011)
    b.grant(role="model")
    state = {"budget": {**b.limits_state(), "used": b.used_state()}}
    again = budget.BudgetTracker.from_state(state, clock=FakeClock())
    assert again.limits.role_tokens_max == 120000
    assert again.limits.rounds_max == 8
    assert again.used_tokens("model") == 0
    assert len(again.used["grants"]) == 1
