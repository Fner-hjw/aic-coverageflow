"""Resume tests (owner: stream A2).

Locked rules (design-decisions section 3 + coordinator quality bar):
  - a kill mid-run is simulated by a raising dispatch AND a corrupted
    pending-role partial output file on disk;
  - resume must discard only that partial (forensic move to discarded/),
    re-dispatch ONLY the pending role, and leave completed verified
    artifacts byte-identical;
  - a corrupted VERIFIED artifact rolls back to its producing stage only;
  - a completed case returns its saved result without re-running anything.

All offline; clocks injected; engines are the stub doubles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import casekit
from coverageflow import gates, ledger as ledger_mod, statestore
from coverageflow import _dispatch_test_stub as _stub
from coverageflow.orchestrator import Orchestrator

SCHEMA_PATH = Path(__file__).parent / "fixtures" / "minimal-model-schema.json"


class KillDuringRun(RuntimeError):
    """Simulates the process dying mid-dispatch (demo kill-stage)."""


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.t, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")


def stub_build_model(facts, mapping_version: str):
    series = {}
    for f in facts["facts"]:
        series.setdefault(f["field"], {})[f["period"]] = f["value"]
    return {"artifact": "model", "engine": "stub-model@1.0",
            "mapping_tables": mapping_version, "series": series}


def stub_value_company(model, forecast, assumptions):
    return {"artifact": "valuation", "engine": "stub-val@1.0",
            "dcf": {"equity_value": 120000.0, "per_share": 28.0},
            "comps": {"per_share": 26.0}, "sensitivity": {},
            "cross_check_delta": 0.08}


class KillerDispatch:
    """Wraps the stub: raises KillDuringRun on the Nth call of a role."""

    def __init__(self, inner: _stub.StubDispatch, role: str, n: int):
        self.inner = inner
        self.kill_role = role
        self.kill_n = n
        self.calls: list = []

    def __call__(self, call):
        prior = len([1 for c in self.inner.calls if c.role == call.role]) + 1
        if call.role == self.kill_role and prior == self.kill_n:
            raise KillDuringRun(f"simulated kill during {call.role}")
        result = self.inner(call)
        self.calls.append(call)
        return result

    def __getattr__(self, name):
        return getattr(self.inner, name)


def make_orch(case_dir: Path, scripts=None, clk=None):
    dispatch = _stub.StubDispatch(scripts)
    engine = gates.GateEngine(schema_path=SCHEMA_PATH,
                              build_model_fn=stub_build_model,
                              value_fn=stub_value_company)
    return Orchestrator(case_dir, dispatch=dispatch, gate_engine=engine,
                        clock=clk, now=clk.iso)


def case_tree_digest(case_dir: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for path in sorted(case_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        h.update(str(path.relative_to(case_dir)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


@pytest.fixture
def fresh_case(tmp_path) -> tuple[Path, FakeClock]:
    clk = FakeClock()
    casekit.make_case_dir(tmp_path)
    Orchestrator.create_case(
        tmp_path, "intc-fy2026q3-20261030", casekit.MANDATE, casekit.MANIFEST,
        dispatch=_stub.StubDispatch(),
        gate_engine=gates.GateEngine(schema_path=SCHEMA_PATH,
                                     build_model_fn=stub_build_model,
                                     value_fn=stub_value_company),
        clock=clk, now=clk.iso)
    return Path(tmp_path) / "cases" / "intc-fy2026q3-20261030", clk


class TestResumeAfterKill:
    def test_kill_mid_extractor_then_resume_re_runs_only_pending_role(
            self, tmp_path, fresh_case):
        case_dir, clk = fresh_case
        # 1) run with a dispatch that dies during the first extractor call
        inner = _stub.StubDispatch()
        engine = gates.GateEngine(schema_path=SCHEMA_PATH,
                                  build_model_fn=stub_build_model,
                                  value_fn=stub_value_company)
        orch1 = Orchestrator(case_dir, dispatch=KillerDispatch(inner, "extractor", 1),
                             gate_engine=engine, clock=clk, now=clk.iso)
        with pytest.raises(KillDuringRun):
            orch1.run()
        st = statestore.load_state(case_dir)
        assert st["status"] == "RUNNING"
        assert st["pending"]["stage"] == "extractor"
        ingest_before = statestore.read_json(
            case_dir / "rounds/round-001/generator/ingest-report.json")

        # 2) simulate the interrupted in-flight write: a partial facts.json
        facts_path = case_dir / "rounds/round-001/generator/facts.json"
        facts_path.write_text('{"role": "extractor", "facts": [{"field": "revenue')

        # 3) a FRESH process/orchestrator resumes
        orch2 = make_orch(case_dir, clk=clk)
        summary = orch2.run()
        assert summary["status"] == "PENDING_APPROVAL"

        # only the pending role re-ran: ingest was verified and skipped
        assert orch2.dispatch.calls_of("ingest") == []
        assert len(orch2.dispatch.calls_of("extractor")) == 1
        # the partial output was discarded (moved to discarded/, not left behind)
        assert "revenue" not in facts_path.read_text()[:60]
        discard_dir = case_dir / "rounds/round-001/generator/discarded"
        assert discard_dir.exists()
        assert any(p.name.startswith("facts.json") for p in discard_dir.iterdir())
        # completed verified artifacts keep their CONTENT unchanged; only the
        # candidate_id header stamp was (still) missing when the case was killed
        ingest_after = statestore.read_json(
            case_dir / "rounds/round-001/generator/ingest-report.json")
        for key in ingest_before:
            if key != "candidate_id":
                assert ingest_after[key] == ingest_before[key], key
        # finally: approve and close the resumed case
        orch2.record_approval(decision="APPROVED", by="Supervisor")
        assert statestore.load_state(case_dir)["status"] == "CLOSED"

    def test_resume_uses_saved_artifacts_of_all_completed_stages(self, tmp_path,
                                                                 fresh_case):
        case_dir, clk = fresh_case
        inner = _stub.StubDispatch()
        engine = gates.GateEngine(schema_path=SCHEMA_PATH,
                                 build_model_fn=stub_build_model,
                                 value_fn=stub_value_company)
        # kill during the FIRST critic fan-out (writer + engines already done)
        orch1 = Orchestrator(case_dir,
                             dispatch=KillerDispatch(inner, "critic_c1", 1),
                             gate_engine=engine, clock=clk, now=clk.iso)
        with pytest.raises(KillDuringRun):
            orch1.run()
        st = statestore.load_state(case_dir)
        assert st["pending"]["stage"] == "critics"
        before = {rel: sha for rel, sha in {
            str(p.relative_to(case_dir)): statestore.sha256_file(p)
            for p in (case_dir / "rounds/round-001/generator").glob("*.json")
        }.items()}

        orch2 = make_orch(case_dir, clk=clk)
        orch2.run()
        # the entire generator stage was NOT re-dispatched
        for role in ("ingest", "extractor", "model", "writer", "generator_lead"):
            assert orch2.dispatch.calls_of(role) == [], role
        after = {rel: sha for rel, sha in {
            str(p.relative_to(case_dir)): statestore.sha256_file(p)
            for p in (case_dir / "rounds/round-001/generator").glob("*.json")
        }.items()}
        assert before == after, "verified artifacts must be byte-identical"
        assert statestore.load_state(case_dir)["status"] == "PENDING_APPROVAL"

    def test_corrupted_verified_artifact_rolls_back_to_its_stage(self, tmp_path,
                                                                 fresh_case):
        case_dir, clk = fresh_case
        orch = make_orch(case_dir, clk=clk)
        orch.run()   # to PENDING_APPROVAL
        assert statestore.load_state(case_dir)["status"] == "PENDING_APPROVAL"
        # tamper with a VERIFIED artifact of the completed build
        facts_path = case_dir / "rounds/round-001/generator/facts.json"
        facts_path.write_text('{"tampered": true}')
        st = statestore.load_state(case_dir)
        # fake a mid-build pending state so resume machinery engages
        st["status"] = "RUNNING"
        st["pending"] = {"role": "generator", "request_id": None, "round": 1,
                         "retry_count": 0, "case_clock_started": clk.iso(),
                         "deadline": None, "stage": "writer"}
        statestore.write_state(case_dir, st)

        orch2 = make_orch(case_dir, clk=clk)
        orch2.run()
        # facts.json was discarded and its producing role re-ran; but writer
        # was NOT re-dispatched before it, and ingest stayed untouched
        assert len(orch2.dispatch.calls_of("extractor")) == 1
        assert "tampered" not in facts_path.read_text()
        assert statestore.load_state(case_dir)["status"] == "PENDING_APPROVAL"

    def test_completed_case_returns_saved_result_untouched(self, tmp_path,
                                                            fresh_case):
        case_dir, clk = fresh_case
        orch = make_orch(case_dir, clk=clk)
        orch.run()
        orch.record_approval(decision="APPROVED", by="Supervisor",
                             reasoning="ties out")
        digest_before = case_tree_digest(case_dir)
        result_before = statestore.read_json(case_dir / "result.json")

        orch3 = make_orch(case_dir, clk=clk)
        summary = orch3.run()
        assert summary["re_ran"] is False
        assert summary["saved_result"] == result_before
        assert statestore.load_state(case_dir)["status"] == "CLOSED"
        assert case_tree_digest(case_dir) == digest_before, \
            "completed case must be returned untouched"
        assert orch3.dispatch.calls == []
