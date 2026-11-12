"""Engine rejections are repair work, not dead ends.

A reconciliation / linkage / missing-field violation from the model engine
belongs to the extractor; a forecast proposal the valuation engine cannot use
belongs to the model role. Both open a blocking ledger finding and take the
gate-failure repair path. Anything else still blocks as engine_violation.
"""

from __future__ import annotations

import json

import pytest

from coverageflow import _dispatch_test_stub as _stub
from coverageflow import gates, model_engine, orchestrator as orchmod
from coverageflow import statestore, valuation_engine
from coverageflow.orchestrator import Orchestrator, classify_engine_error
import casekit
from tests.test_orchestrator import (SCHEMA_PATH, build_env, load_state,
                                     stub_build_model, stub_value_company)
from tests.test_owners import _events, _packet


def _broken_gross_profit(payload: dict) -> dict:
    """Round 1 mis-reads gross profit; later rounds answer correctly."""
    out = _stub._default_facts(payload)
    if payload.get("round") == 1:
        out["rows"] = [
            "income_statement.gross_profit|2026-Q3|4000.0|USDmm|line 99"
            if r.startswith("income_statement.gross_profit|2026-Q3|") else r
            for r in out["rows"]]
    return out


def _ledger(case_dir) -> list[dict]:
    return json.loads((case_dir / "findings.json").read_text())["findings"]


class TestExtractorOwned:
    def _run(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"extractor": _broken_gross_profit})
        orch.run()
        return dispatch, case_dir

    def test_the_case_reaches_review_instead_of_blocking(self, tmp_path):
        dispatch, case_dir = self._run(tmp_path)
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        assert st["budget"]["used"]["rounds"] == 2      # the repair is a round

    def test_finding_is_opened_for_the_extractor_then_closed(self, tmp_path):
        dispatch, case_dir = self._run(tmp_path)
        rows = [r for r in _ledger(case_dir)
                if r["source_critic"] == "engine:model_engine"]
        assert len(rows) == 1
        row = rows[0]
        assert row["owner"] == "extractor" and row["severity"] == "major"
        assert row["affected_artifacts"] == ["facts.json"]
        assert "Gross profit reconciliation violation" in row["summary"]
        assert row["dedup_key"].startswith("engine:model_engine|engine:gross_profit")
        assert row["status"] == "closed"
        assert row["closed_by"]["critic"] == "engine:model_engine"
        assert row["closed_by"]["verified_evidence"]["artifact"].endswith(
            "round-002/generator/model.json")
        assert _events(case_dir, "FINDING_VERIFIED_CLOSED")

    def test_repair_packet_restarts_extraction_and_carries_the_violation(
            self, tmp_path):
        dispatch, case_dir = self._run(tmp_path)
        packet = _packet(case_dir)
        assert packet["rebuild_from"] == "extractor"
        assert any("Gross profit reconciliation" in f["summary"]
                   for f in packet["open_findings"])

    def test_every_extractor_unit_payload_carries_the_finding(self, tmp_path):
        dispatch, case_dir = self._run(tmp_path)
        calls = dispatch.calls_of("extractor")
        first = [c for c in calls if c.payload["round"] == 1]
        again = [c for c in calls if c.payload["round"] > 1]
        assert first and again
        assert all(not c.payload["carried_findings"] for c in first)
        for c in again:
            carried = c.payload["carried_findings"]
            assert [f["source_critic"] for f in carried] == ["engine:model_engine"]
            assert "reported gross profit is 4000" in carried[0]["summary"]
            assert "facts.json" in carried[0]["affected_artifacts"]

    def test_a_violation_that_never_clears_is_bounded_by_rounds(self, tmp_path):
        always = lambda payload: dict(
            _broken_gross_profit(dict(payload, round=1)))
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"extractor": always})
        orch.run()
        st = load_state(case_dir)
        assert st["status"] != "RUNNING"
        assert st.get("reason", {}).get("code") != "engine_violation"
        assert st["status"] == "EXHAUSTED"
        assert 1 < st["budget"]["used"]["rounds"] <= orch._budget(st).limits.rounds_max
        open_rows = [r for r in _ledger(case_dir) if r["status"] == "open"]
        assert len(open_rows) <= 1      # deduplicated across rounds


class TestModelOwned:
    def test_a_proposal_omitting_a_segment_goes_to_the_model_role(self, tmp_path):
        bad = _stub._default_forecast({})
        bad["segments"] = bad["segments"][:1]       # CCG only
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"model": [bad]})
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        row = [r for r in _ledger(case_dir)
               if r["source_critic"] == "engine:valuation_engine"][0]
        assert row["owner"] == "model" and row["status"] == "closed"
        assert row["affected_artifacts"] == ["forecast_assumptions.json"]
        assert "proposal omits segment" in row["summary"]
        assert _packet(case_dir)["rebuild_from"] == "model"
        assert len(dispatch.calls_of("model")) == 2
        assert len(dispatch.calls_of("extractor")) == 1     # facts carried
        second = dispatch.calls_of("model")[1].payload
        assert "proposal omits segment" in second["carried_findings"][0]["summary"]


class TestUnclassifiable:
    def test_a_code_bug_still_blocks_as_engine_violation(self, tmp_path):
        def broken(facts, mapping_version):
            raise TypeError("'NoneType' object is not subscriptable")
        casekit.make_case_dir(tmp_path)
        engine = gates.GateEngine(schema_path=SCHEMA_PATH,
                                  build_model_fn=broken,
                                  value_fn=stub_value_company)
        from tests.test_orchestrator import FakeClock
        clk = FakeClock()
        orch = Orchestrator.create_case(
            tmp_path, "intc-fy2026q3-20261030", casekit.MANDATE,
            casekit.MANIFEST, dispatch=_stub.StubDispatch(),
            gate_engine=engine, clock=clk, now=clk.iso)
        orch.run()
        case_dir = tmp_path / "cases" / "intc-fy2026q3-20261030"
        st = load_state(case_dir)
        assert st["status"] == "BLOCKED"
        assert st["reason"]["code"] == "engine_violation"
        assert not (case_dir / "findings.json").exists() or not [
            r for r in _ledger(case_dir) if r["source_critic"].startswith("engine:")]


MV, MM = model_engine.BoundsViolation, model_engine.MissingFactError
VB, VM = valuation_engine.BoundsViolation, valuation_engine.MissingFactError


class TestClassification:
    @pytest.mark.parametrize("engine,exc,owner,kind,period", [
        ("model_engine", MV("Gross profit reconciliation violation in FY2023: "
                            "revenue 22680 - cost of revenue 11278 = 11402 but "
                            "reported gross profit is 10460"),
         "extractor", "gross_profit_reconciliation", "FY2023"),
        ("model_engine", MV("EPS linkage violation in 2026-Q3: x"),
         "extractor", "eps_linkage", "2026-Q3"),
        ("model_engine", MV("Segment revenue reconciliation violation in FY2025: x"),
         "extractor", "segment_reconciliation", "FY2025"),
        ("model_engine", MM("missing required fact field: income_statement.revenue"),
         "extractor", "missing_required_fact", ""),
        ("valuation_engine", VM("no fiscal year and no complete year-to-date pair: x"),
         "extractor", "valuation_missing_fact", ""),
        ("valuation_engine", VB("proposal omits segment foundry (every segment "
                                "in segment_slugs needs a growth path)"),
         "model", "forecast_proposal", ""),
        ("valuation_engine", VB("segment ccg growth 0.9 outside mandate bounds [0, 0.2]"),
         "model", "forecast_proposal", ""),
        ("valuation_engine", VB("terminal growth 0.05 must be below WACC 0.04"),
         None, "", ""),
        ("valuation_engine", VM("mandate assumption tax_rate is missing; x"),
         None, "", ""),
        ("model_engine", TypeError("boom"), None, "", ""),
        ("valuation_engine", KeyError("boom"), None, "", ""),
    ])
    def test_table(self, engine, exc, owner, kind, period):
        assert classify_engine_error(engine, exc) == (owner, kind, period)
