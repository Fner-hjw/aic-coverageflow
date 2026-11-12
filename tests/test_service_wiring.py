"""UI service wiring toggle: COVERAGEFLOW_WORKSPACE selects controller mode.

Integration stream (A6). The demo service (default) and the real-controller
service (env) share the same function names, signatures and return shapes -
these tests pin the toggle:

  - default (env unset): deterministic demo store, mode "stub-service";
  - COVERAGEFLOW_WORKSPACE=<real workspace>: every operation routes through
    the controller package (Orchestrator / statestore / ledger / backends /
    bundle), with the offline demo store as a clean fallback where the real
    path cannot run offline (EDGAR).

All tests stay offline (no LLM, no network): the controller-mode case store
is built with the controller CLI itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coverageflow import cli, statestore
from ui import service
from ui.api import create_app

import casekit

WIZARD = {
    "ticker": "AMDX", "cik": "0001234567",
    "trigger_document": "x-10q-2026q3", "cutoff_date": "2026-10-30",
    "period": "fy2026q3",
    "report_policy": {"note_type": "quarterly_update",
                      "length_words": [300, 1600],
                      "sections": ["Summary", "Results", "Valuation",
                                   "Risks"]},
    "assumptions": {
        "wacc": 0.10, "terminal_growth": 0.02,
        "exit_multiple_ebitda": 14.0,
        "forecast_bounds": {"segments": {"DCAI": [-0.05, 0.25]},
                            "margin_floor_gross": 0.30,
                            "margin_ceiling_gross": 0.55}},
}


@pytest.fixture()
def real_ws(tmp_path, monkeypatch):
    """A small real controller workspace wired into the service via env."""
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = cli.main(["--workspace", str(ws), "case", "new", "--ticker", "INTC",
                   "--trigger-doc", "intc-10q-2026q3",
                   "--cutoff", "2026-10-30"])
    assert rc == 0
    # a case is only runnable once its source filing is in the inputs dir
    casekit.materialize_inputs(ws, "intc-fy2026q3-20261030")
    monkeypatch.setenv("COVERAGEFLOW_WORKSPACE", str(ws))
    yield ws


@pytest.fixture()
def demo(monkeypatch):
    monkeypatch.delenv("COVERAGEFLOW_WORKSPACE", raising=False)
    return TestClient(create_app())


@pytest.fixture()
def controller(real_ws):
    return TestClient(create_app())


class TestDemoModeDefault:
    def test_env_unset_is_demo_mode(self, demo):
        body = demo.get("/api/health").json()
        assert body["mode"] == "stub-service"
        # the demo store is module-global; other suite tests may have added
        # demo cases - the three fixture fixtures are the floor
        assert body["cases"] >= 3

    def test_demo_store_serves_the_three_demo_cases(self, demo):
        ids = {c["case_id"] for c in demo.get("/api/cases").json()["cases"]}
        assert {"intc-fy2026q3-20261030", "intc-fy2026q4-20270129",
                "amd-fy2026q3-20261029"} <= ids

    def test_case_list_exposes_clock_start_from_case_state(self, demo):
        rows = demo.get("/api/cases").json()["cases"]
        row = next(r for r in rows if r["case_id"] == "intc-fy2026q3-20261030")
        state = demo.get(f"/api/cases/{row['case_id']}").json()["state"]
        live = state["status"] == "RUNNING"
        assert row["case_clock_started"] == (
            state["pending"]["case_clock_started"] if live else None)

    def test_invalid_workspace_env_falls_back_to_demo(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("COVERAGEFLOW_WORKSPACE",
                           str(tmp_path / "does-not-exist"))
        body = TestClient(create_app()).get("/api/health").json()
        assert body["mode"] == "stub-service"


class TestControllerMode:
    def test_health_reports_controller_mode_and_real_workspace(self, controller):
        body = controller.get("/api/health").json()
        assert body["mode"] == "controller"
        assert body["cases"] == 1

    def test_list_reads_real_state_from_disk(self, controller, real_ws):
        rows = controller.get("/api/cases").json()["cases"]
        assert [r["case_id"] for r in rows] == ["intc-fy2026q3-20261030"]
        state = statestore.load_state(real_ws / "cases" /
                                      "intc-fy2026q3-20261030")
        assert rows[0]["status"] == state["status"]
        assert rows[0]["ticker"] == "INTC"

    def test_controller_list_exposes_active_clock_start(self, controller, real_ws, monkeypatch):
        case_dir = real_ws / "cases" / "intc-fy2026q3-20261030"
        state = statestore.load_state(case_dir)
        state["pending"]["case_clock_started"] = "2026-11-10T12:00:00Z"
        state["status"] = "RUNNING"
        monkeypatch.setattr(service, "_rstate", lambda _: state)
        row = controller.get("/api/cases").json()["cases"][0]
        assert row["case_clock_started"] == "2026-11-10T12:00:00Z"

    def test_backend_choice_survives_a_server_restart(self, controller, real_ws, monkeypatch):
        r = controller.post("/api/backends/select", json={"backend": "replay"})
        assert r.status_code == 200, r.text
        assert (real_ws / ".backend-selection.json").is_file()
        # a restarted server has the in-memory default again
        monkeypatch.setattr(service, "_REAL_SELECTED_BACKEND", "auto")
        assert controller.get("/api/health").json()["active_backend"] == "replay"

    def test_only_a_running_case_has_a_live_clock(self, controller, real_ws, monkeypatch):
        # a stale clock start on a finished case must not keep the timer going
        case_dir = real_ws / "cases" / "intc-fy2026q3-20261030"
        state = statestore.load_state(case_dir)
        state["pending"]["case_clock_started"] = "2026-11-10T12:00:00Z"
        state["status"] = "EXHAUSTED"
        monkeypatch.setattr(service, "_rstate", lambda _: state)
        row = controller.get("/api/cases").json()["cases"][0]
        assert row["case_clock_started"] is None

    def test_create_and_get_case_roundtrip_via_orchestrator(self, controller):
        r = controller.post("/api/cases", json=WIZARD)
        assert r.status_code == 200, r.text
        out = r.json()
        case_id = out["case_id"]
        assert case_id == "amdx-fy2026q3-20261030"
        # duplicate -> 409 (real dir exists)
        assert controller.post("/api/cases", json=WIZARD).status_code == 409
        d = client_case(controller, case_id)
        assert d["state"]["status"] == "CREATED"
        # the created mandate carries the valuation-engine extras
        man = d["mandate"]
        assert {"market", "own_band", "sensitivity"} <= set(
            man["assumptions"])
        assert man["assumptions"]["market"]["price"] is None
        assert len(man["assumptions"]["sensitivity"]["wacc_points"]) == 5
        # artifacts served from the real case dir
        art = controller.get(
            f"/api/cases/{case_id}/artifacts/mandate.json").json()
        assert art["content"]["ticker"] == "AMDX"
        # path traversal is blocked
        rr = client_get_raw(controller,
                            f"/api/cases/{case_id}/artifacts/../../state.json")
        assert rr.status_code in (400, 404)

    def test_trace_cursor_reads_real_trace_jsonl(self, controller):
        cid = "intc-fy2026q3-20261030"
        full = controller.get(f"/api/cases/{cid}/trace").json()
        assert full["count"] >= 1
        assert full["events"][0]["event"] == "CASE_CREATED"
        tail = controller.get(f"/api/cases/{cid}/trace?from={full['count']}").json()
        assert tail["count"] == 0

    def test_replays_empty_and_404_before_any_recording(self, controller):
        cid = "intc-fy2026q3-20261030"
        assert controller.get(f"/api/cases/{cid}/replay").json()["count"] == 0
        assert client_replay(controller, cid, "req-0001").status_code == 404

    def test_demo_verbs_operate_on_the_real_case_directory(
            self, controller):
        cid = "intc-fy2026q3-20261030"
        seed = client_seed(controller, cid).json()
        assert seed["status"] == "open"
        d = client_case(controller, cid)
        assert len(d["findings"]) == 1
        assert d["findings"][0]["status"] == "open"
        # kill-stage writes a torn partial for the pending stage
        kill = client_kill(controller, cid).json()
        assert kill["killed_stage"]
        # perturb: deterministic recompute + cutoff discipline
        pert = client_perturb(controller, cid).json()
        assert pert["deterministic_recompute"] is True
        assert pert["no_post_cutoff_documents"] is True
        assert pert["leakage_detected"] is False

    def test_approval_wrong_state_is_a_conflict(self, controller):
        cid = "intc-fy2026q3-20261030"
        r = controller.post(f"/api/cases/{cid}/approval",
                            json={"decision": "APPROVED", "by": "Supervisor"})
        assert r.status_code == 409  # CREATED, not PENDING_APPROVAL

    def test_backends_manager_uses_real_detection(self, controller):
        body = controller.get("/api/backends").json()
        names = {row["name"] for row in body["backends"]}
        assert names == {"claude", "codex", "kimi", "vibe", "api",
                         "replay", "stub"}
        detected = {row["name"] for row in body["backends"]
                    if row["detected"]}
        # replay is always available (offline); anything else came from
        # coverageflow.backends detection, never from the stale stub flags
        assert {"replay"} <= detected
        r = controller.post("/api/backends/select",
                            json={"backend": "replay"})
        assert r.status_code == 200
        assert r.json()["selected"] == "replay"
        assert [row["name"] for row in r.json()["backends"] if row["selected"]] == ["replay"]
        assert controller.post("/api/backends/select", json={"backend": "stub"}).status_code == 200
        rows = controller.get("/api/backends").json()["backends"]
        assert [row["name"] for row in rows if row["selected"]] == ["stub"]

    def test_api_configuration_is_used_by_dispatch(self, controller, real_ws, monkeypatch):
        monkeypatch.setenv("COVERAGEFLOW_BASE_URL", "https://old.example/v1")
        result = controller.post("/api/backends/api/configure", json={
            "base_url": "https://new.example/v1", "model": "test-model", "api_key": "secret-value"})
        assert result.status_code == 200, result.text
        assert "secret-value" not in result.text
        assert result.json()["selected"] == "api"
        assert (real_ws / ".coverageflow-api.json").stat().st_mode & 0o077 == 0
        dispatch, name = service._rdispatch_for("intc-fy2026q3-20261030", "api")
        assert name == "api"
        assert dispatch._backend._config() == {"base_url": "https://new.example/v1", "model": "test-model", "api_key": "secret-value"}
        result = controller.post("/api/backends/api/configure", json={"base_url": "bad", "model": "x"})
        assert result.status_code == 422

    def test_bundles_list_scans_real_manifests(self, controller):
        ids = {b["bundle_id"] for b in
               controller.get("/api/bundles").json()["bundles"]}
        assert {"intc-2026-10"} <= ids  # repo data/intel-bundle

    def test_edgar_offline_falls_back_with_a_clear_note(self, controller, monkeypatch):
        from coverageflow.bundle import EdgarFetcher, FetchError
        def offline(*args, **kwargs):
            raise FetchError("offline")
        monkeypatch.setattr(EdgarFetcher, "get", offline)
        body = controller.get("/api/edgar/search?ticker=INTC").json()
        assert body["ticker"] == "INTC"
        assert "offline demo index" in body["note"]

    def test_bundle_build_without_accessions_is_rejected(self, controller):
        r = controller.post("/api/bundles", json={
            "cik": "0000050863",
            "docs": ["intc-10q-2026q3", "intc-8k-earnings-2026q3"],
            "name": "wired-bundle"})
        assert r.status_code == 422
        assert "SEC filing details" in r.json()["error"]
        assert not client_bundle(controller, "wired-bundle")

    def test_demo_mode_is_restored_when_env_cleared(self, controller,
                                                   monkeypatch):
        monkeypatch.delenv("COVERAGEFLOW_WORKSPACE", raising=False)
        body = TestClient(create_app()).get("/api/health").json()
        assert body["mode"] == "stub-service"
        assert body["cases"] >= 3


class TestInterruptedRuns:
    """RUNNING is a claim about the state file; the lock is the liveness signal.

    A run that died (crash, restart, or a state file edited outside the
    orchestrator) leaves RUNNING behind with nothing executing. Reporting that
    as "in progress" left a case that looked busy forever: a ticking clock, no
    next action, and an active_runs count that never dropped.
    """

    CID = "intc-fy2026q3-20261030"

    def _set_running(self, real_ws):
        case_dir = real_ws / "cases" / self.CID
        state = statestore.load_state(case_dir)
        state["status"] = "RUNNING"
        statestore.write_state(case_dir, state)
        return case_dir

    def test_running_without_a_live_owner_is_interrupted(self, controller,
                                                         real_ws):
        self._set_running(real_ws)
        row = controller.get("/api/cases").json()["cases"][0]
        assert row["status"] == "RUNNING"
        assert row["interrupted"] is True
        assert client_case(controller, self.CID)["interrupted"] is True
        health = controller.get("/api/health").json()
        assert health["active_runs"] == 0
        assert health["interrupted_runs"] == 1

    def test_a_live_lock_holder_keeps_the_run_in_progress(self, controller,
                                                          real_ws):
        case_dir = self._set_running(real_ws)
        lock = statestore.acquire_case_lock(case_dir)
        try:
            row = controller.get("/api/cases").json()["cases"][0]
            assert row["interrupted"] is False
            health = controller.get("/api/health").json()
            assert health["active_runs"] == 1
            assert health["interrupted_runs"] == 0
        finally:
            statestore.release_case_lock(lock)


class TestControllerModeRunOverReplay:
    """The full loop through the UI, controller mode, offline replay.

    1. record a scripted rehearsal with the REAL dispatch (same backend seam
       the CLI uses) + real engines on the case in the real workspace;
    2. reset the case to CREATED (fresh dir, recordings preserved);
    3. POST /api/cases/{id}/run {backend: "replay"} drives the controller
       loop from the HTTP API - the UI action routes through the
       Orchestrator, never around it.
    """

    def test_api_run_reaches_pending_approval(self, real_ws, controller):
        import shutil
        from coverageflow.backends.replay_backends import RecordingBackend
        from coverageflow.orchestrator import Orchestrator
        from tests.test_cli_real_dispatch import (CannedBackend,
                                                  real_dispatch_over)

        case_id = "intc-fy2026q3-20261030"
        case_dir = real_ws / "cases" / case_id

        # --- 1. recording run (real dispatch + real engines) --------------
        record_dir = case_dir / "replay"
        backend = RecordingBackend(CannedBackend(), record_dir=str(record_dir))
        orch = Orchestrator(
            case_dir, dispatch=real_dispatch_over(backend, real_ws),
            gate_engine=cli.load_gate_engine(stub_engines=False,
                                             workspace=real_ws))
        assert orch.run().get("status") == "PENDING_APPROVAL"
        kept = sorted(p.name for p in record_dir.glob("*.json"))
        assert len(kept) >= 10

        # --- 2. reset the case to CREATED, keep the recordings ------------
        copies = {n: (record_dir / n).read_bytes() for n in kept}
        shutil.rmtree(case_dir)
        rc = cli.main(["--workspace", str(real_ws), "case", "new",
                       "--ticker", "INTC",
                       "--trigger-doc", "intc-10q-2026q3",
                       "--cutoff", "2026-10-30"])
        assert rc == 0
        casekit.materialize_inputs(real_ws, case_id)
        record_dir.mkdir(parents=True)
        for n, blob in copies.items():
            (record_dir / n).write_bytes(blob)
        assert statestore.load_state(case_dir)["status"] == "CREATED"

        # --- 3. run through the API --------------------------------------
        r = controller.post(f"/api/cases/{case_id}/run",
                            json={"backend": "replay", "replay": True})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] is True
        assert out["backend"] == "replay"
        assert out["status"] == "PENDING_APPROVAL"
        events = statestore.read_trace(case_dir)
        dispatched = [e for e in events if e["event"] == "AGENT_RESULT"]
        assert dispatched and all(e["backend"] == "replay" for e in dispatched)


def client_case(c, cid):
    return c.get(f"/api/cases/{cid}").json()


def client_get_raw(c, path):
    return c.get(path)


def client_replay(c, cid, rid):
    return c.get(f"/api/cases/{cid}/replay/{rid}")


def client_seed(c, cid):
    return c.post(f"/api/cases/{cid}/demo/seed-finding")


def client_kill(c, cid, stage=None):
    return c.post(f"/api/cases/{cid}/demo/kill-stage",
                  json={"stage": stage} if stage else {})


def client_perturb(c, cid):
    return c.post(f"/api/cases/{cid}/demo/perturb")


def client_bundle(c, bundle_id):
    return any(b["bundle_id"] == bundle_id for b in
               c.get("/api/bundles").json()["bundles"])


class TestControllerModeRunOverStub:
    """Selected 'stub' backend drives UI runs offline, no recordings needed."""

    def test_selected_stub_backend_drives_ui_run(self, real_ws, controller):
        case_id = "intc-fy2026q3-20261030"
        case_dir = real_ws / "cases" / case_id

        sel = controller.post("/api/backends/select", json={"backend": "stub"})
        assert sel.status_code == 200, sel.text

        r = controller.post(f"/api/cases/{case_id}/run",
                            json={"backend": "auto"})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] is True
        assert out["status"] == "PENDING_APPROVAL"
        assert out["backend"] == "stub (offline)"
        events = statestore.read_trace(case_dir)
        dispatched = [e for e in events if e["event"] == "AGENT_RESULT"]
        assert dispatched and "stub" in dispatched[0]["backend"]


class TestReportAndApprovalAtTheHumanGate:
    """The reviewer must be able to read and sign off what the UI shows.

    `report/v<N>-<candidate8>.md` is the *published* artifact and only exists
    after approval, so before the fix the Approval tab had no note to render
    and the evidence panel resolved only extracted facts -- every computed
    number in the note showed "not resolved".
    """

    @pytest.fixture()
    def pending(self, real_ws, controller):
        case_id = "intc-fy2026q3-20261030"
        assert controller.post("/api/backends/select",
                               json={"backend": "stub"}).status_code == 200
        r = controller.post(f"/api/cases/{case_id}/run", json={"backend": "auto"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "PENDING_APPROVAL"
        return case_id

    def test_report_is_readable_before_any_version_is_published(
            self, real_ws, controller, pending):
        case_dir = real_ws / "cases" / pending
        assert not list((case_dir / "report").glob("v*.md")) if (
            case_dir / "report").is_dir() else True
        body = controller.get(f"/api/cases/{pending}/report").json()
        assert body["version"] == "draft"
        assert body["is_draft"] is True
        assert body["markdown"].strip()
        assert body["word_count"] > 0

    def test_evidence_resolves_derived_and_computed_markers(
            self, controller, pending):
        body = controller.get(f"/api/cases/{pending}/report").json()
        kinds = {e["citation"].get("kind") for e in body["evidence"]}
        assert "filing" in kinds
        # the note quotes margins, segment shares and DCF outputs; those are
        # not extracted facts and used to come back unresolved
        assert kinds & {"derived", "computed"}
        assert "unresolved" not in kinds

    def test_acknowledge_findings_must_be_a_list(self, controller, pending):
        r = controller.post(f"/api/cases/{pending}/approval",
                            json={"decision": "APPROVED", "by": "Reviewer",
                                  "acknowledge_findings": "fnd-101"})
        assert r.status_code == 422, r.text
        assert "acknowledge_findings" in r.text


class TestEvidenceResolution:
    """`_revidence` resolves the three kinds of marker the note can carry."""

    @pytest.fixture()
    def case_dir(self, tmp_path):
        cd = tmp_path / "case"
        gen = cd / "rounds" / "round-001" / "generator"
        gen.mkdir(parents=True)
        (cd / "inputs").mkdir()
        (cd / "inputs" / "manifest.json").write_text(json.dumps(
            {"documents": [{"doc_id": "intc-10q-2026q3", "sha256": "abc"}]}))
        (gen / "facts.json").write_text(json.dumps({"facts": [
            {"field": "income_statement.revenue", "value": 100.0,
             "citation": {"doc_id": "intc-10q-2026q3", "slice_id": "s1",
                          "anchor": "p.1"}},
            {"field": "income_statement.gross_profit", "value": 40.0,
             "citation": {"doc_id": "intc-10q-2026q3", "slice_id": "s2",
                          "anchor": "p.2"}}]}))
        (gen / "model.json").write_text(json.dumps({
            "derivations": {
                "derived.gross_margin":
                    "income_statement.gross_profit / income_statement.revenue"},
            "series": {"segments.ccg.revenue": {"2026-Q3": 8097.0,
                                                "yoy": {}, "qoq": {}}}}))
        (gen / "valuation.json").write_text(json.dumps({
            "dcf": {"base": {"revenue": 66052.0}},
            "sensitivity": {"grid": [[1.0, 2.0]]}}))
        return cd

    def test_filing_marker_carries_the_document(self, case_dir):
        ev = service._revidence(case_dir, ["income_statement.revenue"])[0]
        assert ev["citation"]["kind"] == "filing"
        assert ev["citation"]["doc_id"] == "intc-10q-2026q3"
        assert ev["citation"]["doc_sha256"] == "abc"

    def test_derived_marker_carries_the_formula_and_its_inputs(self, case_dir):
        ev = service._revidence(case_dir, ["derived.gross_margin"])[0]
        cit = ev["citation"]
        assert cit["kind"] == "derived"
        assert cit["source_artifact"] == "model.json"
        assert cit["formula"].startswith("income_statement.gross_profit")
        assert [i["field"] for i in cit["inputs"]] == [
            "income_statement.gross_profit", "income_statement.revenue"]
        assert all(i["citation"]["doc_id"] for i in cit["inputs"])

    def test_series_marker_carries_the_latest_period(self, case_dir):
        ev = service._revidence(case_dir, ["segments.ccg.revenue"])[0]
        assert ev["citation"]["kind"] == "derived"
        assert ev["citation"]["period"] == "2026-Q3"
        assert ev["citation"]["value"] == 8097.0

    def test_computed_marker_carries_the_valuation_path(self, case_dir):
        ev = service._revidence(case_dir, ["dcf.base.revenue"])[0]
        assert ev["citation"]["kind"] == "computed"
        assert ev["citation"]["source_artifact"] == "valuation.json"
        assert ev["citation"]["value"] == 66052.0

    def test_a_whole_table_still_resolves(self, case_dir):
        ev = service._revidence(case_dir, ["sensitivity.grid"])[0]
        assert ev["citation"]["kind"] == "computed"
        assert "1.0" in ev["citation"]["value"]

    def test_an_unknown_marker_is_reported_as_unresolved(self, case_dir):
        ev = service._revidence(case_dir, ["nope.not_a_field"])[0]
        assert ev["citation"]["kind"] == "unresolved"


class TestDeleteCase:
    CID = "intc-fy2026q3-20261030"

    def test_delete_needs_the_typed_case_id(self, controller, real_ws):
        r = controller.delete(f"/api/cases/{self.CID}?confirm=wrong")
        assert r.status_code == 422
        assert (real_ws / "cases" / self.CID).is_dir()

    def test_delete_moves_the_case_to_deleted(self, controller, real_ws):
        r = controller.delete(f"/api/cases/{self.CID}?confirm={self.CID}")
        assert r.status_code == 200, r.text
        assert not (real_ws / "cases" / self.CID).exists()
        moved = list((real_ws / "deleted").glob(f"{self.CID}-*"))
        assert len(moved) == 1 and (moved[0] / "state.json").is_file()
        assert controller.get(f"/api/cases/{self.CID}").status_code == 404

    def test_delete_refuses_a_running_case(self, controller, real_ws, monkeypatch):
        from ui import service
        monkeypatch.setattr(service._real_statestore, "run_owner", lambda cd: 4242)
        r = controller.delete(f"/api/cases/{self.CID}?confirm={self.CID}")
        assert r.status_code == 409
        assert (real_ws / "cases" / self.CID).is_dir()

    def test_delete_rejects_path_traversal(self, controller):
        r = controller.delete("/api/cases/..%2F..%2Fetc?confirm=..%2F..%2Fetc")
        assert r.status_code in (404, 422)
