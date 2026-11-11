"""Rubric v1.1: checklist-shaped critic reports and the controls gate."""
from __future__ import annotations

import copy
import json
import re

from coverageflow import gates, ledger, statestore
from coverageflow import _prompt_assembly as pa

from tests.test_orchestrator import build_env, _critic_findings


def _report(role="critic_c1", ids=("C1.1", "C1.2", "C1.3", "C1.4", "C1.5"),
            failed=(), severity="major", resolutions=()):
    findings = [{"finding_id": f"f{i}", "check_id": c, "severity": severity,
                 "summary": "x", "evidence": "y",
                 "affected_artifacts": ["report.md"], "required_evidence": "z"}
                for i, c in enumerate(failed)]
    has_major = severity == "major" and bool(findings)
    return {"role": role, "verdict": "findings" if has_major else "clean",
            "checks": [{"check_id": c, "result": "fail" if c in failed else "pass",
                        "evidence": "e"} for c in ids],
            "findings": findings, "verified_resolutions": list(resolutions)}


class TestCriticReportValidation:
    def test_complete_report_is_valid(self, tmp_path):
        orch, *_ = build_env(tmp_path)
        assert orch._validate_critic_report("critic_c1", _report()) is None

    def test_missing_check_id_is_invalid(self, tmp_path):
        orch, *_ = build_env(tmp_path)
        rep = _report()
        rep["checks"] = [c for c in rep["checks"] if c["check_id"] != "C1.3"]
        err = orch._validate_critic_report("critic_c1", rep)
        assert err and "C1.3" in err

    def test_minor_only_gives_a_clean_verdict(self, tmp_path):
        orch, *_ = build_env(tmp_path)
        rep = _report(failed=("C1.4",), severity="minor")
        assert rep["verdict"] == "clean"
        assert orch._validate_critic_report("critic_c1", rep) is None
        rep["verdict"] = "findings"          # minors never make it "findings"
        assert "verdict" in orch._validate_critic_report("critic_c1", rep)

    def test_finding_for_a_passed_check_is_invalid(self, tmp_path):
        orch, *_ = build_env(tmp_path)
        rep = _report(failed=("C1.1",))
        rep["checks"][0]["result"] = "pass"
        assert orch._validate_critic_report("critic_c1", rep)

    def test_silence_on_a_carried_finding_is_invalid(self, tmp_path):
        orch, *_ = build_env(tmp_path)
        fid = orch.ledger().open_finding(
            {"source_critic": "critic_c1", "check_id": "C1.1", "severity": "major",
             "summary": "s", "affected_artifacts": ["report.md"]}, round=1)
        silent = _report()
        err = orch._validate_critic_report("critic_c1", silent)
        assert err and fid in err
        # resolving it, or re-emitting it with the same check, is not silence
        resolved = _report(resolutions=[{"finding_id": fid}])
        assert orch._validate_critic_report("critic_c1", resolved) is None
        kept = _report(failed=("C1.1",))
        assert orch._validate_critic_report("critic_c1", kept) is None

    def test_silence_goes_through_the_same_role_retry(self, tmp_path):
        """An invalid critic report is repaired at the same role, no new path."""
        bad = _critic_findings()
        bad["checks"] = [{"check_id": "C1.1", "result": "pass", "evidence": "e"}]
        orch, dispatch, *_ = build_env(tmp_path, scripts={"critic_c1": [bad]})
        orch.run()
        calls = dispatch.calls_of("critic_c1")
        assert len(calls) >= 2
        assert "check IDs" in calls[1].payload["submission_feedback"]

    def test_payloads_carry_only_the_area_findings(self, tmp_path):
        orch, *_ = build_env(tmp_path)
        orch.ledger().open_finding(
            {"source_critic": "critic_c2", "check_id": "C2.2", "severity": "major",
             "summary": "s", "affected_artifacts": ["model.json"]}, round=1)
        assert orch._area_carried("critic_c1") == []
        assert len(orch._area_carried("critic_c2")) == 1


class TestDedupKey:
    def test_stable_across_rounds_with_reworded_summaries(self):
        a = {"source_critic": "critic_c2", "check_id": "C2.1",
             "summary": "Revenue growth in the note is 4%, the model says 3%",
             "affected_artifacts": ["report.md", "model.json"]}
        b = {"source_critic": "critic_c2", "check_id": "C2.1",
             "summary": "growth rate differs from model (round 3)",
             "dedup_key": "something-new-round-3",
             "affected_artifacts": ["model.json", "report.md"]}
        assert ledger.compute_dedup_key(a) == ledger.compute_dedup_key(b)
        b["check_id"] = "C2.2"
        assert ledger.compute_dedup_key(a) != ledger.compute_dedup_key(b)


MANDATE = {"cutoff_date": "2026-10-30"}
INPUTS = {"bundle_id": "b", "documents": []}


def _good(tmp_path):
    sha = statestore.sha256_json(INPUTS)
    d = tmp_path / "rounds/round-001/generator"
    d.mkdir(parents=True)
    names = ["report.md", "facts.json"]
    for n in names:
        (d / n).write_text("x")
    manifest = {
        "candidate_id": "cand-1", "request_id": "req-5",
        "cutoff_date": "2026-10-30", "input_manifest_sha256": sha,
        "prompt_versions": {"writer": "1.3"}, "engine_versions": {"mapping": "1.0"},
        "reports": [{"artifact": f"rounds/round-001/generator/{n}"} for n in names]}
    hdr = {"request_id": "req-2", "candidate_id": "cand-1",
           "provenance": {"cutoff_date": "2026-10-30", "input_manifest_sha256": sha}}
    controls = {"artifact_headers": {"facts.json": hdr, "model.json":
                                     {"candidate_id": "cand-1",
                                      "provenance": hdr["provenance"]}},
                "expected_prompt_versions": {"writer": "1.3"},
                "pinned_mapping_version": "1.0",
                "tokens_by_role": {"writer": 100}, "role_tokens_max": 1000}
    note = "> **Candidate:** cand-1 · **Request:** req-4\n\ntext"
    return manifest, note, controls


def _run(tmp_path, manifest, note, controls):
    return gates.gate_controls_conformance(manifest, note, INPUTS, MANDATE,
                                           controls, tmp_path)


class TestControlsConformanceGate:
    def test_in_gate_order_before_nothing_else_changes(self):
        assert gates.GATE_ORDER[-1] == "controls_conformance"

    def test_passes_on_a_conforming_candidate(self, tmp_path):
        assert _run(tmp_path, *_good(tmp_path)) == (True, [])

    def test_no_controls_supplied_passes(self, tmp_path):
        manifest, note, _ = _good(tmp_path)
        assert _run(tmp_path, manifest, note, None) == (True, [])

    def test_failures_are_system_owned(self, tmp_path):
        manifest, note, controls = _good(tmp_path)
        manifest["cutoff_date"] = "2027-01-01"                      # header
        controls["expected_prompt_versions"]["writer"] = "1.4"      # drift
        controls["tokens_by_role"]["writer"] = 5000                 # budget
        (tmp_path / "rounds/round-001/generator/state.json").write_text("{}")  # scope
        controls["artifact_headers"]["facts.json"]["candidate_id"] = "cand-9"
        ok, findings = _run(tmp_path, manifest, note, controls)
        assert not ok
        keys = {f["dedup_key"] for f in findings}
        assert "header:candidate-manifest.json:cutoff_date" in keys
        assert "pin:prompt:writer" in keys
        assert "budget:tokens:writer" in keys
        assert any(k.startswith("scope:") and k.endswith("state.json") for k in keys)
        assert "header:facts.json:candidate_id" in keys
        assert all(f["owner"] == "system" and f["severity"] == "major"
                   for f in findings)

    def test_path_outside_generator_scope_fails(self, tmp_path):
        manifest, note, controls = _good(tmp_path)
        manifest["reports"].append({"artifact": "state.json"})
        ok, findings = _run(tmp_path, manifest, note, controls)
        assert not ok and findings[0]["dedup_key"] == "scope:state.json"

    def test_missing_note_header_fails(self, tmp_path):
        manifest, _, controls = _good(tmp_path)
        ok, findings = _run(tmp_path, manifest, "no header", controls)
        assert {f["dedup_key"] for f in findings} >= {
            "header:report.md:candidate_id", "header:report.md:request_id"}


class TestCards:
    def test_critic_cards_are_v12_and_example_version_matches(self):
        root = pa.default_root()
        for role in ("critic_c1", "critic_c2", "critic_c3", "critic_c4"):
            text = (root / "prompts" / f"{role}.md").read_text("utf-8")
            assert pa.parse_role_card(root / "prompts" / f"{role}.md")["version"] == "1.2"
            assert set(re.findall(r'"prompt_version":"([^"]+)"', text)) == {"1.2"}
            assert "`owner: system`" in text and "owner (optional)" in text

    def test_writer_example_version_equals_card_version(self):
        root = pa.default_root()
        text = (root / "prompts" / "writer.md").read_text("utf-8")
        ver = pa.parse_role_card(root / "prompts" / "writer.md")["version"]
        assert set(re.findall(r'prompt_version\("([^"]+)"\)', text)) == {ver}
        assert set(re.findall(r'"prompt_version":"([^"]+)"', text)) == {ver}


class TestOrphanFindings:
    def test_a_finding_with_no_critic_source_goes_to_c1_only(self, tmp_path):
        orch, *_ = build_env(tmp_path)
        orch.ledger().open_finding({"severity": "major", "summary": "seeded",
                                    "affected_artifacts": ["report.md"],
                                    "dedup_key": "demo"}, round=1)
        assert len(orch._area_carried("critic_c1")) == 1
        assert orch._area_carried("critic_c2") == []
