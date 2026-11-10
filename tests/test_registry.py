"""Tests for the version registry (owner: stream A2).

Locked rules (design-decisions section 5 + contracts section 9):
  - versions/registry.json holds version entries + an active pointer;
  - only human-APPROVED versions can become active (pointer swap);
  - rejected/deferred proposals retain the current active version;
  - rollback restores the prior pointer, records rollback_to, and keeps
    existing cases pinned to their ORIGINAL version (their state's
    config_versions is never rewritten);
  - a new case created after a rollback verifies the rolled-back config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import casekit
from coverageflow import registry as reg


@pytest.fixture
def regpath(tmp_path) -> Path:
    return tmp_path / "versions" / "registry.json"


def sample_proposal(version="sys-1.1", parent="sys-1.0", scope="prompt card extractor",
                    diff_files=("prompts/extractor.md",)):
    return {
        "version": version, "parent": parent, "scope": scope,
        "proposal": {"author": "evo-change-02", "diff_files": list(diff_files),
                     "diff_sha256": "a" * 64},
        "tested": {"model": "1.0",
                   "prompt_cards": {"ingest": "1.0", "extractor": "1.1",
                                    "model": "1.0", "writer": "1.0",
                                    "critics": ["1.0"]},
                   "engines": "1.0", "cases": ["dev-1", "dev-2"]},
    }


class TestRegistryBasics:
    def test_bootstrap_creates_baseline_active_version(self, regpath):
        r = reg.VersionRegistry(regpath)
        assert r.active() == "sys-1.0"
        entry = r.get("sys-1.0")
        assert entry["parent"] is None
        assert entry["scope"] == "baseline"

    def test_register_appends_unapproved_entry(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        assert r.active() == "sys-1.0", "unapproved proposal must not activate"
        entry = r.get("sys-1.1")
        assert entry["approval"] is None
        assert entry["rollback_to"] is None
        assert entry["parent"] == "sys-1.0"

    def test_register_rejects_duplicate_version(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        with pytest.raises(ValueError):
            r.register(sample_proposal())

    def test_register_rejects_unknown_parent(self, regpath):
        with pytest.raises(ValueError):
            reg.VersionRegistry(regpath).register(sample_proposal(parent="sys-9.9"))

    def test_record_review_and_regressions(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        r.record_review("sys-1.1", fresh_critic_findings=[], regressions_all_pass=True,
                        identical_outputs_proof={"dev-1": "dede", "dev-2": "c0de"})
        entry = r.get("sys-1.1")
        assert entry["review"]["regressions_all_pass"] is True
        assert entry["review"]["identical_outputs_proof"]["dev-2"] == "c0de"


class TestPointerSwap:
    def test_approval_swaps_active_pointer(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        r.record_review("sys-1.1", fresh_critic_findings=[],
                        regressions_all_pass=True, identical_outputs_proof={})
        r.approve("sys-1.1", by="Gaetano", decision="APPROVED")
        assert r.active() == "sys-1.1"
        assert r.get("sys-1.1")["approval"]["decision"] == "APPROVED"
        assert r.get("sys-1.1")["approval"]["by"] == "Gaetano"

    def test_rejected_decision_retains_active(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        r.approve("sys-1.1", by="Gaetano", decision="REJECTED")
        assert r.active() == "sys-1.0"
        assert r.get("sys-1.1")["approval"]["decision"] == "REJECTED"

    def test_deferred_decision_retains_active(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        r.approve("sys-1.1", by="Gaetano", decision="DEFERRED")
        assert r.active() == "sys-1.0"

    def test_approve_requires_registered_entry(self, regpath):
        r = reg.VersionRegistry(regpath)
        with pytest.raises(LookupError):
            r.approve("sys-2.0", by="x", decision="APPROVED")

    def test_review_must_pass_before_approval(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        with pytest.raises(RuntimeError):
            r.approve("sys-1.1", by="x", decision="APPROVED")


class TestRollback:
    def test_rollback_restores_pointer_and_records_it(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        r.record_review("sys-1.1", fresh_critic_findings=[],
                         regressions_all_pass=True, identical_outputs_proof={})
        r.approve("sys-1.1", by="Gaetano", decision="APPROVED")
        assert r.active() == "sys-1.1"
        r.rollback("sys-1.0", by="Gaetano", reason="critic found regression in dev-3")
        assert r.active() == "sys-1.0", "pointer must be restored to the prior version"
        assert r.get("sys-1.1")["rollback_to"] == "sys-1.0"
        # entries are kept (audit), nothing is deleted
        assert r.get("sys-1.1") is not None

    def test_rollback_only_from_active(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        r.record_review("sys-1.1", fresh_critic_findings=[],
                        regressions_all_pass=True, identical_outputs_proof={})
        with pytest.raises(RuntimeError):
            r.rollback("sys-1.0", by="x", reason="not active")


class TestCasePinning:
    def _make_state(self, tmp_path, config: dict) -> Path:
        st = {
            "case_id": "intc-x", "status": "CLOSED",
            "pending": {"role": "approval", "request_id": None, "round": 1,
                        "retry_count": 0, "case_clock_started": None,
                        "deadline": None, "stage": None},
            "candidate": {"candidate_id": "c", "clean_review_rounds": 2,
                          "built_round": 1},
            "budget": {"rounds_max": 4},
            "inputs_manifest_ref": "inputs/manifest.json",
            "config_versions": config,
            "checkpoints": [], "reason": None, "updated_at": None,
        }
        case_dir = Path(tmp_path) / "cases" / "intc-x"
        case_dir.mkdir(parents=True)
        from coverageflow import statestore
        statestore.write_state(case_dir, st)
        return case_dir

    def test_existing_cases_stay_pinned_to_original_version(self, regpath, tmp_path):
        r = reg.VersionRegistry(regpath)
        v1_cfg = r.config_versions()
        case_dir = self._make_state(tmp_path, v1_cfg)

        r.register(sample_proposal())
        r.record_review("sys-1.1", fresh_critic_findings=[],
                        regressions_all_pass=True, identical_outputs_proof={})
        r.approve("sys-1.1", by="Gaetano", decision="APPROVED")
        r.rollback("sys-1.0", by="Gaetano", reason="post-approval issue")

        # the existing case keeps its ORIGINAL config versions
        after = json.loads((case_dir / "state.json").read_text())["config_versions"]
        assert after == v1_cfg
        assert r.pinned_config_for_case(case_dir) == v1_cfg
        # a NEW case after the rollback verifies rolled-back behavior
        assert r.config_versions()["prompt_cards"]["extractor"] == "1.0"

    def test_config_versions_of_active(self, regpath):
        r = reg.VersionRegistry(regpath)
        assert r.config_versions()[
            "prompt_cards"]["extractor"] == "1.0"   # baseline
        r.register(sample_proposal())
        r.record_review("sys-1.1", fresh_critic_findings=[],
                        regressions_all_pass=True, identical_outputs_proof={})
        r.approve("sys-1.1", by="Gaetano", decision="APPROVED")
        assert r.active() == "sys-1.1"
        assert r.config_versions()["prompt_cards"]["extractor"] == "1.1"

    def test_pinned_version_for_case_reads_state(self, regpath, tmp_path):
        case_dir = self._make_state(tmp_path, {"marker": "cfg-vX"})
        r = reg.VersionRegistry(regpath)
        st_cfg = r.pinned_config_for_case(case_dir)
        assert st_cfg == {"marker": "cfg-vX"}


class TestRegistryIntegrity:
    def test_corrupt_registry_is_not_silently_reset(self, regpath):
        regpath.parent.mkdir(parents=True)
        regpath.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="refusing to reset"):
            reg.VersionRegistry(regpath)
        # the file is left exactly as it was for a human to inspect
        assert regpath.read_text(encoding="utf-8") == "{not json"

    def test_registry_without_versions_is_not_silently_reset(self, regpath):
        regpath.parent.mkdir(parents=True)
        regpath.write_text(json.dumps({"active": "sys-1.0", "versions": []}),
                           encoding="utf-8")
        with pytest.raises(ValueError, match="no version entries"):
            reg.VersionRegistry(regpath)

    def test_registry_without_active_pointer_is_rejected(self, regpath):
        regpath.parent.mkdir(parents=True)
        regpath.write_text(json.dumps({"versions": [{"version": "sys-1.0"}]}),
                           encoding="utf-8")
        with pytest.raises(ValueError, match="no active pointer"):
            reg.VersionRegistry(regpath)

    def test_register_rejects_null_version(self, regpath):
        r = reg.VersionRegistry(regpath)
        with pytest.raises(ValueError, match="non-empty string"):
            r.register(sample_proposal(version=None))

    def test_cannot_reject_the_active_version(self, regpath):
        r = reg.VersionRegistry(regpath)
        with pytest.raises(RuntimeError, match="active"):
            r.approve("sys-1.0", by="x", decision="REJECTED")
        assert r.active() == "sys-1.0"

    def test_rollback_records_who_and_why_on_the_entry(self, regpath):
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())
        r.record_review("sys-1.1", fresh_critic_findings=[],
                        regressions_all_pass=True, identical_outputs_proof={})
        r.approve("sys-1.1", by="Gaetano", decision="APPROVED")
        r.rollback("sys-1.0", by="Gaetano", reason="dev-3 regression")
        audit = r.get("sys-1.1")["rollback"]
        assert audit["to"] == "sys-1.0"
        assert audit["by"] == "Gaetano"
        assert audit["reason"] == "dev-3 regression"
        assert audit["ts"]
        # and it survives a reload from disk
        assert reg.VersionRegistry(regpath).get("sys-1.1")["rollback"]["by"] == "Gaetano"

    def test_config_versions_is_a_deep_copy(self, regpath):
        r = reg.VersionRegistry(regpath)
        cfg = r.config_versions()
        cfg["prompt_cards"]["critics"].append("9.9")
        cfg["prompt_cards"]["ingest"] = "9.9"
        fresh = r.config_versions()
        assert fresh["prompt_cards"]["critics"] == ["1.0"]
        assert fresh["prompt_cards"]["ingest"] == "1.0"

    def test_config_versions_inherits_from_the_parent_chain(self, regpath):
        """A version that changes one card inherits the rest from its parent."""
        r = reg.VersionRegistry(regpath)
        r.register(sample_proposal())          # sys-1.1, extractor 1.1
        r.record_review("sys-1.1", fresh_critic_findings=[],
                        regressions_all_pass=True, identical_outputs_proof={})
        r.approve("sys-1.1", by="Gaetano", decision="APPROVED")
        # a child that only pins the writer card
        child = sample_proposal(version="sys-1.2", parent="sys-1.1")
        child["tested"] = {"prompt_cards": {"writer": "1.2"}}
        r.register(child)
        cfg = r.config_versions("sys-1.2")
        assert cfg["prompt_cards"]["writer"] == "1.2"
        # inherited from sys-1.1, not reset to the baseline
        assert cfg["prompt_cards"]["extractor"] == "1.1"
        assert cfg["prompt_cards"]["ingest"] == "1.0"
        assert cfg["engines"] == "1.0"   # inherited from sys-1.1

    def test_config_versions_rejects_an_unknown_version(self, regpath):
        r = reg.VersionRegistry(regpath)
        with pytest.raises(LookupError):
            r.config_versions("sys-9.9")
