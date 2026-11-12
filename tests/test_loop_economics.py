"""Repair-loop economics: stop early on defects no role can fix, patch-only
writer retries, schema-bound extraction units, and a booked case clock."""

import json

import casekit
from tests.test_orchestrator import build_env, load_state

from coverageflow import extraction, gates
from coverageflow.ledger import FindingsLedger
from coverageflow.orchestrator import flagged_line_numbers, patch_flagged_lines


def _mandate(**kw):
    a = {"wacc": 0.1354, "terminal_growth": 0.025, "exit_multiple_ebitda": 21.5,
         "tax_rate": 0.21}
    a.update(kw)
    return {"assumptions": a}


def test_amd_mandate_is_infeasible_before_any_round():
    r = gates.terminal_feasibility(_mandate())
    assert r["checked"] and not r["feasible"]
    assert 3.0 < r["best_case_ratio"] < 3.2
    f = gates.terminal_feasibility_finding(_mandate())
    assert f["owner"] == "mandate" and "at most" in f["summary"]


def test_consistent_mandate_is_feasible():
    assert gates.terminal_feasibility(_mandate(exit_multiple_ebitda=8.0))["feasible"]
    assert gates.terminal_feasibility_finding(_mandate(exit_multiple_ebitda=8.0)) is None


def test_feasibility_skips_unusable_inputs():
    assert not gates.terminal_feasibility({"assumptions": {}})["checked"]
    assert not gates.terminal_feasibility(_mandate(terminal_growth=0.2))["checked"]


def test_ledger_reassign_owner_keeps_history(tmp_path):
    led = FindingsLedger(tmp_path)
    fid = led.open_finding({"source_critic": "gate:terminal_reconciliation",
                            "gate": "terminal_reconciliation",
                            "dedup_key": "terminal:outside-tolerance",
                            "severity": "major", "summary": "x",
                            "affected_artifacts": ["valuation.json"]}, round=1)
    assert led.owner_of(fid) == "model"
    led.reassign_owner(fid, "mandate", "unchanged")
    assert led.owner_of(fid) == "mandate"
    assert led.stopping_findings()
    assert led.open_findings()[0]["owner_history"][0]["owner"] == "model"


def test_patch_only_restores_unflagged_lines():
    prev = "a 1\nb 2 [EV:x]\nc 3\n"
    new = "a 9 [EV:y]\nb 2 [EV:x]\nc 3 [EV:z]\n"
    assert patch_flagged_lines(prev, new, {3}) == "a 1\nb 2 [EV:x]\nc 3 [EV:z]\n"
    # a reflowed note (extra line added elsewhere) still only keeps the fix
    new2 = "intro\n" + new
    assert patch_flagged_lines(prev, new2, {3}) == "a 1\nb 2 [EV:x]\nc 3 [EV:z]\n"
    assert flagged_line_numbers('numeric token "3" on line 3 lacks x; line 12') == {3, 12}


def test_patch_only_always_takes_the_new_provenance_header():
    hdr = "> **Cutoff:** 2026-11-04 · **Bundle:** b / s\n> **Candidate:** c · **Request:** {}\n"
    prev = hdr.format("req-1") + "\nRevenue 12.7\n"
    new = hdr.format("req-2") + "\nRevenue 12.7 [EV:a]\n"
    out = patch_flagged_lines(prev, new, {4})
    assert "req-2" in out and "[EV:a]" in out


def test_unit_schema_is_strict_and_matches_required_keys():
    from coverageflow.dispatch import _SCHEMA_REQUIRED_KEYS
    sch = extraction.UNIT_JSON_SCHEMA
    assert sch["additionalProperties"] is False
    assert set(sch["required"]) == set(sch["properties"])
    assert tuple(sch["required"]) == _SCHEMA_REQUIRED_KEYS["facts-unit.json"]
    json.dumps(sch)


def test_infeasible_mandate_blocks_before_any_role_runs(tmp_path):
    mandate = json.loads(json.dumps(casekit.MANDATE))
    mandate["assumptions"]["exit_multiple_ebitda"] = 40.0
    mandate["assumptions"]["wacc"] = 0.14
    orch, dispatch, clk, case_dir = build_env(tmp_path, mandate=mandate)
    out = orch.run()
    st = load_state(case_dir)
    assert st["status"] == "BLOCKED"
    assert st["reason"]["code"] == "needs_mandate_change"
    assert st["budget"]["used"]["rounds"] == 0
    assert not dispatch.calls
    assert (case_dir / "evolution-input.json").is_file()


def test_settle_clock_books_minutes_and_marks_the_case_started(tmp_path):
    from coverageflow import budget as budget_mod
    orch, dispatch, clk, case_dir = build_env(tmp_path)
    state = load_state(case_dir)
    t = [1000.0]
    b = budget_mod.BudgetTracker(state["budget"], clock_started=1000.0,
                                 clock=lambda: t[0])
    t[0] += 13 * 60
    orch._settle_clock(state, b)
    assert state["budget"]["used"]["elapsed_minutes"] == 13.0
    assert state["pending"]["case_clock_started"] is None
    assert state["pending"]["clock_paused"] is True
