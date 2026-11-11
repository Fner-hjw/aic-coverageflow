"""Finding owners (diagnosis 3.2): assignment, repair-packet scope, BLOCKED stop."""

from __future__ import annotations

import json

import pytest

import casekit
from coverageflow import _dispatch_test_stub as _stub
from coverageflow import ledger, render_note, statestore
from tests.test_orchestrator import (_critic_findings, _writer_output,
                                     build_env, load_state)


def _finding(artifacts, owner=None, summary="a defect"):
    f = _critic_findings(summary=summary)
    f["findings"][0]["affected_artifacts"] = list(artifacts)
    if owner:
        f["findings"][0]["owner"] = owner
    return f


def _packet(case_dir):
    files = sorted((case_dir / "rounds").glob("*/repair-packet.json"))
    assert files, "no repair packet was issued"
    return json.loads(files[0].read_text())


def _events(case_dir, name):
    return [e for e in statestore.read_trace(case_dir) if e["event"] == name]


# ---------------------------------------------------------------- the table
class TestOwnerAssignment:
    @pytest.mark.parametrize("gate,owner", [
        ("facts_schema", "extractor"), ("citation_presence", "extractor"),
        ("period_coverage", "extractor"), ("forecast_bounds", "model"),
        ("terminal_reconciliation", "model"), ("report_evidence_links", "writer"),
        ("input_cutoff", "system"), ("candidate_manifest_integrity", "system"),
        ("controls_conformance", "system"), ("recompute", "system"),
    ])
    def test_gate_findings_are_owned_by_gate(self, gate, owner):
        assert ledger.assign_owner({"source_critic": f"gate:{gate}"}) == owner

    def test_a_gate_finding_keeps_the_owner_it_already_carries(self):
        f = {"source_critic": "gate:facts_schema", "owner": "system"}
        assert ledger.assign_owner(f) == "system"

    @pytest.mark.parametrize("artifact,owner", [
        ("facts.json", "extractor"), ("ingest-report.json", "extractor"),
        ("forecast_assumptions.json", "model"), ("report.md", "writer"),
        ("model.json", "system"), ("valuation.json", "system"),
        ("candidate-manifest.json", "system"),
        ("rounds/round-003/generator/valuation.json", "system"),
    ])
    def test_critic_findings_follow_the_affected_artifact(self, artifact, owner):
        f = {"source_critic": "critic_c1", "affected_artifacts": [artifact]}
        assert ledger.assign_owner(f) == owner

    def test_the_earliest_affected_artifact_decides(self):
        f = {"source_critic": "critic_c2",
             "affected_artifacts": ["report.md", "forecast_assumptions.json"]}
        assert ledger.assign_owner(f) == "model"
        f["affected_artifacts"] = ["report.md", "facts.json", "model.json"]
        assert ledger.assign_owner(f) == "extractor"

    def test_an_explicit_critic_owner_wins_only_when_valid(self):
        base = {"source_critic": "critic_c2", "affected_artifacts": ["report.md"]}
        assert ledger.assign_owner(dict(base, owner="mandate")) == "mandate"
        assert ledger.assign_owner(dict(base, owner="system")) == "system"
        assert ledger.assign_owner(dict(base, owner="the intern")) == "writer"

    def test_no_known_artifact_defaults_to_the_writer(self):
        assert ledger.assign_owner({"source_critic": "critic_c4"}) == "writer"

    def test_every_ledger_row_carries_an_owner(self, tmp_path):
        led = ledger.FindingsLedger(tmp_path)
        led.open_finding({"source_critic": "gate:recompute", "summary": "x",
                          "dedup_key": "k"}, round=1)
        led.open_finding({"source_critic": "critic_c1", "summary": "y",
                          "affected_artifacts": ["facts.json"]}, round=1)
        rows = ledger.FindingsLedger(tmp_path).all_findings()
        assert [r["owner"] for r in rows] == ["system", "extractor"]

    def test_rows_written_before_owners_existed_get_one_on_load(self, tmp_path):
        statestore.atomic_write_json(tmp_path / "findings.json", {"findings": [{
            "finding_id": "fnd-0001", "critic_ref": "f", "dedup_key": "k",
            "source_critic": "critic_c1", "status": "open", "severity": "major",
            "summary": "s", "affected_artifacts": ["forecast_assumptions.json"]}]})
        assert ledger.FindingsLedger(tmp_path).all_findings()[0]["owner"] == "model"

    def test_only_role_owned_findings_are_repairable(self, tmp_path):
        led = ledger.FindingsLedger(tmp_path)
        for i, art in enumerate(["facts.json", "report.md", "model.json"]):
            led.open_finding({"source_critic": "critic_c1", "summary": art,
                              "severity": "major",
                              "affected_artifacts": [art]}, round=1)
        assert {r["owner"] for r in led.repairable_findings()} == {"extractor", "writer"}
        assert [r["owner"] for r in led.stopping_findings()] == ["system"]


# ------------------------------------------------------- system / mandate stop
class TestOwnerBlocksTheCase:
    def _blocked(self, tmp_path, owner, artifacts=("valuation.json",)):
        scripts = {"critic_c2": [None, _finding(artifacts, owner=owner,
                                                summary="exit multiple of 14x is unsupported")]}
        scripts["critic_c2"][1]["role"] = "critic_c2"
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        return orch, dispatch, case_dir

    def test_a_system_finding_ends_blocked_with_evolution_input(self, tmp_path):
        orch, dispatch, case_dir = self._blocked(tmp_path, owner=None)
        st = load_state(case_dir)
        assert st["status"] == "BLOCKED"
        assert st["reason"]["code"] == "needs_system_change"
        listed = st["reason"]["detail"]["findings"]
        assert [f["owner"] for f in listed] == ["system"]
        assert "exit multiple" in listed[0]["summary"]
        evo = statestore.read_json(case_dir / "evolution-input.json")
        assert evo["reason_code"] == "needs_system_change"
        assert evo["candidate_id"] == st["candidate"]["candidate_id"]
        assert evo["findings"][0]["finding_id"] == listed[0]["finding_id"]
        assert evo["case_id"] == st["case_id"]
        assert statestore.read_json(case_dir / "result.json")["terminal"] == "BLOCKED"

    def test_no_repair_packet_and_no_rebuild(self, tmp_path):
        orch, dispatch, case_dir = self._blocked(tmp_path, owner=None)
        assert not list((case_dir / "rounds").glob("*/repair-packet.json"))
        assert load_state(case_dir)["budget"]["used"]["rounds"] == 1
        assert len(dispatch.calls_of("writer")) == 1

    def test_a_mandate_finding_has_its_own_code(self, tmp_path):
        orch, dispatch, case_dir = self._blocked(
            tmp_path, owner="mandate", artifacts=("report.md",))
        st = load_state(case_dir)
        assert st["status"] == "BLOCKED"
        assert st["reason"]["code"] == "needs_mandate_change"
        assert statestore.read_json(
            case_dir / "evolution-input.json")["reason_code"] == "needs_mandate_change"

    def test_rerunning_a_blocked_case_reports_and_does_not_loop(self, tmp_path):
        orch, dispatch, case_dir = self._blocked(tmp_path, owner=None)
        before = len(statestore.read_trace(case_dir))
        calls = sum(len(dispatch.calls_of(r)) for r in
                    ("extractor", "model", "writer", "critic_c1", "critic_c2"))
        out = orch.run()
        assert out["status"] == "BLOCKED"
        assert out["reason"]["code"] == "needs_system_change"
        assert "evolution-input.json" in out["message"]
        assert load_state(case_dir)["status"] == "BLOCKED"
        assert len(statestore.read_trace(case_dir)) == before
        assert calls == sum(len(dispatch.calls_of(r)) for r in
                            ("extractor", "model", "writer", "critic_c1", "critic_c2"))

    def test_a_gate_system_finding_blocks_the_same_way(self, tmp_path):
        import copy
        manifest = copy.deepcopy(casekit.MANIFEST)
        manifest["documents"].append(dict(manifest["documents"][0],
                                          doc_id="intc-10q-2026q4", filed="2026-11-15"))
        orch, dispatch, clk, case_dir = build_env(tmp_path, manifest=manifest)
        orch.run()
        st = load_state(case_dir)
        assert st["reason"]["code"] == "needs_system_change"
        assert not list((case_dir / "rounds").glob("*/repair-packet.json"))
        assert dispatch.calls_of("critic_c1") == []     # stopped before review

    def test_a_system_blocker_outranks_a_repairable_one(self, tmp_path):
        scripts = {"critic_c1": [None, _finding(["report.md"], summary="wording")],
                   "critic_c2": [None, dict(_finding(["valuation.json"], owner="system",
                                                     summary="engine defect"),
                                            role="critic_c2")]}
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        st = load_state(case_dir)
        assert st["reason"]["code"] == "needs_system_change"
        assert st["reason"]["detail"]["other_open_blocking_finding_ids"]
        assert not list((case_dir / "rounds").glob("*/repair-packet.json"))

    def test_the_repair_packet_lists_only_role_owned_findings(self, tmp_path):
        scripts = {"critic_c1": [None, _finding(["report.md"], summary="wording")],
                   "critic_c2": [None, dict(_finding(["model.json"], summary="minor engine nit"),
                                            role="critic_c2")]}
        scripts["critic_c2"][1]["findings"][0]["severity"] = "minor"
        scripts["critic_c2"][1]["verdict"] = "clean"
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        packet = _packet(case_dir)
        assert {f["owner"] for f in packet["open_findings"]} == {"writer"}
        # a minor system finding is advisory: it never blocks the case
        assert load_state(case_dir)["status"] != "BLOCKED"


