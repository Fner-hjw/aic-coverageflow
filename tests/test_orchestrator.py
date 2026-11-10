"""Tests for the CoverageFlow controller/orchestrator (owner: stream A2).

This file covers: statestore foundations, the findings ledger, and — after
implementation — the full orchestrator state machine rules from
docs/design-decisions.md §3. Every LLM role is satisfied by the scripted
offline stub from coverageflow._dispatch_test_stub; no network, no wall clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coverageflow import ledger, statestore
import casekit


# ===========================================================================
# statestore foundations
# ===========================================================================

class TestStateStore:
    def test_atomic_write_json_roundtrip_and_hash(self, tmp_path):
        obj = {"a": 1, "b": [1, 2, 3]}
        sha = statestore.atomic_write_json(tmp_path / "f.json", obj)
        assert sha == statestore.sha256_file(tmp_path / "f.json")
        assert statestore.read_json(tmp_path / "f.json") == obj

    def test_atomic_write_leaves_no_temp_on_crash(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(statestore.os, "replace", boom)
        with pytest.raises(OSError):
            statestore.atomic_write_text(tmp_path / "g.json", "x")
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
        assert leftovers == []
        assert not (tmp_path / "g.json").exists()

    def test_sha256_json_deterministic_regardless_of_key_order(self):
        a = statestore.sha256_json({"x": 1, "y": 2})
        b = statestore.sha256_json({"y": 2, "x": 1})
        assert a == b

    def test_trace_append_and_read(self, tmp_path):
        statestore.append_trace(tmp_path, {"event": "CASE_CREATED", "case_id": "c1"})
        statestore.append_trace(tmp_path, {"event": "STATE_TRANSITION", "case_id": "c1"})
        evs = statestore.read_trace(tmp_path)
        assert [e["event"] for e in evs] == ["CASE_CREATED", "STATE_TRANSITION"]
        assert all(e["ts"] for e in evs)
        with pytest.raises(ValueError):
            statestore.append_trace(tmp_path, {"event": "NOT_AN_EVENT"})

    def test_trace_tolerates_torn_trailing_line(self, tmp_path):
        p = tmp_path / statestore.TRACE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        # simulate a kill mid-append: valid line + torn fragment
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"event":"CASE_CREATED"}\n{"event":"STATE_TR')
        evs = statestore.read_trace(tmp_path)
        assert len(evs) == 1

    def test_write_state_appends_checkpoint_and_history(self, tmp_path):
        state = {"case_id": "c1", "status": "RUNNING", "checkpoints": []}
        statestore.write_state(
            tmp_path, state, transition=("RUNNING", "RUNNING"),
            verified="rounds/round-001/generator/ingest-report.json",
            verified_artifacts={"rounds/round-001/generator/ingest-report.json": "ab" * 32},
        )
        st = statestore.load_state(tmp_path)
        assert len(st["checkpoints"]) == 1
        ck = st["checkpoints"][0]
        assert ck["from"] == "RUNNING" and ck["to"] == "RUNNING"
        assert ck["verified"].endswith("ingest-report.json")
        assert ck["state"].startswith("sha256:")
        hist = statestore.read_json(tmp_path / "history" / "checkpoint-0001.json")
        assert hist["verified_artifacts"]["rounds/round-001/generator/ingest-report.json"] == "ab" * 32

    def test_stateless_write_has_no_checkpoint(self, tmp_path):
        state = {"case_id": "c1", "status": "CREATED", "checkpoints": []}
        statestore.write_state(tmp_path, state)
        st = statestore.load_state(tmp_path)
        assert st["checkpoints"] == []

    def test_request_binding_roundtrip(self, tmp_path):
        req = {"request_id": "req-0001", "role": "ingest", "payload_digest": "x"}
        statestore.write_request(tmp_path, req)
        assert statestore.read_request(tmp_path)["role"] == "ingest"


# ===========================================================================
# findings ledger (contracts state-and-artifacts.md section 8)
# ===========================================================================

class TestFindingsLedger:
    def _open_two(self, tmp_path):
        led = ledger.FindingsLedger(tmp_path)
        f1 = led.open_finding({
            "severity": "major", "summary": "revenue uncited",
            "affected_artifacts": ["facts.json"],
            "required_evidence": "citation for income_statement.revenue",
            "source_critic": "critic_c1", "dedup_key": "uncited-rev",
        }, round=1)
        f2 = led.open_finding({
            "severity": "minor", "summary": "margin gap",
            "affected_artifacts": ["report.md"],
            "required_evidence": "recomputed gross margin row",
            "source_critic": "critic_c2", "dedup_key": "margin-gap",
        }, round=1)
        return led, f1, f2

    def test_open_appends_with_sequential_ids(self, tmp_path):
        led, f1, f2 = self._open_two(tmp_path)
        assert f1 == "fnd-001" and f2 == "fnd-002"
        assert len(led.open_findings()) == 2

    def test_persistence_across_instances(self, tmp_path):
        self._open_two(tmp_path)
        led2 = ledger.FindingsLedger(tmp_path)
        assert len(led2.all_findings()) == 2
        assert led2.by_id("fnd-001")["status"] == "open"

    def test_dedup_key_updates_last_seen_round(self, tmp_path):
        led = ledger.FindingsLedger(tmp_path)
        a = led.open_finding({"summary": "x", "severity": "major", "dedup_key": "k"}, round=1)
        b = led.open_finding({"summary": "x", "severity": "major", "dedup_key": "k"}, round=2)
        assert a == b
        assert led.by_id(a)["last_seen_round"] == 2
        assert len(led.all_findings()) == 1

    def test_closed_finding_reopens_as_new_entry(self, tmp_path):
        led = ledger.FindingsLedger(tmp_path)
        a = led.open_finding({"summary": "x", "severity": "major", "dedup_key": "k"}, round=1)
        led.close_finding(a, critic="c2", round=2, verified_evidence={"artifact": "facts.json", "sha256": "ff"})
        b = led.open_finding({"summary": "x", "severity": "major", "dedup_key": "k"}, round=3)
        assert b != a  # append-only: recurrence is a new ledger row
        assert len(led.open_findings()) == 1

    def test_close_requires_verified_artifact_and_sha(self, tmp_path):
        led = ledger.FindingsLedger(tmp_path)
        a = led.open_finding({"summary": "x", "severity": "major", "dedup_key": "k"}, round=1)
        with pytest.raises(ValueError):
            led.close_finding(a, critic="c2", round=2, verified_evidence={})
        with pytest.raises(ValueError):
            led.close_finding(a, critic="c2", round=2, verified_evidence={"artifact": "facts.json"})
        with pytest.raises(ValueError):
            led.close_finding(a, critic="c2", round=2,
                              verified_evidence={"artifact": "facts.json", "sha256": ""})
        led.close_finding(a, critic="c2", round=2,
                          verified_evidence={"artifact": "facts.json", "sha256": "ff" * 32})
        assert led.open_findings() == []
        row = led.by_id(a)
        assert row["status"] == "closed"
        assert row["closed_by"]["critic"] == "c2"
        assert row["closed_by"]["verified_evidence"]["artifact"] == "facts.json"
        with pytest.raises(LookupError):
            led.close_finding("fnd-999", critic="c2", round=2,
                              verified_evidence={"artifact": "x", "sha256": "y"})

    def test_carried_payload_for_repair_packets(self, tmp_path):
        led, f1, f2 = self._open_two(tmp_path)
        carried = led.carried()
        assert {c["finding_id"] for c in carried} == {"fnd-001", "fnd-002"}
        assert all(c["required_evidence"] for c in carried)
        assert {c["status"] for c in carried} == {"open"}


# ===========================================================================
# orchestrator state machine — rule tests (written before implementation)
# ===========================================================================

import json
from datetime import datetime, timezone

import casekit
from coverageflow import gates, orchestrator as orchmod
from coverageflow import _dispatch_test_stub as _stub
from coverageflow.orchestrator import Orchestrator

SCHEMA_PATH = Path(__file__).parent / "fixtures" / "minimal-model-schema.json"


class FakeClock:
    """Injected epoch clock (ISO timestamps derived from it). No wall time."""

    def __init__(self, t: float = 1_000_000.0, step: float = 0.0):
        self.t = t
        self.step = step

    def __call__(self) -> float:
        t = self.t
        self.t += self.step
        return t

    def iso(self) -> str:
        return datetime.fromtimestamp(self.t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _writer_output(md: str):
    return {"role": "writer", "report_md": md, "conformance": "schema 1.0",
            "prompt_version": "1.0"}


def _critic_findings(critic: str = "critic_c1", summary: str = "revenue citation incomplete"):
    return {
        "role": critic, "round": None, "verdict": "findings",
        "findings": [{
            "finding_id": None,
            "severity": "major", "summary": summary,
            "evidence": "facts ledger shows no citation for field",
            "affected_artifacts": ["facts.json"],
            "required_evidence": "citation for the flagged fact",
        }],
        "coverage": {"rubric_version": "1.0", "areas_checked": ["citation_integrity"]},
        "conformance": "schema 1.0", "prompt_version": "1.0",
    }


def stub_build_model(facts, mapping_version: str):
    # delegate post-merge to the real (offline, deterministic) engines so
    # builds and recomputes agree; engine math itself is TDD'd by stream A1
    from coverageflow._dispatch_test_stub import stub_build_model as _real
    return _real(facts, mapping_version)


def stub_value_company(model, forecast, assumptions):
    from coverageflow._dispatch_test_stub import stub_value_company as _real
    return _real(model, forecast, assumptions)


def build_env(tmp_path: Path, scripts=None, clk: FakeClock | None = None,
              mandate=None, manifest=None):
    """Create a case + orchestrator with offline stub dispatch and engines."""
    clk = clk or FakeClock()
    casekit.make_case_dir(tmp_path, mandate=mandate, manifest=manifest)
    dispatch = _stub.StubDispatch(scripts)
    engine = gates.GateEngine(schema_path=SCHEMA_PATH,
                              build_model_fn=stub_build_model,
                              value_fn=stub_value_company)
    orch = Orchestrator.create_case(
        tmp_path, "intc-fy2026q3-20261030",
        mandate if mandate is not None else casekit.MANDATE,
        manifest if manifest is not None else casekit.MANIFEST,
        dispatch=dispatch, gate_engine=engine, clock=clk, now=clk.iso)
    case_dir = Path(tmp_path) / "cases" / "intc-fy2026q3-20261030"
    return orch, dispatch, clk, case_dir


def load_state(case_dir) -> dict:
    return statestore.load_state(case_dir)


class TestHappyPath:
    def test_run_reaches_pending_approval_after_two_clean_reviews(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        summary = orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        assert summary["status"] == "PENDING_APPROVAL"
        # two consecutive clean full reviews of the SAME candidate
        assert st["candidate"]["clean_review_rounds"] == 2
        # reviewers are fresh every round: every critic dispatched twice
        for i in range(1, 5):
            assert len(dispatch.calls_of(f"critic_c{i}")) == 2
        # one build only: the second review round did not rebuild
        assert st["budget"]["used"]["rounds"] == 1
        # deadline honoured
        assert st["pending"]["deadline"] is not None
        # approval payload recorded the exact candidate
        assert st["candidate"]["candidate_id"] == summary["candidate_id"]
        # gate report written for the built round
        gates_report = json.loads((case_dir / "rounds" / "round-001" /
                                   "numerical" / "gates.json").read_text())
        assert gates_report["passed"]

    def test_approval_records_and_closes_case(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        out = orch.record_approval(decision="APPROVED", by="Supervisor",
                                   reasoning="numbers tie out")
        st = load_state(case_dir)
        assert st["status"] == "CLOSED"
        assert out["terminal"] == "APPROVED"
        result = statestore.read_json(case_dir / "result.json")
        assert result["terminal"] == "APPROVED"
        assert "human approved" in result["reason"]["message"].lower()
        assert result["reason"]["code"] == "APPROVED"
        apr_files = list((case_dir / "approvals").glob("apr-*.json"))
        assert len(apr_files) == 1
        apr = statestore.read_json(apr_files[0])
        assert apr["decision"] == "APPROVED" and apr["by"] == "Supervisor"
        # versioned report written
        reports = list((case_dir / "report").glob("v*.md"))
        assert len(reports) == 1
        # trace contains the terminal pair of events
        events = [e["event"] for e in statestore.read_trace(case_dir)]
        assert events.count("APPROVAL_RECORDED") == 1
        assert events.count("CASE_CLOSED") == 1

    def test_partial_review_streak_is_not_enough_for_approval(self, tmp_path):
        # kill after the first clean review: status must NOT be PENDING_APPROVAL
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        # block the second review round by raising mid-flight (simulated kill)
        state = None

        class _Boom:
            def __init__(self, dispatch):
                self._d = dispatch
            def __call__(self, call):
                if call.role == "critic_c1" and len(self._d.calls_of(call.role)) == 1:
                    raise RuntimeError("simulated kill mid-review")
                return self._d(call)
            def __getattr__(self, name):
                return getattr(self._d, name)

        wrapped = _Boom(dispatch)
        orch2 = Orchestrator(case_dir, dispatch=wrapped,
                             gate_engine=gates.GateEngine(
                                 schema_path=SCHEMA_PATH,
                                 build_model_fn=stub_build_model,
                                 value_fn=stub_value_company),
                             clock=clk, now=clk.iso)
        try:
            orch2.run()
        except RuntimeError:
            pass
        st = load_state(case_dir)
        assert st["status"] in ("RUNNING", "BLOCKED", "EXHAUSTED")
        assert st["status"] != "PENDING_APPROVAL"
        assert st["candidate"]["clean_review_rounds"] == 1


class TestInvalidSubmissions:
    def test_invalid_submission_repairs_at_role_without_consuming_round(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"extractor": [{"__invalid": True}]})
        out = orch.run()
        orch.record_approval(decision="APPROVED", by="Sup")
        st = load_state(case_dir)
        assert st["status"] == "CLOSED"
        # the round was NOT consumed by the repair:
        assert st["budget"]["used"]["rounds"] == 1
        # extractor dispatched twice with DIFFERENT request ids (new request bound)
        ids = [c.request_id for c in dispatch.calls_of("extractor")]
        assert len(ids) == 2 and ids[0] != ids[1]
        # retry counter reset after the valid submission
        assert st["pending"]["retry_count"] == 0

    def test_invalid_submissions_are_repaired_not_blocked(self, tmp_path):
        # retries_max=3: two invalid submissions are repaired at the same role
        # and the third attempt succeeds - the case is not blocked.
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"extractor": [{"__invalid": True}, {"__invalid": True}]})
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        assert len(dispatch.calls_of("extractor")) == 3
        assert st["budget"]["used"]["retries_by_role"]["extractor"] == 2

    def test_retries_exhausted_ends_exhausted_with_precise_reason(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"extractor": [{"__invalid": True}] * 10})
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED"
        assert st["reason"]["code"] == "retries"
        assert "extractor" in st["reason"]["message"]
        result = statestore.read_json(case_dir / "result.json")
        assert result["terminal"] == "EXHAUSTED"
        assert result["reason"]["code"] == "retries"
        # one initial dispatch + retries_max repairs
        assert len(dispatch.calls_of("extractor")) == 4

    def test_stale_request_id_rejected_as_invalid(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"extractor": [{"__stale": True}]})
        orch.run()
        orch.record_approval(decision="APPROVED", by="Sup")
        st = load_state(case_dir)
        assert st["status"] == "CLOSED"
        assert st["budget"]["used"]["rounds"] == 1
        assert len(dispatch.calls_of("extractor")) == 2


class TestCandidateChangeResetsStreak:
    def test_any_candidate_artifact_change_resets_clean_streak(self, tmp_path):
        # R1: clean review of candidate A (streak 1)
        # R2: review-only, critic_c1 raises a finding -> repair
        # R3: rebuild with a CHANGED writer output (candidate B); clean review -> streak 1 again
        # R4: review-only of B clean -> streak 2 -> PENDING_APPROVAL
        writer_alt = {"__stale": False, "role": "writer",
                      "report_md": _stub.ALT_REPORT_MD,
                      "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {
            "critic_c1": [None, _critic_findings()],   # clean, then findings
            "writer": [_writer_output(_stub.DEFAULT_REPORT_MD), writer_alt],
        }
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        # A candidate change MUST reset the streak: 2 fresh reviews of B were needed.
        assert st["candidate"]["clean_review_rounds"] == 2
        assert len(dispatch.calls_of("critic_c1")) == 4  # R1,R2,R3,R4 reviews
        assert st["budget"]["used"]["rounds"] == 2       # two builds (R1, R3)
        # candidate changed between R1 and R3 reviews
        cand_a = dispatch.calls_of("critic_c1")[0].candidate_id
        cand_b = dispatch.calls_of("critic_c1")[2].candidate_id
        assert cand_a and cand_b and cand_a != cand_b


class TestBudgetExhaustion:
    def test_rounds_exhaustion_ends_exhausted_with_precise_reason(self, tmp_path):
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED"
        assert st["reason"]["code"] == "rounds"
        assert st["reason"]["detail"]["rounds"] == 2
        assert st["reason"]["detail"]["max"] == 2
        result = statestore.read_json(case_dir / "result.json")
        assert result["terminal"] == "EXHAUSTED"
        assert result["reason"]["code"] == "rounds"
        assert result["budget_final"]["used"]["rounds"] == 2

    def test_role_token_cap_exhaustion(self, tmp_path):
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["role_tokens_max"] = 1000
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
        # dispatch burns 140+260 = 400 per call... bump tokens per call:
        dispatch.tokens_in = 700
        dispatch.tokens_out = 600  # 1300 per call > 1000 cap
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED"
        assert st["reason"]["code"] == "role_tokens"
        # the R2 rebuild breaches at the first re-dispatched role (ingest,
        # which already burned 1300 of the 1000-token cap in R1)
        assert st["reason"]["detail"]["role"] == "ingest"
        assert st["reason"]["detail"]["tokens"] == 1300
        result = statestore.read_json(case_dir / "result.json")
        assert result["terminal"] == "EXHAUSTED"

    def test_case_clock_exhaustion(self, tmp_path):
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["case_minutes"] = 5  # 300 s
        clk = FakeClock(step=120.0)            # every clock read advances 120 s
        orch, dispatch, clk_unused, case_dir = build_env(
            tmp_path, mandate=mandate, clk=clk)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED"
        assert st["reason"]["code"] == "case_clock"

    def test_exhaustion_never_grants_acceptance(self, tmp_path):
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
        orch.run()
        assert load_state(case_dir)["status"] == "EXHAUSTED"
        with pytest.raises(RuntimeError):
            orch.record_approval(decision="APPROVED", by="Sup")
        assert load_state(case_dir)["status"] == "EXHAUSTED"


class TestApprovalDeadline:
    def test_expiry_blocks_and_reapproval_restarts_acceptance_from_zero(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"
        clk.advance(49 * 3600)  # beyond the default 48h deadline
        summary = orch.run()
        st = load_state(case_dir)
        assert st["status"] == "BLOCKED"
        assert st["reason"]["code"] == "APPROVAL_EXPIRED"
        result = statestore.read_json(case_dir / "result.json")
        assert result["terminal"] == "BLOCKED"
        assert result["reason"]["code"] == "APPROVAL_EXPIRED"
        # re-approval restarts ACCEPTANCE FROM ZERO:
        orch.record_approval(decision="APPROVED", by="Supervisor")
        st = load_state(case_dir)
        assert st["status"] == "RUNNING"
        assert st["candidate"]["clean_review_rounds"] == 0
        assert st["pending"]["deadline"] is None
        # continue: acceptance must be earned again (2 fresh clean reviews)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        assert st["candidate"]["clean_review_rounds"] == 2

    def test_rejected_terminal_records_result(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        out = orch.record_approval(decision="REJECTED", by="Chevalier",
                                   reasoning="valuation stale")
        st = load_state(case_dir)
        assert st["status"] == "REJECTED"
        result = statestore.read_json(case_dir / "result.json")
        assert result["terminal"] == "REJECTED"


class TestCutoffGateEndToEnd:
    def test_post_cutoff_doc_rejected_by_gate_and_leads_to_repair(self, tmp_path):
        import copy
        manifest = copy.deepcopy(casekit.MANIFEST)
        manifest["documents"].append(dict(manifest["documents"][0],
                                          doc_id="intc-10q-2026q4",
                                          filed="2026-11-15"))
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(tmp_path, mandate=mandate,
                                                  manifest=manifest)
        orch.run()
        st = load_state(case_dir)
        # the cutoff gate failed deterministically each rebuild
        gates_report = json.loads((case_dir / "rounds" / "round-001" /
                                   "numerical" / "gates.json").read_text())
        assert not gates_report["passed"]
        failed = [g["gate"] for g in gates_report["results"] if not g["ok"]]
        assert "input_cutoff" in failed
        # the finding was carried into a repair packet
        packet = json.loads((case_dir / "rounds" / "round-001" /
                             "repair-packet.json").read_text())
        assert packet["open_findings"], "cutoff finding must be carried"
        assert any("post-cutoff" in f["summary"] or "cutoff" in f["summary"]
                   for f in packet["open_findings"])
        assert "unchanged_criteria" in packet and "delta_scope" in packet
        # and the ledger holds the cutoff finding open; case ended exhausted on rounds
        led = ledger.FindingsLedger(case_dir)
        assert any("intc-10q-2026q4" in f["summary"] for f in led.open_findings())
        assert st["status"] == "EXHAUSTED"
        assert st["reason"]["code"] == "rounds"


class TestRepairPacketsFromCritics:
    def test_findings_open_ledger_and_packet_then_verified_closure_on_repair(
            self, tmp_path):
        # critic_c1 flags a finding on review 1; the rebuild resolves it and the
        # fresh review verifies the closure with artifact+sha evidence
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": [_critic_findings()]})
        orch.run()
        orch.record_approval(decision="APPROVED", by="Sup")
        st = load_state(case_dir)
        assert st["status"] == "CLOSED"
        led = ledger.FindingsLedger(case_dir)
        assert led.all_findings(), "finding must exist"
        row = led.all_findings()[0]
        assert row["status"] == "closed", row
        assert row["closed_by"]["verified_evidence"]["artifact"].endswith("facts.json")
        # closure evidence must match the candidate artifact hashes
        assert row["closed_by"]["verified_evidence"]["sha256"] != ("0" * 64)
        # the verification sha must be a real file hash in the case
        art = case_dir / row["closed_by"]["verified_evidence"]["artifact"]
        assert statestore.sha256_file(art) == row["closed_by"]["verified_evidence"]["sha256"]
        events = [e["event"] for e in statestore.read_trace(case_dir)]
        assert "FINDING_OPENED" in events
        assert "FINDING_VERIFIED_CLOSED" in events
        assert "REPAIR_PACKET_ISSUED" in events

    def test_unverified_closure_does_not_close_finding(self, tmp_path):
        # A critic claims a resolution but with a WRONG evidence sha: the
        # closure must be rejected by the orchestrator, the finding stays open
        # and the case can never accept (ends EXHAUSTED on rounds).
        def tampering_critic(payload):
            base = _stub._default_critic("critic_c1")(payload)
            if len(payload.get("carried_findings", [])):
                base["verdict"] = "clean"
                base["findings"] = []
                base["verified_resolutions"] = [{
                    "finding_id": f["finding_id"],
                    "resolved": True,
                    "verified_evidence": {"artifact": "facts.json",
                                          "sha256": "f" * 64},  # does not match
                } for f in payload["carried_findings"]]
                return base
            return _critic_findings()  # first review: raises the finding

        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": tampering_critic}, mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED"
        led = ledger.FindingsLedger(case_dir)
        # the tampered evidence (sha "f"*64) must never appear in ANY close
        # record; only legitimately-verified evidence closed findings
        for row in led.all_findings():
            cb = row.get("closed_by") or {}
            ev = (cb.get("verified_evidence") or {})
            assert ev.get("sha256") != "f" * 64, "unverified close leaked through"
        # the tampering critic kept raising findings -> acceptance never granted
        assert led.open_findings(), "finding reopened by critic must stay open"
        assert st["candidate"]["clean_review_rounds"] < 2


# ===========================================================================
# CLI surface (contracts interfaces section 3; offline wiring)
# ===========================================================================

import shutil as _shutil
from coverageflow import cli


class TestCLI:
    def _workspace(self, tmp_path: Path) -> Path:
        # pin the (A1-owned) model schema into the workspace; the fixture
        # schema stands in until A1 drops references/model-schema.json
        refs = tmp_path / "references"
        refs.mkdir(exist_ok=True)
        _shutil.copyfile(SCHEMA_PATH, refs / "model-schema.json")
        return tmp_path

    def test_case_new_list_run_approve_roundtrip(self, tmp_path, capsys):
        ws = self._workspace(tmp_path)
        rc = cli.main(["--workspace", str(ws), "case", "new",
                       "--ticker", "INTC", "--trigger-doc", "intc-10q-2026q3",
                       "--cutoff", "2026-10-30"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        case_id = out["created"]
        assert case_id == "intc-fy2026q3-20261030"
        casekit.materialize_inputs(ws, case_id)

        rc = cli.main(["--workspace", str(ws), "case", "list"])
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows and rows[0]["case_id"] == case_id
        assert rows[0]["status"] == "CREATED"

        rc = cli.main(["--workspace", str(ws), "run"])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["status"] == "PENDING_APPROVAL"

        rc = cli.main(["--workspace", str(ws), "gates", "--case", case_id])
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["passed"] is True

        rc = cli.main(["--workspace", str(ws), "approve", "--case", case_id,
                       "--decision", "APPROVED", "--by", "Chiantia",
                       "--reason", "ties out"])
        assert rc == 0
        outcome = json.loads(capsys.readouterr().out)
        assert outcome["status"] == "CLOSED"

        rc = cli.main(["--workspace", str(ws), "case", "list"])
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["status"] == "CLOSED"

    def test_export_and_ui_are_placeholders(self, tmp_path, capsys):
        ws = self._workspace(tmp_path)
        rc = cli.main(["--workspace", str(ws), "export", "--case", "x"])
        assert rc == 2
        assert "owned by another stream" in capsys.readouterr().out
        rc = cli.main(["--workspace", str(ws), "ui"])
        assert rc == 2
        assert "owned by another stream" in capsys.readouterr().out

    def test_demo_seed_finding_and_perturb(self, tmp_path, capsys):
        ws = self._workspace(tmp_path)
        cli.main(["--workspace", str(ws), "case", "new",
                  "--ticker", "INTC", "--trigger-doc", "intc-10q-2026q3",
                  "--cutoff", "2026-10-30"])
        capsys.readouterr()
        casekit.materialize_inputs(ws, "intc-fy2026q3-20261030")
        rc = cli.main(["--workspace", str(ws), "demo", "seed-finding",
                       "--case", "intc-fy2026q3-20261030"])
        assert rc == 0
        seeded = json.loads(capsys.readouterr().out)
        assert seeded["seeded_finding"].startswith("fnd-")
        rc = cli.main(["--workspace", str(ws), "demo", "perturb",
                       "--case", "intc-fy2026q3-20261030"])
        assert rc == 0
        perturb = json.loads(capsys.readouterr().out)["perturbation"]
        assert perturb["deterministic_recompute"] is True
        assert perturb["no_post_cutoff_documents"] is True


class TestCaseCreationIsNotDestructive:
    def test_create_case_refuses_to_overwrite_an_existing_case(self, tmp_path):
        orch, _dispatch, _clk, case_dir = build_env(tmp_path)
        before = (case_dir / "state.json").read_bytes()
        with pytest.raises(FileExistsError, match="already exists"):
            Orchestrator.create_case(
                tmp_path, "intc-fy2026q3-20261030", casekit.MANDATE,
                casekit.MANIFEST, dispatch=_stub.StubDispatch(),
                gate_engine=gates.GateEngine(
                    schema_path=SCHEMA_PATH, build_model_fn=stub_build_model,
                    value_fn=stub_value_company))
        assert (case_dir / "state.json").read_bytes() == before

    def test_create_case_overwrite_is_explicit(self, tmp_path):
        _orch, _dispatch, _clk, case_dir = build_env(tmp_path)
        statestore.write_state(case_dir, {**statestore.load_state(case_dir),
                                          "status": "EXHAUSTED"})
        fresh = Orchestrator.create_case(
            tmp_path, "intc-fy2026q3-20261030", casekit.MANDATE,
            casekit.MANIFEST, dispatch=_stub.StubDispatch(),
            gate_engine=gates.GateEngine(
                schema_path=SCHEMA_PATH, build_model_fn=stub_build_model,
                value_fn=stub_value_company),
            overwrite=True)
        assert statestore.load_state(fresh.case_dir)["status"] == "CREATED"

    def test_cli_case_new_reports_the_conflict_cleanly(self, tmp_path, capsys):
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = ["--workspace", str(ws), "case", "new", "--ticker", "INTC",
                "--trigger-doc", "intc-10q-2026q3", "--cutoff", "2026-10-30"]
        assert cli.main(argv) == 0
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert "already exists" in str(exc.value)


class TestMissingInputsBlock:
    def _case_without_inputs(self, tmp_path):
        """A case whose manifest declares a filing that is not on disk."""
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        for path in (case_dir / "inputs").rglob("*"):
            if path.is_file() and path.name != "manifest.json":
                path.unlink()
        return orch, dispatch, case_dir

    def test_run_blocks_immediately_without_dispatching_any_role(self, tmp_path):
        orch, dispatch, case_dir = self._case_without_inputs(tmp_path)
        summary = orch.run()
        assert summary["status"] == "BLOCKED"
        assert summary["reason"]["code"] == "missing_inputs"
        assert "intc-10q-2026q3" in summary["reason"]["message"]
        assert dispatch.calls == [], "no role may be dispatched into missing inputs"
        # the block is recorded as a result.json a human can read
        result = statestore.read_json(case_dir / "result.json")
        assert result["terminal"] == "BLOCKED"
        assert result["reason"]["code"] == "missing_inputs"

    def test_blocked_case_reports_the_reason_on_re_run(self, tmp_path):
        orch, _dispatch, _case_dir = self._case_without_inputs(tmp_path)
        orch.run()
        again = orch.run()
        assert again["status"] == "BLOCKED"
        assert "still missing" in again["message"]

    def test_fetching_the_inputs_unblocks_the_case(self, tmp_path):
        orch, dispatch, case_dir = self._case_without_inputs(tmp_path)
        assert orch.run()["status"] == "BLOCKED"
        # the human fetches the filing, then re-runs
        casekit.materialize_inputs(tmp_path, case_dir.name)
        summary = orch.run()
        assert summary["status"] == "PENDING_APPROVAL", summary
        assert dispatch.calls, "the run resumed after the inputs arrived"


class TestSlicesAreProducedByTheHarness:
    """Ingest only DECLARES slices; the deterministic slicer must produce them.

    Nothing called slicer.slice_document before, so a real ingest left the
    extractor's payload pointing at files that never existed on disk.
    """

    def _case_with_a_real_filing(self, tmp_path, monkeypatch):
        # a real agent declares slices but writes no files; the stub writes
        # them itself, so disable that to exercise the harness path
        monkeypatch.setattr(_stub.StubDispatch, "_write_slices",
                            staticmethod(lambda call, parsed: None))
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        # replace the placeholder filing with the hand-written mini 10-Q
        import shutil
        from pathlib import Path as _P
        fixture = _P(__file__).parent / "fixtures" / "mini-10q.htm"
        target = case_dir / "inputs" / "intc-10q-2026q3.htm"
        shutil.copyfile(fixture, target)
        for path in (case_dir / "inputs" / "slices").rglob("*.txt"):
            path.unlink()
        return orch, dispatch, case_dir

    @staticmethod
    def _repair_errors(case_dir) -> list[str]:
        """Submission errors fed back to the ingest role for repair."""
        return [e["detail"].get("error") or ""
                for e in statestore.read_trace(case_dir)
                if e["event"] == "STATE_TRANSITION"
                and (e.get("detail") or {}).get("repair_at_same_role") == "ingest"]

    def test_declared_slices_are_written_from_the_source_filing(self, tmp_path,
                                                               monkeypatch):
        import hashlib
        orch, dispatch, case_dir = self._case_with_a_real_filing(tmp_path, monkeypatch)
        # an ingest that declares a slice but writes no file (like a real agent)
        declared = {
            "role": "ingest", "doc_id": "intc-10q-2026q3",
            "classification": {"form": "10-Q", "affects": ["extraction"],
                               "reason": "new quarterly statements"},
            "slices": [{
                "slice_id": "intc-10q-2026q3/IS", "kind": "income_statement",
                "path": "inputs/slices/intc-10q-2026q3/IS.txt",
                "sha256": None, "anchors": {"first_line": "Net revenue"},
            }],
            "superseded": [], "conformance": "schema 1.0", "prompt_version": "1.0",
        }
        dispatch.scripts["ingest"] = declared
        summary = orch.run()
        assert summary["status"] == "PENDING_APPROVAL", summary
        produced = case_dir / "inputs" / "slices" / "intc-10q-2026q3" / "IS.txt"
        assert produced.is_file(), "the harness must produce the declared slice"
        text = produced.read_text(encoding="utf-8")
        assert "Net revenue | 13,500 | 12,500" in text
        assert hashlib.sha256(produced.read_bytes()).hexdigest()

    def test_a_false_slice_sha256_is_repaired_not_accepted(self, tmp_path,
                                                          monkeypatch):
        orch, dispatch, case_dir = self._case_with_a_real_filing(tmp_path, monkeypatch)
        bad = {
            "role": "ingest", "doc_id": "intc-10q-2026q3",
            "classification": {"form": "10-Q", "affects": ["extraction"],
                               "reason": "new quarterly statements"},
            "slices": [{
                "slice_id": "intc-10q-2026q3/IS", "kind": "income_statement",
                "path": "inputs/slices/intc-10q-2026q3/IS.txt",
                "sha256": "0" * 64, "anchors": {"first_line": "Net revenue"},
            }],
            "superseded": [], "conformance": "schema 1.0", "prompt_version": "1.0",
        }
        dispatch.scripts["ingest"] = bad
        summary = orch.run()
        assert summary["status"] == "EXHAUSTED"
        assert summary["reason"]["code"] == "retries"
        assert any("declares sha256" in err
                   for err in self._repair_errors(case_dir))

    def test_a_slice_for_an_unknown_document_is_rejected(self, tmp_path,
                                                        monkeypatch):
        orch, dispatch, case_dir = self._case_with_a_real_filing(tmp_path, monkeypatch)
        bad = {
            "role": "ingest", "doc_id": "intc-10q-2026q3",
            "classification": {"form": "10-Q", "affects": ["extraction"],
                               "reason": "new quarterly statements"},
            "slices": [{
                "slice_id": "not-in-the-manifest/IS", "kind": "income_statement",
                "path": "inputs/slices/not-in-the-manifest/IS.txt",
                "sha256": None, "anchors": {"first_line": "x"},
            }],
            "superseded": [], "conformance": "schema 1.0", "prompt_version": "1.0",
        }
        dispatch.scripts["ingest"] = bad
        summary = orch.run()
        assert summary["status"] == "EXHAUSTED"
        assert any("not in inputs/manifest.json" in err
                   for err in self._repair_errors(case_dir))
