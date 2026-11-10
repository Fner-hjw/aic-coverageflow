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

    def test_bundles_list_scans_real_manifests(self, controller):
        ids = {b["bundle_id"] for b in
               controller.get("/api/bundles").json()["bundles"]}
        assert {"intc-2026-10"} <= ids  # repo data/intel-bundle

    def test_edgar_offline_falls_back_with_a_clear_note(self, controller):
        body = controller.get("/api/edgar/search?ticker=INTC").json()
        assert body["ticker"] == "INTC"
        assert ("offline fallback" in body["note"]
                or "frozen, hashed snapshots" in body["note"])

    def test_bundle_build_without_accesssions_uses_the_offline_fallback(
            self, controller):
        r = controller.post("/api/bundles", json={
            "cik": "0000050863",
            "docs": ["intc-10q-2026q3", "intc-8k-earnings-2026q3"],
            "name": "wired-bundle"})
        assert r.status_code == 200
        man = r.json()
        assert man["bundle_id"] == "wired-bundle"
        assert man["bundle_sha256"]
        assert client_bundle(controller, "wired-bundle")

    def test_demo_mode_is_restored_when_env_cleared(self, controller,
                                                   monkeypatch):
        monkeypatch.delenv("COVERAGEFLOW_WORKSPACE", raising=False)
        body = TestClient(create_app()).get("/api/health").json()
        assert body["mode"] == "stub-service"
        assert body["cases"] >= 3


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
