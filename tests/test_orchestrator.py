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

    def test_a_round_in_the_dedup_key_does_not_open_a_new_row(self, tmp_path):
        """Critics put the round in their keys; the round is not identity.

        `c4|round-008-artifacts|version-pinning-unverifiable` and the round-014
        spelling are the same complaint. Left alone, every recurrence opened a
        new row and the ledger only grew, so the candidate never converged.
        """
        led = ledger.FindingsLedger(tmp_path)
        a = led.open_finding({"summary": "x", "severity": "minor",
                              "dedup_key": "c4|round-008-artifacts|pinning"},
                             round=8)
        b = led.open_finding({"summary": "x", "severity": "minor",
                              "dedup_key": "c4|round-014-artifacts|pinning"},
                             round=14)
        assert a == b
        assert len(led.all_findings()) == 1
        assert led.by_id(a)["last_seen_round"] == 14

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
import types
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


class TestCandidateIdIgnoresPerRoundBookkeeping:
    """The id digests what the artifacts SAY, not which round said it.

    Every round stamps a fresh request_id into the artifacts and the note's
    header. Digesting that made the same candidate look new every round, so
    the clean-review streak reset forever and acceptance could never fire --
    the case could only ever exhaust.
    """

    def test_a_rebuild_with_identical_content_keeps_the_candidate_id(self, tmp_path):
        scripts = {"critic_c1": [None, _critic_findings()]}
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        ids = {c.candidate_id for c in dispatch.calls_of("critic_c1")}
        assert len(ids) == 1, ids
        st = load_state(case_dir)
        assert st["candidate"]["clean_review_rounds"] == 2
        assert st["status"] == "PENDING_APPROVAL"

    def test_the_note_header_request_id_is_not_part_of_the_digest(self):
        from coverageflow.orchestrator import _blank_candidate
        a = ("> **Candidate:** cand-aaa · **Request:** req-0001\n"
             "Revenue rose to 13,500 [EV:income_statement.revenue].\n")
        b = ("> **Candidate:** cand-bbb · **Request:** req-9999\n"
             "Revenue rose to 13,500 [EV:income_statement.revenue].\n")
        assert _blank_candidate(a) == _blank_candidate(b)


class TestCriticsRunConcurrently:
    def test_the_four_critics_are_in_flight_together(self, tmp_path):
        """Serializing them added ~6 minutes to every round.

        The four critics are independent fresh-context reviews of the same
        candidate, so nothing about the review depends on their order.
        """
        import threading
        import time

        orch, dispatch, clk, case_dir = build_env(tmp_path)
        original = dispatch.__call__
        lock = threading.Lock()
        live = {"now": 0, "peak": 0}

        def slow(call):
            if not call.role.startswith("critic_c"):
                return original(call)
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            try:
                time.sleep(0.05)
                return original(call)
            finally:
                with lock:
                    live["now"] -= 1

        orch.dispatch = slow
        orch.run()
        assert live["peak"] >= 2, live


class TestUnreproducedCriticFindingsClose:
    """A critic that stops raising a finding has verified it fixed.

    Real critics reword a complaint every round, so the dedup_key drifts
    (``valuation-section-empty-vs-manifest-artifacts`` becomes
    ``valuation-section-declares-artifacts-absent``) and the row it replaces
    never closes. They also reject their own closures on artifact+sha256
    mismatches, so nothing else drains the ledger and acceptance -- which
    requires every carried finding closed -- is unreachable.
    """

    @staticmethod
    def _clean_without_resolutions(role: str) -> dict:
        return {
            "role": role, "round": None, "verdict": "clean", "findings": [],
            "verified_resolutions": [],
            "coverage": {"rubric_version": "1.0",
                         "areas_checked": ["citation_integrity"]},
            "conformance": "schema 1.0", "prompt_version": "1.0",
        }

    def test_finding_closes_when_its_critic_does_not_raise_it_again(self, tmp_path):
        clean = self._clean_without_resolutions

        def c1(payload):
            return (_critic_findings() if payload.get("round") == 2
                    else clean("critic_c1"))

        scripts = {"critic_c1": c1}
        for i in range(2, 5):
            role = f"critic_c{i}"
            scripts[role] = (lambda r: lambda payload: clean(r))(role)
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        rows = ledger.FindingsLedger(case_dir).all_findings()
        raised = [r for r in rows if r["source_critic"] == "critic_c1"]
        assert len(raised) == 1
        assert raised[0]["status"] == "closed"
        assert raised[0]["closed_by"]["critic"] == "critic_c1"
        assert raised[0]["closed_by"]["verified_evidence"]["artifact"].endswith(
            "critic_c1.json")

    def test_a_critic_that_reports_no_coverage_closes_nothing(self, tmp_path):
        # silence from a critic that did not review is not evidence of a fix
        clean = self._clean_without_resolutions
        blind = clean("critic_c1")
        blind["coverage"] = {}

        def c1(payload):
            return _critic_findings() if payload.get("round") == 2 else blind

        scripts = {"critic_c1": c1}
        for i in range(2, 5):
            role = f"critic_c{i}"
            scripts[role] = (lambda r: lambda payload: clean(r))(role)
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        rows = ledger.FindingsLedger(case_dir).all_findings()
        raised = [r for r in rows if r["source_critic"] == "critic_c1"]
        assert raised and raised[0]["status"] == "open"


class TestSeverityAwareAcceptance:
    """Only a blocking finding stops acceptance.

    Critics raise advisory nits (a wording preference, a missing nice-to-have
    exhibit) that no amount of rebuilding removes. When every open finding
    blocked, a case with one permanent minor could never be accepted -- it
    could only burn its round budget and land in EXHAUSTED, which is not a
    verdict on the work. A ``major``/``critical`` finding still blocks.
    """

    @staticmethod
    def _finding(severity: str) -> dict:
        return {
            "role": "critic_c1", "round": None, "verdict": "findings",
            "findings": [{
                "finding_id": None, "severity": severity,
                "summary": "exhibit 4 would read better as a waterfall",
                "evidence": "report section 4 is a table",
                "affected_artifacts": ["report.md"],
                "required_evidence": "a waterfall exhibit",
            }],
            "coverage": {"rubric_version": "1.0",
                         "areas_checked": ["narrative_quality"]},
            "conformance": "schema 1.0", "prompt_version": "1.0",
        }

    def _scripts(self, severity: str) -> dict:
        # critic_c1 raises the same nit every single round: a permanent
        # advisory finding that no rebuild will ever remove
        raised = self._finding(severity)

        def c1(payload):
            return raised

        scripts = {"critic_c1": c1}
        for i in range(2, 5):
            role = f"critic_c{i}"
            scripts[role] = (lambda r: lambda payload: {
                "role": r, "round": None, "verdict": "clean", "findings": [],
                "verified_resolutions": [],
                "coverage": {"rubric_version": "1.0",
                             "areas_checked": ["citation_integrity"]},
                "conformance": "schema 1.0", "prompt_version": "1.0",
            })(role)
        return scripts

    def test_a_minor_finding_does_not_block_acceptance(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts=self._scripts("minor"))
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        # the nit is still open and rides along for the human approver
        rows = ledger.FindingsLedger(case_dir).all_findings()
        minor = [r for r in rows if r["severity"] == "minor"]
        assert minor and minor[0]["status"] == "open"
        # a permanent nit never even forces a repair round
        assert not list((case_dir / "rounds").glob("*/repair-packet.json"))

    def test_a_major_finding_still_blocks_acceptance(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts=self._scripts("major"))
        orch.run()
        st = load_state(case_dir)
        # never accepted: the major is still open and the case needs a human
        assert st["status"] == "PENDING_APPROVAL"
        assert st["candidate"]["requires_acknowledgement"] is True
        rows = ledger.FindingsLedger(case_dir).all_findings()
        major = [r for r in rows if r["severity"] == "major"]
        assert major and major[0]["status"] == "open"

    def test_repair_packet_carries_the_major_and_not_the_minor(self, tmp_path):
        # a minor must never become a repair obligation: feeding it back made
        # the critics re-raise it verbatim, so the ledger could not drain
        scripts = self._scripts("major")
        minor = self._finding("minor")
        minor["role"] = "critic_c2"
        # distinct summary: the dedup_key is derived from it, and an identical
        # one would bump the major's row instead of opening its own
        minor["findings"][0]["summary"] = "the appendix would read better as a table"
        scripts["critic_c2"] = lambda payload: minor
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        packet = json.loads((case_dir / "rounds" / "round-001" /
                             "repair-packet.json").read_text())
        assert [f["severity"] for f in packet["open_findings"]] == ["major"]
        assert [f["severity"] for f in packet["advisory_findings"]] == ["minor"]


class TestIngestReuseAcrossRounds:
    """Ingest is a pure function of the frozen input manifest.

    Re-dispatching it every round re-reads every filing and spends the whole
    role deadline on tool calls without emitting output, so a rebuild could
    never get past its first stage.
    """

    def _two_build_scripts(self):
        writer_alt = {"__stale": False, "role": "writer",
                      "report_md": _stub.ALT_REPORT_MD,
                      "conformance": "schema 1.0", "prompt_version": "1.0"}
        return {
            "critic_c1": [None, _critic_findings()],
            "writer": [_writer_output(_stub.DEFAULT_REPORT_MD), writer_alt],
        }

    def test_second_build_reuses_the_verified_ingest_report(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts=self._two_build_scripts())
        orch.run()
        st = load_state(case_dir)
        assert st["budget"]["used"]["rounds"] == 2      # two builds happened
        assert len(dispatch.calls_of("ingest")) == 1    # ingest ran once
        first = statestore.read_json(
            case_dir / "rounds" / "round-001" / "generator" / "ingest-report.json")
        second = statestore.read_json(
            case_dir / "rounds" / "round-003" / "generator" / "ingest-report.json")
        # the classification and slices are carried; the request binding is not
        for key in ("request_id", "candidate_id"):
            first.pop(key), second.pop(key)
        assert second == first
        # the reuse is traced, not silent
        events = [json.loads(line) for line in
                  (case_dir / "trace.jsonl").read_text().splitlines() if line.strip()]
        reused = [e for e in events if e.get("event") == "INGEST_REUSED"]
        assert reused and reused[0]["detail"]["from_round"] == 1

    def test_second_build_reuses_the_verified_facts(self, tmp_path):
        """The extractor reads the frozen ingest report and nothing else.

        Re-running it every round cost ~150s of the round for no new
        information.
        """
        finding = _critic_findings()
        finding["findings"][0]["affected_artifacts"] = ["report.md"]
        writer_alt = {"__stale": False, "role": "writer",
                      "report_md": _stub.ALT_REPORT_MD,
                      "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {"critic_c1": [None, finding],
                   "writer": [_writer_output(_stub.DEFAULT_REPORT_MD), writer_alt]}
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        st = load_state(case_dir)
        assert st["budget"]["used"]["rounds"] == 2
        assert len(dispatch.calls_of("extractor")) == 1
        events = [json.loads(line) for line in
                  (case_dir / "trace.jsonl").read_text().splitlines() if line.strip()]
        reused = [e for e in events if e.get("event") == "EXTRACTOR_REUSED"]
        assert reused and reused[0]["detail"]["from_round"] == 1

    def test_a_finding_against_facts_forces_a_fresh_extraction(self, tmp_path):
        """A repair packet that names facts.json must be honoured."""
        writer_alt = {"__stale": False, "role": "writer",
                      "report_md": _stub.ALT_REPORT_MD,
                      "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {"critic_c1": [None, _critic_findings()],   # names facts.json
                   "writer": [_writer_output(_stub.DEFAULT_REPORT_MD), writer_alt]}
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        assert len(dispatch.calls_of("extractor")) == 2

    def test_a_changed_input_manifest_forces_a_fresh_ingest(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        manifest_sha = statestore.sha256_json(orch.input_manifest())
        assert orch._ingest_report_reusable(1, manifest_sha) is not None
        assert orch._ingest_report_reusable(1, "0" * 64) is None

    def test_a_missing_slice_forces_a_fresh_ingest(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        report = statestore.read_json(
            case_dir / "rounds" / "round-001" / "generator" / "ingest-report.json")
        victim = case_dir / report["slices"][0]["path"]
        victim.unlink()
        assert orch._ingest_report_reusable(
            1, statestore.sha256_json(orch.input_manifest())) is None


class TestArtifactProvenance:
    """Every candidate artifact states the cutoff and the bundle it came from.

    The critic checklist requires each artifact's header to carry both. Only
    the writer's markdown had them, so the extractor described its own header
    in prose and the checklist was unsatisfiable by any build.
    """

    def test_every_json_candidate_artifact_carries_the_bundle_header(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        gen = case_dir / "rounds" / "round-001" / "generator"
        for name in ("facts.json", "forecast_assumptions.json",
                     "model.json", "valuation.json"):
            payload = json.loads((gen / name).read_text())
            prov = payload.get("provenance") or {}
            assert prov.get("cutoff_date") == "2026-10-30", name
            assert prov.get("input_manifest_sha256"), name
            assert prov.get("bundle_id"), name

    def test_engine_input_digest_is_the_digest_of_the_file_on_disk(self, tmp_path):
        # atomic_write_json pretty-prints, so sha256_json(obj) matches no file
        # and the provenance chain from facts.json into model.json resolved to
        # nothing -- a critic read that as a broken chain
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        gen = case_dir / "rounds" / "round-001" / "generator"
        model = json.loads((gen / "model.json").read_text())
        assert model["input_facts_sha256"] == statestore.sha256_file(
            gen / "facts.json")
        assert model["input_forecast_sha256"] == statestore.sha256_file(
            gen / "forecast_assumptions.json")

    def test_model_publishes_the_formula_behind_each_derived_series(self, tmp_path):
        # the only description of net debt used to be the extractor's prose,
        # which went stale and made a correct figure look unreproducible
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        model = json.loads((case_dir / "rounds" / "round-001" / "generator" /
                            "model.json").read_text())
        derivations = model.get("derivations") or {}
        assert "short_term_investments" in derivations["derived.net_debt"]
        assert "capital_expenditure" in derivations["derived.fcf"]


class TestReuseIsRecordedInTheBudget:
    """A carried-forward role spends no tokens; the zero must be explained.

    A critic saw zero cumulative spend for ingest and engines and concluded the
    per-role cap check could not be completed.
    """

    def test_a_carried_forward_role_is_named_in_the_budget(self, tmp_path):
        # a round-1 finding forces a rebuild, and the rebuild reuses ingest and
        # the extractor -- both then spend zero tokens
        raised = _critic_findings()
        # the finding must not name facts.json: an open finding against the
        # extractor's output is exactly what forces it to be re-dispatched
        raised["findings"][0]["affected_artifacts"] = ["report.md"]

        def c1(payload):
            return (raised if payload.get("round") == 1
                    else {"role": "critic_c1", "round": None, "verdict": "clean",
                          "findings": [], "verified_resolutions": [],
                          "coverage": {"rubric_version": "1.0",
                                       "areas_checked": ["citation_integrity"]},
                          "conformance": "schema 1.0", "prompt_version": "1.0"})

        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts={"critic_c1": c1})
        orch.run()
        st = load_state(case_dir)
        reused = st["budget"]["used"].get("reused_roles") or {}
        assert reused.get("ingest", 0) >= 1
        assert reused.get("extractor", 0) >= 1
        # the engines are deterministic, not reused: they must not be labelled
        assert "engines" not in reused


class TestManifestHeader:
    """The manifest must tie every artifact to the cutoff, bundle and versions.

    Critics check exactly that. Asking the lead to echo it produced a manifest
    with none of it, so the same "header incomplete" finding came back every
    round and the candidate could never converge.
    """

    def test_manifest_carries_the_provenance_header(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        manifest = statestore.read_json(
            case_dir / "rounds" / "round-001" / "generator" /
            "candidate-manifest.json")
        assert manifest["cutoff_date"] == casekit.MANDATE["cutoff_date"]
        assert manifest["input_manifest_sha256"] == statestore.sha256_json(
            orch.input_manifest())
        assert manifest["bundle_id"] == casekit.MANIFEST.get("bundle_id")
        assert manifest["bundle_sha256"] == casekit.MANIFEST.get("bundle_sha256")
        # every role that produced an artifact is pinned to a card version
        for role in ("ingest", "extractor", "model", "writer"):
            assert manifest["prompt_versions"][role]
        assert manifest["engine_versions"]["engine"]
        assert manifest["engine_versions"]["mapping"]

    def test_scope_statement_reports_reuse_instead_of_claiming_a_full_rebuild(
            self, tmp_path):
        """The lead asserted "full rebuild" every round; ingest is reused now.

        A critic that checks the claim finds the rebuild unevidenced, so the
        statement has to come from the artifact hashes.
        """
        writer_alt = {"__stale": False, "role": "writer",
                      "report_md": _stub.ALT_REPORT_MD,
                      "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {
            "critic_c1": [None, _critic_findings()],
            "writer": [_writer_output(_stub.DEFAULT_REPORT_MD), writer_alt],
        }
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        second = statestore.read_json(
            case_dir / "rounds" / "round-003" / "generator" /
            "candidate-manifest.json")
        statement = second["scope_statement"]
        assert "ingest classification and slices carried from round 001" in statement
        assert "re-emitted" in statement

    def test_every_critic_receives_every_candidate_artifact(self, tmp_path):
        """A digest is not evidence.

        Critics handed only sha256 digests reported "header uncheckable" every
        round, so the candidate could never converge.
        """
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        for role in ("critic_c1", "critic_c2", "critic_c3", "critic_c4"):
            payload = dispatch.calls_of(role)[0].payload
            supplied = payload["artifacts"]
            for name in ("ingest-report.json", "facts.json",
                         "forecast_assumptions.json", "report.md",
                         "model.json", "valuation.json"):
                assert any(k.endswith("/" + name) for k in supplied), (role, name)

    def test_writer_is_given_the_bundle_provenance(self, tmp_path):
        """The note's header block cites a bundle id the writer must be given."""
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        payload = dispatch.calls_of("writer")[0].payload
        assert payload["bundle"]["cutoff_date"] == casekit.MANDATE["cutoff_date"]
        assert payload["bundle"]["bundle_id"] == casekit.MANIFEST.get("bundle_id")
        assert payload["bundle"]["bundle_sha256"]

    def test_report_header_carries_the_computed_candidate_id(self, tmp_path):
        """The writer cannot know the id: it is a digest over every artifact.

        Leaving the writer to guess put "not assigned" in the header a
        reviewer checks.
        """
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"writer": [_writer_output(
                "> **Cutoff:** 2026-10-30 · **Bundle:** intc-2026-10 / abc\n"
                "> **Candidate:** not assigned · **Request:** req-0001\n\n"
                + _stub.DEFAULT_REPORT_MD)]})
        orch.run()
        report = (case_dir / "rounds" / "round-001" / "generator" /
                  "report.md").read_text()
        cand = load_state(case_dir)["candidate"]["candidate_id"]
        assert f"**Candidate:** {cand}" in report
        assert "not assigned" not in report

    def test_reused_ingest_and_engines_still_appear_in_the_token_ledger(
            self, tmp_path):
        """A zero-cost role must still be named, or its cap check cannot run."""
        writer_alt = {"__stale": False, "role": "writer",
                      "report_md": _stub.ALT_REPORT_MD,
                      "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {
            "critic_c1": [None, _critic_findings()],
            "writer": [_writer_output(_stub.DEFAULT_REPORT_MD), writer_alt],
        }
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        used = load_state(case_dir)["budget"]["used"]["tokens_by_role"]
        assert "ingest" in used and "engines" in used


class TestSingleWriterLock:
    """Two orchestrators on one case corrupt it.

    The second one's resume discards the first one's artifacts as unverified,
    and the round then proceeds on empty inputs -- which is exactly how a
    healthy case started failing its extractor on every retry.
    """

    def test_a_second_run_is_refused_while_the_first_holds_the_case(
            self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        lock = statestore.acquire_case_lock(case_dir)
        try:
            with pytest.raises(statestore.CaseLocked):
                orch.run()
        finally:
            statestore.release_case_lock(lock)
        # the lock is gone, so the case runs normally again
        assert orch.run()["status"] == "PENDING_APPROVAL"

    def test_a_stale_lock_from_a_dead_process_is_taken_over(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        (case_dir / statestore.LOCK_FILE).write_text(
            json.dumps({"pid": 999_999_999, "ts": "2026-01-01T00:00:00Z"}))
        assert orch.run()["status"] == "PENDING_APPROVAL"
        assert not (case_dir / statestore.LOCK_FILE).exists()


class TestBudgetExhaustion:
    def test_rounds_exhaustion_escalates_a_complete_candidate(self, tmp_path):
        # the candidate passed every gate and was fully reviewed; the only
        # thing left is a critic objection the machine cannot settle, so the
        # case goes to a human instead of dying as EXHAUSTED
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        assert st["candidate"]["requires_acknowledgement"] is True
        assert st["reason"]["code"] == "ESCALATED"
        assert st["reason"]["detail"]["budget"]["code"] == "rounds"
        assert st["reason"]["detail"]["blocking_finding_ids"]
        assert st["budget"]["used"]["rounds"] == 2
        events = [e["event"] for e in statestore.read_trace(case_dir)]
        assert events.count("ESCALATED") == 1

    def test_rounds_exhaustion_still_exhausts_an_unverified_candidate(self, tmp_path):
        # a gate failure means the work is unverified: that is not something a
        # human can sign off, so EXHAUSTED stays the answer
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(tmp_path, mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        # the happy path accepts before the budget matters
        assert st["status"] == "PENDING_APPROVAL"
        assert not st["candidate"].get("requires_acknowledgement")

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
        # the R2 rebuild breaches at the first re-dispatched role. Ingest is
        # reused across rounds (frozen inputs), so that role is the extractor.
        assert st["reason"]["detail"]["role"] == "extractor"
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

    def test_exhausted_case_reopens_with_an_explicit_budget_grant(self, tmp_path):
        """Exhaustion is a budget verdict, not a tombstone.

        Without this the only recovery is a brand-new case, which is how a
        workspace fills with dead cases that all point at the same bundle.
        """
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["role_tokens_max"] = 1000
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
        dispatch.tokens_in = 700
        dispatch.tokens_out = 600
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED"
        role = st["reason"]["detail"]["role"]

        # a plain re-run must NOT silently reopen it
        orch.run()
        assert load_state(case_dir)["status"] == "EXHAUSTED"
        assert load_state(case_dir)["budget"]["used"]["grants"] == []

        orch.run(extend=True)
        st = load_state(case_dir)
        grants = st["budget"]["used"]["grants"]
        assert len(grants) == 1
        assert grants[0]["role"] == role
        assert grants[0]["by"] == "human"
        assert grants[0]["role_tokens_max_before"] == 1000
        assert grants[0]["role_tokens_max_after"] == 2000
        assert st["budget"]["role_tokens_max"] == 2000
        # the counters restarted from zero at grant time, so the allowance is
        # actually spendable (the snapshot proves it)
        assert grants[0]["tokens_at_grant"]["ingest"] == 1300
        # the grant reopens the run; it never launders exhaustion into
        # an approval
        assert st["status"] != "APPROVED"
        # and the case is not a dead end: a further grant is still possible
        orch.run(extend=True)
        assert len(load_state(case_dir)["budget"]["used"]["grants"]) == 2

    def test_grant_also_restarts_the_retry_counters(self, tmp_path):
        """A case that exhausted on retries must not stop on its next dispatch.

        Retries are spent like tokens. Reopening without resetting them made
        the grant a no-op: the run resumed and immediately exhausted again on
        the same counter.
        """
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["retries_max"] = 1
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"extractor": [{"__invalid": True}] * 10},
            mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "EXHAUSTED"
        assert st["reason"]["code"] == "retries"
        assert st["budget"]["used"]["retries_by_role"]["extractor"] >= 1
        calls_before = len(dispatch.calls_of("extractor"))

        orch.run(extend=True)
        st = load_state(case_dir)
        grant = st["budget"]["used"]["grants"][-1]
        # the grant record must not carry the pre-grant retry counts: a critic
        # reading them sees a live breach of retries_max that the grant already
        # forgave, and raises it as a finding every round
        assert "retries_at_grant" not in grant
        # the grant was spendable: the role was dispatched again instead of
        # stopping on the counter it had already exhausted
        assert len(dispatch.calls_of("extractor")) > calls_before

    def test_escalation_is_not_acceptance(self, tmp_path):
        # escalation hands the case to a human; it does not approve anything.
        # A blanket approval that swallows the objections is refused.
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        with pytest.raises(RuntimeError, match="not acknowledged"):
            orch.record_approval(decision="APPROVED", by="Sup")
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"

    def test_an_approver_may_sign_off_over_named_objections(self, tmp_path):
        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(
            tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
        orch.run()
        st = load_state(case_dir)
        ids = st["reason"]["detail"]["blocking_finding_ids"]
        out = orch.record_approval(decision="APPROVED", by="Sup",
                                   reasoning="the objection is wrong: the blend "
                                             "does include the segment",
                                   acknowledge_findings=ids)
        assert out["terminal"] == "APPROVED"
        result = statestore.read_json(case_dir / "result.json")
        assert result["reason"]["detail"]["acknowledged_findings"] == ids
        assert "acknowledged" in result["reason"]["message"]


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
        # and the case can never accept -- it escalates to a human instead.
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
        assert st["status"] == "PENDING_APPROVAL"
        assert st["candidate"]["requires_acknowledgement"] is True
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


class TestCaseCreationMaterializesTheBundle:
    """A case manifest only declares paths; creation must copy the filings.

    Without this every case is born blocked on missing_inputs, and the only
    way forward is another case — which is how a workspace fills with dead
    cases that all point at the same frozen bundle.
    """

    def _bundle(self, tmp_path):
        import hashlib
        import json

        dest = tmp_path / "bundle"
        (dest / "inputs").mkdir(parents=True)
        blob = b"<html>10-Q</html>"
        rel = "inputs/intc-10q-2026q3.htm"
        (dest / rel).write_bytes(blob)
        manifest = {
            "as_of_cutoff": "2026-10-30",
            "documents": [{
                "doc_id": "intc-10q-2026q3", "form": "10-Q",
                "filed": "2026-10-30", "period": "2026-09-26", "path": rel,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob), "source_url": "https://www.sec.gov/x"}],
            "bundle_id": "intc-2026-10", "bundle_sha256": "a" * 64,
        }
        (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return dest, manifest

    def test_create_case_copies_the_filings_so_the_case_is_runnable(self, tmp_path):
        bundle_dir, manifest = self._bundle(tmp_path)
        dispatch = _stub.StubDispatch()
        orch = Orchestrator.create_case(
            tmp_path, "intc-fy2026q3-20261030", casekit.MANDATE, manifest,
            dispatch=dispatch,
            gate_engine=gates.GateEngine(
                schema_path=SCHEMA_PATH, build_model_fn=stub_build_model,
                value_fn=stub_value_company),
            bundle_dir=bundle_dir)
        case_dir = orch.case_dir
        assert (case_dir / "inputs" / "intc-10q-2026q3.htm").read_bytes() == \
            (bundle_dir / "inputs" / "intc-10q-2026q3.htm").read_bytes()
        # the case starts, instead of blocking on inputs it never had
        summary = orch.run()
        assert summary["status"] == "PENDING_APPROVAL", summary
        assert dispatch.calls

    def test_create_case_without_a_bundle_still_blocks_on_missing_inputs(self, tmp_path):
        _bundle_dir, manifest = self._bundle(tmp_path)
        orch = Orchestrator.create_case(
            tmp_path, "intc-fy2026q3-20261030", casekit.MANDATE, manifest,
            dispatch=_stub.StubDispatch(),
            gate_engine=gates.GateEngine(
                schema_path=SCHEMA_PATH, build_model_fn=stub_build_model,
                value_fn=stub_value_company))
        summary = orch.run()
        assert summary["status"] == "BLOCKED"
        assert summary["reason"]["code"] == "missing_inputs"


class TestBlockedCasesAreRerunnable:
    """A block is an instruction ("fix the cause, then re-run"), not a tombstone.

    Only APPROVAL_EXPIRED needs a fresh decision; every other block must be
    clearable by re-running the SAME case, so a transient backend failure
    never forces a brand-new case.
    """

    def _blocked_mid_build(self, tmp_path, code="invalid_submission",
                           role="ingest"):
        """A case that started round 1 and blocked at its first role."""
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        state = load_state(case_dir)
        state["status"] = "BLOCKED"
        state["pending"]["round"] = 1
        state["pending"]["stage"] = "ingest"
        state["pending"]["role"] = "generator"
        state["pending"]["case_clock_started"] = clk.iso()
        state["budget"]["used"]["retries_by_role"] = {role: 99}
        state["reason"] = {"code": code, "message": "role could not submit",
                           "detail": {"role": role, "error": "backend timeout"}}
        statestore.write_state(case_dir, state, now=clk.iso)
        statestore.atomic_write_json(case_dir / "result.json",
                                     {"case_id": case_dir.name,
                                      "terminal": "BLOCKED",
                                      "reason": state["reason"]})
        return orch, dispatch, case_dir

    def test_rerun_clears_the_block_and_resumes_the_same_case(self, tmp_path):
        orch, dispatch, case_dir = self._blocked_mid_build(tmp_path)
        summary = orch.run()
        assert summary["status"] == "PENDING_APPROVAL", summary
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        assert st["reason"] is None
        # the stale terminal result is gone; the block stays in the trace
        assert not (case_dir / "result.json").exists()
        events = [e["event"] for e in statestore.read_trace(case_dir)]
        assert "STATE_TRANSITION" in events
        unblocked = [e for e in statestore.read_trace(case_dir)
                     if (e.get("detail") or {}).get("unblocked")]
        assert unblocked and unblocked[-1]["detail"]["unblocked"] == "invalid_submission"

    def test_rerun_resets_the_repair_budget_for_the_blocked_role(self, tmp_path):
        orch, dispatch, case_dir = self._blocked_mid_build(tmp_path)
        # one invalid submission would exhaust a 99-deep retry count; the
        # reset gives the repair the human just made a full budget
        dispatch.scripts["ingest"] = [{"__invalid": True}]
        summary = orch.run()
        assert summary["status"] == "PENDING_APPROVAL", summary
        st = load_state(case_dir)
        assert st["budget"]["used"]["retries_by_role"]["ingest"] == 1

    def test_approval_expired_is_not_cleared_by_a_rerun(self, tmp_path):
        orch, dispatch, case_dir = self._blocked_mid_build(
            tmp_path, code="APPROVAL_EXPIRED", role=None)
        summary = orch.run()
        assert summary["status"] == "BLOCKED"
        assert "re-approval" in summary["message"]
        assert dispatch.calls == []


class TestCaseDirIsAbsolute:
    """The case dir is a backend's working directory, so it must be absolute.

    A relative path is resolved by the CLI backend against its own cwd — which
    IS that directory — so the backend reports the workdir missing and every
    dispatch fails. That burned the whole retry budget on a path bug.
    """

    def test_relative_workspace_still_yields_an_absolute_workdir(self, tmp_path,
                                                                monkeypatch):
        build_env(tmp_path)
        monkeypatch.chdir(tmp_path)
        orch = Orchestrator(
            Path("cases") / "intc-fy2026q3-20261030",
            dispatch=_stub.StubDispatch(),
            gate_engine=gates.GateEngine(
                schema_path=SCHEMA_PATH, build_model_fn=stub_build_model,
                value_fn=stub_value_company))
        assert orch.case_dir.is_absolute()
        assert orch.case_dir == (tmp_path / "cases" /
                                 "intc-fy2026q3-20261030").resolve()

    def test_every_dispatch_carries_an_absolute_workdir(self, tmp_path,
                                                        monkeypatch):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        monkeypatch.chdir(tmp_path)
        orch.run()
        assert dispatch.calls
        for call in dispatch.calls:
            assert Path(call.workdir).is_absolute(), call.role


class TestExtractorPromptIsSelfContained:
    """The extractor must receive the statement text, not just its path.

    A prompt that only names ``inputs/slices/.../IS.txt`` forces the model to
    go read files, which turns one completion into a multi-minute agentic
    exploration. The text is inlined (hash-verified) so the call is a plain
    completion.
    """

    def _case_with_slices(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        return orch, dispatch, case_dir

    def test_extractor_payload_carries_the_slice_text(self, tmp_path):
        orch, dispatch, case_dir = self._case_with_slices(tmp_path)
        extractor_calls = dispatch.calls_of("extractor")
        assert extractor_calls
        slices = extractor_calls[0].payload["prior_artifacts"]["slices"]
        assert slices
        for entry in slices:
            assert entry.get("text"), entry.get("slice_id")
            assert "text" in entry

    def test_writer_payload_carries_the_computed_valuation(self, tmp_path):
        """The writer card's read scope includes the model and valuation.

        Withholding them left the note able to say only that the computed DCF
        and comps were "absent from the bound request" -- which was true of
        the request, and made every valuation section unciteable.
        """
        orch, dispatch, case_dir = self._case_with_slices(tmp_path)
        writer = dispatch.calls_of("writer")[0].payload["prior_artifacts"]
        assert writer["model"]["series"]["income_statement.revenue"]
        assert writer["valuation"]["dcf"]["per_diluted_share"] is not None
        assert writer["forecast"]

    def test_other_roles_do_not_carry_the_slice_text(self, tmp_path):
        orch, dispatch, case_dir = self._case_with_slices(tmp_path)
        for role in ("model", "writer", "generator_lead"):
            calls = dispatch.calls_of(role)
            assert calls, role
            slices = (calls[0].payload.get("prior_artifacts") or {}).get("slices") or []
            for entry in slices:
                assert "text" not in entry, role

    def test_a_tampered_slice_is_not_inlined(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        # corrupt a slice after ingest declares it but before the extractor
        # is dispatched: the hash no longer matches, so no text is inlined
        original = orch._slice_payload

        def tampering(slices, *, inline_text):
            # corrupt the slice files BEFORE the text is read, so the
            # declared sha256 no longer matches
            for entry in slices or []:
                rel = (entry or {}).get("path")
                if rel and (case_dir / rel).is_file():
                    (case_dir / rel).write_text("TAMPERED", encoding="utf-8")
            return original(slices, inline_text=inline_text)

        orch._slice_payload = tampering
        orch.run()
        slices = dispatch.calls_of("extractor")[0].payload["prior_artifacts"]["slices"]
        assert all(entry.get("text") is None for entry in slices)


class TestGateFindingLifecycle:
    def test_a_gate_finding_the_gate_no_longer_reproduces_is_closed(self, tmp_path):
        """A phantom gate finding must not haunt every later repair packet.

        Only a critic can close a critic finding, but a gate finding whose gate
        now passes has been verified fixed by the gate itself.
        """
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        fid = orch.ledger().open_finding({
            "dedup_key": "unlinked-number:1:0:2026",
            "source_critic": "gate:report_evidence_links",
            "severity": "major",
            "summary": 'numeric token "2026" on line 1 lacks an evidence reference',
            "affected_artifacts": ["report.md"],
            "required_evidence": 'an [EV:...] evidence reference for "2026"',
        }, round=1)
        assert [r["finding_id"] for r in orch.ledger().open_findings()] == [fid]

        orch.run()

        row = orch.ledger().by_id(fid)
        assert row["status"] == "closed"
        assert row["closed_by"]["critic"] == "gate:report_evidence_links"
        assert row["closed_by"]["verified_evidence"]["artifact"].endswith(
            "gates.json")
        assert row["closed_by"]["verified_evidence"]["sha256"]

    def test_a_reproduced_gate_finding_stays_open(self, tmp_path):
        """Closing is decided by reproduction, not by the row merely existing."""
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        finding = {
            "gate": "report_evidence_links",
            "dedup_key": "unlinked-number:1:0:2026",
            "severity": "major", "summary": "x",
            "affected_artifacts": ["report.md"], "required_evidence": "y",
        }

        class _AlwaysFailing:
            build_model_fn = stub_build_model
            value_fn = stub_value_company

            def run_all(self, ctx):
                outcome = types.SimpleNamespace(
                    gate="report_evidence_links", ok=False, findings=[finding])
                return types.SimpleNamespace(
                    passed=False, results=[outcome], finding_dicts=[finding])

        orch.gate_engine = _AlwaysFailing()
        fid = orch.ledger().open_finding(
            {**finding, "source_critic": "gate:report_evidence_links"}, round=1)
        orch.run()
        assert orch.ledger().by_id(fid)["status"] == "open"


class TestCriticPayload:
    def test_critics_receive_the_artifacts_they_are_contracted_to_read(self, tmp_path):
        """A critic handed only sha256 digests cannot verify anything.

        Every round then produces "unverifiable" findings, the clean-review
        streak never advances, and the case exhausts with a report nobody
        actually reviewed.
        """
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()

        calls = [c for c in dispatch.calls if c.role.startswith("critic_c")]
        assert calls
        by_role = {}
        for c in calls:
            by_role.setdefault(c.role, c.payload)

        c1 = by_role["critic_c1"]
        assert "facts.json" in " ".join(c1["artifacts"])
        assert any("report.md" in rel for rel in c1["artifacts"])
        facts = next(v for k, v in c1["artifacts"].items() if k.endswith("facts.json"))
        assert facts.get("facts"), "facts.json content must be inline, not a digest"

        c2 = by_role["critic_c2"]
        assert any("valuation.json" in rel for rel in c2["artifacts"])
        assert c2["mandate"]["assumptions"]["forecast_bounds"], \
            "C2 needs the mandate bounds"

        c4 = by_role["critic_c4"]
        assert "tokens_by_role" in c4["dispatch_records"]
        assert c4["dispatch_records"]["role_tokens_max"] > 0


class TestExtractionScope:
    """The model needs an annual anchor, not just the trigger filing.

    A 10-Q prints no annual income statement, so extracting only the trigger
    left every FY column empty: the workbook had no income statement, no
    annual cash flow, and no annual ratios.
    """

    @staticmethod
    def _manifest(*docs):
        import copy
        base = copy.deepcopy(casekit.MANIFEST)
        base["documents"] = [dict(d) for d in docs]
        return base

    @staticmethod
    def _doc(doc_id, form, filed):
        return {"doc_id": doc_id, "form": form, "filed": filed,
                "period": filed, "path": f"inputs/{doc_id}.htm",
                "sha256": "f" * 64, "bytes": 1024,
                "source_url": "https://www.sec.gov/Archives/example.htm"}

    def test_scope_names_the_trigger_and_the_latest_annual_filing(self, tmp_path):
        manifest = self._manifest(
            self._doc("intc-10k-fy2024", "10-K", "2025-01-31"),
            self._doc("intc-10k-fy2025", "10-K", "2026-01-23"),
            self._doc("intc-10q-2026q2", "10-Q", "2026-07-24"),
            self._doc("intc-10q-2026q3", "10-Q", "2026-10-30"),
            self._doc("intc-10q-2026q4", "10-Q", "2027-01-29"),  # after cutoff
        )
        orch, _dispatch, _clk, _case_dir = build_env(tmp_path, manifest=manifest)
        roles = {d["role"]: d["doc_id"] for d in orch._extraction_scope()}
        assert roles["trigger"] == "intc-10q-2026q3"
        assert roles["annual_anchor"] == "intc-10k-fy2025"

    def test_scope_never_reaches_past_the_cutoff(self, tmp_path):
        manifest = self._manifest(
            self._doc("intc-10k-fy2025", "10-K", "2026-01-23"),
            self._doc("intc-10q-2026q3", "10-Q", "2026-10-30"),
            self._doc("intc-10q-2026q4", "10-Q", "2027-01-29"),
        )
        orch, _dispatch, _clk, _case_dir = build_env(tmp_path, manifest=manifest)
        cutoff = orch.mandate()["cutoff_date"]
        for doc in orch._extraction_scope():
            assert doc["filed"] <= cutoff, doc

    def test_a_10k_triggered_case_lists_it_once(self, tmp_path):
        manifest = self._manifest(
            self._doc("intc-10k-fy2025", "10-K", "2026-01-23"),
        )
        orch, _dispatch, _clk, _case_dir = build_env(tmp_path, manifest=manifest)
        ids = [d["doc_id"] for d in orch._extraction_scope()]
        assert ids == ["intc-10k-fy2025"], ids


class TestBudgetFindingsAreNotRepairWork:
    """A critic reporting the run's token usage cannot be answered by a rewrite.

    The tokens are already spent, so the finding reopened every round and the
    repair loop could never converge. The budget tracker already records the
    breach and the run exhausts on its own.
    """

    def test_a_token_cap_finding_is_recognised(self):
        from coverageflow.orchestrator import _is_self_referential_finding
        assert _is_self_referential_finding({
            "dedup_key": "c4|candidate-manifest.json|per-role-token-cap-breach-generator-lead",
            "summary": "Per-role token cap breached: generator_lead exceeds the cap.",
        })

    def test_a_content_finding_is_not(self):
        from coverageflow.orchestrator import _is_self_referential_finding
        assert not _is_self_referential_finding({
            "dedup_key": "unknown-ev:derived.revenue_yoy",
            "summary": "evidence marker names a key not in the index",
        })

    def test_growth_series_get_the_name_the_report_uses(self):
        from coverageflow.orchestrator import _growth_aliases
        model = {"series": {
            "income_statement.revenue": {"FY2025": 52853.0,
                                         "yoy": {"FY2025": -0.0047}},
            "income_statement.diluted_eps": {"FY2025": -0.06,
                                             "yoy": {"FY2025": -0.986}},
            "balance_sheet.total_assets": {"FY2025": 211429.0},
        }}
        assert _growth_aliases(model) == ["derived.revenue_yoy",
                                          "derived.diluted_eps_yoy"]

    def test_a_series_without_growth_gets_no_alias(self):
        from coverageflow.orchestrator import _growth_aliases
        assert _growth_aliases({"series": {
            "balance_sheet.total_assets": {"FY2025": 211429.0}}}) == []
