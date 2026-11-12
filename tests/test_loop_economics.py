"""Repair-loop economics: stop early on defects no role can fix, patch-only
writer retries, schema-bound extraction units, and a booked case clock."""

import json

import casekit
from coverageflow import statestore
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


def test_mechanical_rounds_are_counted_apart():
    from coverageflow import budget as budget_mod
    b = budget_mod.BudgetTracker({"mechanical_rounds_max": 2})
    assert b.try_mechanical_round() and b.try_mechanical_round()
    assert not b.try_mechanical_round()
    assert b.used_rounds() == 0
    assert b.used_state()["mechanical_rounds"] == 2


def test_writer_only_gate_failure_does_not_spend_a_review_round(tmp_path):
    from tests.test_orchestrator import TestEvidenceLinkFailureIsASubmissionRepair as T
    from coverageflow._dispatch_test_stub import DEFAULT_REPORT_MD
    from tests.test_orchestrator import _writer_output
    bad = _writer_output(DEFAULT_REPORT_MD + "\nRevenue was 12.7 billion.\n")
    orch, dispatch, clk, case_dir = build_env(
        tmp_path, scripts={"writer": [bad] * 60})
    orch.run()
    st = load_state(case_dir)
    used = st["budget"]["used"]
    # every attempt fails the evidence gate; the rebuilds only re-run the
    # writer, so they spend the mechanical allowance, not review rounds
    assert used["mechanical_rounds"] == st["budget"]["mechanical_rounds_max"]
    builds = sum(1 for e in statestore.read_trace(case_dir)
                 if (e.get("detail") or {}).get("round_start"))
    assert used["rounds"] == st["budget"]["rounds_max"]
    assert builds == used["rounds"] + used["mechanical_rounds"]


def test_evidence_retry_payload_is_slim(tmp_path):
    from tests.test_orchestrator import TestEvidenceLinkFailureIsASubmissionRepair as T
    orch, dispatch, clk, case_dir = build_env(tmp_path, scripts={"writer": [T.BAD]})
    orch.run()
    first, second = dispatch.calls_of("writer")[:2]
    assert "prior_artifacts" in first.payload
    assert "prior_artifacts" not in second.payload
    assert second.payload["previous_draft"] and second.payload["evidence_keys"]
    assert "PATCH ONLY" in second.payload["submission_feedback"]


def _values():
    idx = {k: "valuation.json" for k in (
        "assumptions.terminal_growth", "assumptions.exit_multiple_ebitda",
        "dcf.wacc", "dcf.terminal.gordon.terminal_value", "dcf.upside")}
    vals = {"assumptions.terminal_growth": [0.025],
            "assumptions.exit_multiple_ebitda": [21.5], "dcf.wacc": [0.10],
            "dcf.terminal.gordon.terminal_value": [162809.4],
            "dcf.upside": [-0.84]}
    return idx, vals


def test_value_check_flags_a_number_cited_to_the_wrong_key():
    idx, vals = _values()
    note = ("## Valuation\nThe Gordon case uses a 10.0% WACC and 2.5% terminal "
            "growth with a terminal value of USD 162,809mm [EV:dcf.wacc] "
            "[EV:dcf.terminal.gordon.terminal_value].\n")
    ok, f = gates.gate_report_evidence_links(note, idx, vals)
    assert not ok and len(f) == 1
    assert "2.5" in f[0]["summary"] and "assumptions.terminal_growth" in f[0]["summary"]
    # citing the key that holds it clears the finding
    fixed = note.replace("[EV:dcf.wacc]", "[EV:dcf.wacc] [EV:assumptions.terminal_growth]")
    assert gates.gate_report_evidence_links(fixed, idx, vals)[0]


def test_value_check_leaves_derived_figures_and_unverifiable_cites_alone():
    idx, vals = _values()
    # 13.7 is held by no key: derived, a critic's job
    derived = "## V\nThe spread is 13.7 points [EV:dcf.upside].\n"
    assert gates.gate_report_evidence_links(derived, idx, vals)[0]
    # a cited key with no number cannot be compared
    idx["dcf.empty"] = "valuation.json"; vals["dcf.empty"] = []
    assert gates.gate_report_evidence_links(
        "## V\nExit multiple 21.5 [EV:dcf.empty].\n", idx, vals)[0]
    # no values supplied: check is off
    assert gates.gate_report_evidence_links(
        "## V\nterminal growth 2.5 [EV:dcf.wacc].\n", idx)[0]


def test_autolink_binds_an_unambiguous_number_and_leaves_the_rest():
    from coverageflow.orchestrator import autolink_numbers
    idx, vals = _values()
    note = "## V\nThe exit multiple is 21.5x. The spread is 13.7 points [EV:dcf.upside].\n"
    out, n = autolink_numbers(note, idx, vals)
    # 21.5x is held only by the exit-multiple key; 13.7 was already linked
    assert n == 1 and "21.5x [EV:assumptions.exit_multiple_ebitda]" in out
    # nothing the sentence is about holds this number: left for the writer
    out2, n2 = autolink_numbers("## V\nRevenue was 162,809.4.\n", idx, vals)
    assert n2 == 0 and out2.endswith("162,809.4.\n")


def test_writer_schema_matches_the_envelope_keys():
    from coverageflow.dispatch import WRITER_JSON_SCHEMA, _SCHEMA_REQUIRED_KEYS
    assert tuple(WRITER_JSON_SCHEMA["required"]) == _SCHEMA_REQUIRED_KEYS["report.md"]
    assert WRITER_JSON_SCHEMA["additionalProperties"] is False


def test_ingest_is_built_in_code_when_the_slicer_finds_the_statements(tmp_path):
    import shutil
    from pathlib import Path
    orch, dispatch, clk, case_dir = build_env(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "mini-10q.htm"
    shutil.copyfile(fixture, case_dir / "inputs" / "intc-10q-2026q3.htm")
    for path in (case_dir / "inputs" / "slices").rglob("*.txt"):
        path.unlink()
    orch.run()
    assert dispatch.calls_of("ingest") == []
    events = [e["event"] for e in statestore.read_trace(case_dir)]
    assert "INGEST_BUILT" in events
    report = json.loads(next(case_dir.glob("rounds/round-001/generator/ingest-report.json")).read_text())
    assert report["slices"] and report["classification"]["form"] == "10-Q"
    assert load_state(case_dir)["budget"]["used"]["tokens_by_role"]["ingest"] == 0
