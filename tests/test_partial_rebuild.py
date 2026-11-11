"""Partial rebuild (diagnosis 3.6): earliest owned stage, carried artifacts."""

from __future__ import annotations

import json

import pytest

import casekit
from coverageflow import _dispatch_test_stub as _stub
from coverageflow import ledger, render_note, statestore
from tests.test_orchestrator import (_critic_findings, _writer_output,
                                     build_env, load_state)
from tests.test_owners import _events, _finding, _packet  # noqa


# --------------------------------------------------------- partial rebuild
class TestPartialRebuild:
    def _run(self, tmp_path, artifacts):
        writer_alt = {"__stale": False, "role": "writer",
                      "report_md": _stub.ALT_REPORT_MD,
                      "conformance": "schema 1.0", "prompt_version": "1.0"}
        scripts = {"critic_c1": [None, _finding(artifacts)],
                   "writer": [_writer_output(_stub.DEFAULT_REPORT_MD), writer_alt]}
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        return orch, dispatch, case_dir

    def test_a_writer_only_round_does_not_dispatch_the_model(self, tmp_path):
        orch, dispatch, case_dir = self._run(tmp_path, ["report.md"])
        assert load_state(case_dir)["budget"]["used"]["rounds"] == 2
        assert len(dispatch.calls_of("writer")) == 2
        assert len(dispatch.calls_of("model")) == 1
        assert len(dispatch.calls_of("extractor")) == 1
        reused = _events(case_dir, "MODEL_REUSED")
        assert reused and reused[0]["detail"]["from_round"] == 1

    def test_the_forecast_is_carried_unchanged(self, tmp_path):
        orch, dispatch, case_dir = self._run(tmp_path, ["report.md"])
        gen = lambda n, f: statestore.read_json(
            case_dir / "rounds" / f"round-{n:03d}" / "generator" / f)
        a, b = gen(1, "forecast_assumptions.json"), gen(3, "forecast_assumptions.json")
        for k in ("request_id", "candidate_id"):
            a.pop(k), b.pop(k)
        assert a == b
        # the engines are deterministic: same inputs, same numbers
        assert gen(1, "valuation.json")["dcf"] == gen(3, "valuation.json")["dcf"]
        assert gen(1, "model.json")["series"] == gen(3, "model.json")["series"]

    def test_scope_is_recorded_in_packet_trace_and_manifest(self, tmp_path):
        orch, dispatch, case_dir = self._run(tmp_path, ["report.md"])
        packet = _packet(case_dir)
        assert packet["rebuild_from"] == "writer"
        assert "partial re-build from the writer stage" in packet["delta_scope"]
        issued = _events(case_dir, "REPAIR_PACKET_ISSUED")[0]
        assert issued["detail"]["rebuild_from"] == "writer"
        starts = [e for e in _events(case_dir, "STATE_TRANSITION")
                  if "rebuild_scope" in (e.get("detail") or {})]
        assert starts[-1]["detail"]["rebuild_from"] == "writer"
        manifest = statestore.read_json(
            case_dir / "rounds" / "round-003" / "generator" / "candidate-manifest.json")
        assert "Rebuild scope: partial rebuild from the writer stage" in \
            manifest["scope_statement"]
        assert "model_engine and valuation_engine re-run" in manifest["scope_statement"]

    def test_a_model_owned_finding_reruns_model_and_carries_facts(self, tmp_path):
        orch, dispatch, case_dir = self._run(tmp_path, ["forecast_assumptions.json"])
        assert len(dispatch.calls_of("model")) == 2
        assert len(dispatch.calls_of("extractor")) == 1
        assert _events(case_dir, "EXTRACTOR_REUSED")
        assert not _events(case_dir, "MODEL_REUSED")
        packet = _packet(case_dir)
        assert packet["rebuild_from"] == "model"

    def test_an_extractor_owned_finding_reruns_the_extractor(self, tmp_path):
        orch, dispatch, case_dir = self._run(tmp_path, ["facts.json"])
        assert len(dispatch.calls_of("extractor")) == 2
        assert len(dispatch.calls_of("model")) == 2

    def test_the_earliest_owner_sets_the_scope(self, tmp_path):
        scripts = {"critic_c1": [None, _finding(["report.md"], summary="a")],
                   "critic_c2": [None, dict(_finding(["forecast_assumptions.json"],
                                                     summary="b"), role="critic_c2")]}
        orch, dispatch, clk, case_dir = build_env(tmp_path, scripts=scripts)
        orch.run()
        packet = _packet(case_dir)
        assert packet["rebuild_from"] == "model"
