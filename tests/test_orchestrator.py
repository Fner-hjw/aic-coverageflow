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
