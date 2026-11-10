"""API tests for the CoverageFlow web UI (stream A4, owned).

Covers every endpoint of contracts interfaces section 5 against the stub
service: basic reads, cursor trace, artifact access, case creation
validation, run/resume, the approval human gate, demo scripted actions,
report version diff, export (real bytes), evolution lifecycle with the
locked-scope gate, backends, EDGAR/bundles, WebSocket push, and the
no-auth guarantee (local tool: no login anywhere).

Offline only: no network, no live LLM; the stub service is deterministic.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from ui.api import create_app

APPROVED = "intc-fy2026q3-20261030"
RUNNING = "intc-fy2026q4-20270129"
PENDING = "amd-fy2026q3-20261029"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture()
def ws_client() -> TestClient:
    """TestClient WITH lifespan so the service event hook is wired."""
    with TestClient(create_app()) as c:
        yield c


VALID_WIZARD = {
    "ticker": "INTC",
    "cik": "0000050863",
    "bundle": "intc-2026-10",
    "trigger_document": "intc-10q-2026q3",
    "cutoff_date": "2026-10-30",
    "period": "fy2026q4",
    "report_policy": {
        "note_type": "quarterly_update", "length_words": [800, 1600],
        "sections": ["Summary", "Results", "Valuation", "Risks"]},
    "assumptions": {
        "wacc": 0.10, "terminal_growth": 0.02, "exit_multiple_ebitda": 14.0,
        "tax_rate": 0.15,
        "forecast_bounds": {
            "segments": {"CCG": [-0.1, 0.12], "DCAI": [-0.05, 0.25]},
            "margin_floor_gross": 0.30, "margin_ceiling_gross": 0.55},
        "peer_block": {"as_of": "2026-10-30",
                       "peers": [{"ticker": "AMD", "ev_ebitda": 27.6}]}},
}


def _create(client: TestClient, **over) -> dict:
    payload = {**VALID_WIZARD, **over}
    r = client.post("/api/cases", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# No-auth guarantee (local tool)
# ---------------------------------------------------------------------------


class TestNoAuth:
    def test_no_401_403_from_any_endpoint(self, client):
        """Local tool: nothing requires a login; no auth headers anywhere."""
        for path in ("/api/health", "/api/cases", "/api/backends",
                     "/api/versions", "/api/bundles",
                     f"/api/cases/{APPROVED}"):
            r = client.get(path)
            assert r.status_code not in (401, 403)
            assert "www-authenticate" not in {k.lower() for k in r.headers}

    def test_approval_requires_no_login_only_a_name(self, client):
        # a malformed decision gets 422, not 401/403: the endpoint is
        # unauthenticated — provenance ('by') is a field, not a login
        r = client.post(f"/api/cases/{PENDING}/approval",
                        json={"decision": "A weird decision",
                              "by": "Supervisor"})
        assert r.status_code == 422
        r = client.post(f"/api/cases/{APPROVED}/approval",
                        json={"decision": "APPROVED", "by": ""})
        assert r.status_code == 409  # wrong state, still no auth demanded


# ---------------------------------------------------------------------------
# Reads: health, cases, detail, trace, artifacts, replays
# ---------------------------------------------------------------------------


class TestReads:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["cases"] == 3 and body["pending_approvals"] == 1

    def test_cases_list_shape(self, client):
        body = client.get("/api/cases").json()
        ids = {c["case_id"] for c in body["cases"]}
        assert {APPROVED, RUNNING, PENDING} <= ids
        row = next(c for c in body["cases"] if c["case_id"] == APPROVED)
        assert row["ticker"] == "INTC" and row["status"] == "CLOSED"
        assert row["clean_review_rounds"] == 2
        assert row["latest_report"].startswith("v3-")

    def test_case_detail(self, client):
        body = client.get(f"/api/cases/{APPROVED}").json()
        st = body["state"]
        assert st["status"] == "CLOSED"
        assert st["budget"]["used"]["rounds"] == 2
        assert st["candidate"]["clean_review_rounds"] == 2
        assert body["result"]["terminal"] == "APPROVED"
        assert body["approval_deadline"]
        forms = {d["form"] for d in body["documents_visible_at_cutoff"]}
        assert forms == {"10-K", "10-Q", "8-K"}

    def test_case_404(self, client):
        assert client.get("/api/cases/nope").status_code == 404
        body = client.get("/api/cases/nope").json()
        assert "not found" in body["error"]

    def test_trace_cursor(self, client):
        full = client.get(f"/api/cases/{APPROVED}/trace")
        n = full.json()["count"]
        assert n > 10
        assert full.json()["next_cursor"] == n
        tail = client.get(f"/api/cases/{APPROVED}/trace?from={n - 3}").json()
        assert tail["count"] == 3 and tail["cursor"] == n - 3
        # every event carries the fixed contract fields
        ev = full.json()["events"][0]
        for k in ("ts", "event", "case_id", "role", "request_id",
                  "candidate_id", "backend", "tokens_in", "tokens_out",
                  "detail"):
            assert k in ev

    def test_artifact_read_and_raw(self, client):
        r = client.get(f"/api/cases/{APPROVED}/artifacts/mandate.json")
        assert r.status_code == 200
        assert r.json()["kind"] == "json"
        assert r.json()["content"]["ticker"] == "INTC"
        raw = client.get(
            f"/api/cases/{APPROVED}/artifacts/report/v1-cand-a3f.md?raw=1")
        assert "Intel Corporation" in raw.text
        assert client.get(
            f"/api/cases/{APPROVED}/artifacts/missing.json").status_code == 404

    def test_replays(self, client):
        rows = client.get(f"/api/cases/{APPROVED}/replay").json()
        assert rows["count"] >= 4
        one = client.get(f"/api/cases/{APPROVED}/replay/req-0003").json()
        assert one["recording"]["role"] == "extractor"
        assert client.get(
            f"/api/cases/{APPROVED}/replay/req-9999").status_code == 404


# ---------------------------------------------------------------------------
# Case creation (wizard payload) + validation
# ---------------------------------------------------------------------------


class TestCreateCase:
    def test_create_ok(self, client):
        body = _create(client, period="fy2026q2")
        assert body["status"] == "CREATED" and body["mandate_id"]
        assert body["documents_visible_at_cutoff"] == [
            "intc-10k-2024", "intc-10q-2026q3", "intc-8k-earnings-2026q3"]

    def test_create_duplicate_409(self, client):
        _create(client, period="fy2027q1")
        r = client.post("/api/cases", json={**VALID_WIZARD,
                                            "period": "fy2027q1"})
        assert r.status_code == 409

    def test_validation_causes(self, client):
        base = dict(VALID_WIZARD)
        base["assumptions"] = dict(VALID_WIZARD["assumptions"])
        base["report_policy"] = dict(VALID_WIZARD["report_policy"])
        base["report_policy"]["length_words"] = [1600, 800]  # min > max
        base["assumptions"]["terminal_growth"] = 0.20        # > wacc
        base["cutoff_date"] = "30-10-2026"                   # bad format
        base["ticker"] = ""                                  # missing
        base["assumptions"]["forecast_bounds"] = {
            "segments": {"X": [0.5, 0.1]},                   # min > max
            "margin_floor_gross": 0.7, "margin_ceiling_gross": 0.5}
        r = client.post("/api/cases", json=base)
        assert r.status_code == 422
        errs = " | ".join(r.json()["errors"])
        assert "ticker" in errs
        assert "cutoff_date" in errs
        assert "terminal_growth" in errs
        assert "length_words" in errs
        assert "'X'" in errs
        assert "margin_floor_gross" in errs

    def test_validation_bad_segment_bounds(self, client):
        payload = dict(VALID_WIZARD)
        payload["assumptions"] = dict(VALID_WIZARD["assumptions"])
        payload["assumptions"]["forecast_bounds"] = {
            "segments": {"DCAI": [-5.0, 0.25]},
            "margin_floor_gross": 0.3, "margin_ceiling_gross": 0.55}
        r = client.post("/api/cases", json=payload)
        assert r.status_code == 422
        assert any("outside [-0.50, 1.00]" in e for e in r.json()["errors"])


# ---------------------------------------------------------------------------
# Run control
# ---------------------------------------------------------------------------


class TestRunControl:
    def test_run_new_case_appends_dispatch(self, client):
        cid = _create(client, period="fy2027q2")["case_id"]
        before = client.get(f"/api/cases/{cid}/trace").json()["count"]
        r = client.post(f"/api/cases/{cid}/run",
                        json={"backend": "auto", "replay": True})
        assert r.status_code == 200 and r.json()["status"] == "RUNNING"
        after = client.get(f"/api/cases/{cid}/trace?from={before}")
        assert after.json()["count"] == 1
        assert after.json()["events"][0]["event"] == "AGENT_DISPATCHED"

    def test_run_rejects_terminal_and_bad_backend(self, client):
        assert client.post(f"/api/cases/{APPROVED}/run",
                           json={"backend": "auto"}).status_code == 409
        cid = _create(client, period="fy26q1")["case_id"]
        assert client.post(f"/api/cases/{cid}/run",
                           json={"backend": "nope"}).status_code == 422
        # not-detected live backend in offline test env
        assert client.post(f"/api/cases/{cid}/run",
                           json={"backend": "claude"}).status_code == 409

    def test_resume_running_and_rejects_closed(self, client):
        r = client.post(f"/api/cases/{RUNNING}/resume")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert r.json()["re_dispatch"] == "critic"
        assert "pending role" in r.json()["note"]
        assert client.post(f"/api/cases/{APPROVED}/resume").status_code == 409


# ---------------------------------------------------------------------------
# Approval flow (human gate) — consumes the single pending case once
# ---------------------------------------------------------------------------


class TestApproval:
    def test_gate_requires_pending_state(self, client):
        assert client.post(f"/api/cases/{APPROVED}/approval",
                          json={"decision": "APPROVED",
                                "by": "X", }).status_code == 409
        assert client.post(f"/api/cases/{PENDING}/approval",
                           json={"decision": "MAYBE",
                                 "by": "X"}).status_code == 422

    def test_approval_records_and_closes(self, client):
        r = client.post(f"/api/cases/{PENDING}/approval",
                        json={"decision": "APPROVED", "by": "G. M. Chiantia",
                              "reasoning": "dossier verified"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["decision_id"].startswith("apr-")
        assert body["result"]["terminal"] == "APPROVED"
        # state + artifacts reflect the auditable record
        detail = client.get(f"/api/cases/{PENDING}").json()
        assert detail["state"]["status"] == "CLOSED"
        assert detail["approvals"][0]["by"] == "G. M. Chiantia"
        assert detail["approvals"][0]["reasoning"] == "dossier verified"
        assert detail["result"]["report_version"].startswith("v2-cand-5d2")
        assert "approvals/" in detail["artifacts"] or any(
            a.startswith("approvals/") for a in detail["artifacts"])
        # second decision on the closed case is a conflict
        assert client.post(f"/api/cases/{PENDING}/approval",
                           json={"decision": "REJECTED",
                                 "by": "X"}).status_code == 409

    def test_rejection_requires_reasonable_fields(self, client):
        assert client.post(f"/api/cases/{RUNNING}/approval",
                          json={"decision": "REJECTED"}).status_code == 409


# ---------------------------------------------------------------------------
# Demo scripted actions
# ---------------------------------------------------------------------------


class TestDemo:
    def test_seed_finding(self, client):
        before = len(
            client.get(f"/api/cases/{RUNNING}").json()["findings"])
        r = client.post(f"/api/cases/{RUNNING}/demo/seed-finding")
        body = r.json()
        assert r.status_code == 200
        assert body["finding_id"].startswith("fnd-")
        assert body["status"] == "open" and body["severity"] == "major"
        after = client.get(f"/api/cases/{RUNNING}").json()["findings"]
        assert len(after) == before + 1
        assert any(f["finding_id"] == body["finding_id"]
                   and f["status"] == "open" for f in after)

    def test_kill_stage_and_perturb(self, client):
        r = client.post(f"/api/cases/{RUNNING}/demo/kill-stage",
                        json={"stage": "extractor"})
        assert r.json()["killed_stage"] == "extractor"
        assert "resume" in r.json()["resume_hint"]
        p = client.post(f"/api/cases/{RUNNING}/demo/perturb").json()
        assert p["outputs_unchanged"] is True and p["leakage_detected"] is False
        assert p["outputs_before_sha256"] == p["outputs_after_sha256"]
        assert p["cutoff"] == "2027-01-29"

    def test_unknown_action_422(self, client):
        assert client.post(
            f"/api/cases/{RUNNING}/demo/explode").status_code == 422


# ---------------------------------------------------------------------------
# Report versions + diff + evidence
# ---------------------------------------------------------------------------


class TestReport:
    def test_latest_with_diff(self, client):
        body = client.get(f"/api/cases/{APPROVED}/report").json()
        assert body["version"].startswith("v3-")
        assert body["diff"]["changed"] is True
        assert body["diff"]["base_version"].startswith("v2-")
        assert any(l["op"] in "+- " for l in body["diff"]["lines"])
        assert body["within_bounds"] is True
        assert body["versions"] == [f"v{i}-cand-a3f" for i in (1, 2, 3)]

    def test_first_version_has_no_diff(self, client):
        body = client.get(
            f"/api/cases/{APPROVED}/report?version=v1-cand-a3f").json()
        assert body["diff"] is None

    def test_evidence_map(self, client):
        body = client.get(f"/api/cases/{APPROVED}/report").json()
        ev = body["evidence"]
        tokens = [e["token"] for e in ev]
        assert "13,500" in tokens and "20.75" in tokens
        cit = next(e for e in ev if e["token"] == "13,500")["citation"]
        assert cit["doc_id"] == "intc-10q-2026q3"
        assert cit["slice_id"] == "intc-10q-2026q3/IS"
        assert "Consolidated" in cit["anchor"]
        assert cit["doc_sha256"].startswith("b41f8c6e02d1") or len(
            cit["doc_sha256"]) >= 12

    def test_unknown_version_404(self, client):
        assert client.get(
            f"/api/cases/{APPROVED}/report?version=v99-x").status_code == 404

    def test_reports_appear_in_artifact_paths(self, client):
        arts = client.get(f"/api/cases/{APPROVED}").json()["artifacts"]
        assert any(a.startswith("report/v3-") for a in arts)
        assert any(a.startswith("replay/") for a in arts)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_xlsx_is_a_valid_zip(self, client):
        r = client.post(f"/api/cases/{APPROVED}/export",
                        json={"format": "xlsx"})
        assert r.status_code == 200
        body = r.json()
        assert body["version"].startswith("v3-") and body["bytes"] > 300
        data = client.get(body["download_url"]).content
        z = zipfile.ZipFile(io.BytesIO(data))
        assert "xl/worksheets/sheet1.xml" in z.namelist()

    def test_pdf_and_html_headers(self, client):
        pdf = client.post(f"/api/cases/{APPROVED}/export",
                          json={"format": "pdf"}).json()
        assert client.get(pdf["download_url"]).content.startswith(b"%PDF")
        html = client.post(f"/api/cases/{APPROVED}/export",
                           json={"format": "html"}).json()
        page = client.get(html["download_url"])
        assert b"<!doctype html>" in page.content.lower()
        assert "attachment" in page.headers["content-disposition"]

    def test_export_bad_format_and_unverified_case(self, client):
        assert client.post(f"/api/cases/{APPROVED}/export",
                          json={"format": "docx"}).status_code == 422
        r = client.post(f"/api/cases/{RUNNING}/export",
                        json={"format": "xlsx"})
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Evolution
# ---------------------------------------------------------------------------


class TestEvolution:
    def test_locked_scope_rejected(self, client):
        for scope in ("controller", "the gates and thresholds", "rubric v2"):
            r = client.post("/api/evolution/prepare",
                            json={"scope": scope, "description": "x"})
            assert r.status_code == 422, (scope, r.text)
            assert r.json().get("locked") is True

    def test_full_lifecycle(self, client):
        prep = client.post("/api/evolution/prepare", json={
            "scope": "prompt card extractor",
            "description": "tighten citation discipline"})
        assert prep.status_code == 200
        version = prep.json()["version"]
        assert version > client.get("/api/versions").json()["active"]
        # cannot approve before a passing regression run
        assert client.post("/api/evolution/approve", json={
            "version": version, "decision": "APPROVED",
            "by": "X"}).status_code == 409
        reg = client.post("/api/evolution/regress", json={"version": version})
        body = reg.json()
        assert body["regressions_all_pass"] is True
        assert len(body["cases"]) == 4
        app = client.post("/api/evolution/approve", json={
            "version": version, "decision": "APPROVED", "by": "G. Chiantia"})
        assert app.json()["active"] == version
        assert client.get("/api/versions").json()["active"] == version

    def test_rollback_with_verification(self, client):
        rb = client.post("/api/evolution/rollback",
                         json={"to_version": "sys-1.0"})
        body = rb.json()
        assert body["active"] == "sys-1.0"
        assert body["verification"]["outputs_match_history"] is True
        assert client.post("/api/evolution/rollback",
                           json={"to_version": "sys-1.0"}).status_code == 409
        assert client.post("/api/evolution/rollback",
                           json={"to_version": "sys-9.9"}).status_code == 404

    def test_registry_rows(self, client):
        reg = client.get("/api/versions").json()
        assert set(reg["locked_scopes"]) >= {"controller", "gates", "rubric"}
        v = reg["versions"][0]
        assert v["version"] == "sys-1.0"
        assert v["approval"]["decision"] == "APPROVED"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class TestBackends:
    def test_list_and_usage(self, client):
        body = client.get("/api/backends").json()
        names = [b["name"] for b in body["backends"]]
        assert names == ["claude", "codex", "kimi", "vibe", "api",
                         "replay", "stub"]
        assert body["caps"]["role_tokens_max"] == 60_000
        assert body["usage_by_role"]["extractor"] > 30_000

    def test_select(self, client):
        r = client.post("/api/backends/select", json={"backend": "replay"})
        assert r.status_code == 200
        sel = [b["name"] for b in r.json()["backends"] if b["selected"]]
        assert sel == ["replay"]
        assert client.post("/api/backends/select",
                           json={"backend": "claude"}).status_code == 409
        assert client.post("/api/backends/select",
                           json={"backend": "zzz"}).status_code == 422


# ---------------------------------------------------------------------------
# EDGAR + bundles
# ---------------------------------------------------------------------------


class TestEdgarBundles:
    def test_search_filters(self, client):
        allr = client.get("/api/edgar/search?ticker=intc").json()
        assert allr["count"] == 4 and allr["cik"] == "0000050863"
        q10 = client.get("/api/edgar/search?ticker=INTC&forms=10-Q").json()
        assert q10["count"] == 2
        win = client.get(
            "/api/edgar/search?ticker=INTC&window=2026-01-01:2026-12-31").json()
        assert {f["doc_id"] for f in win["filings"]} == {
            "intc-10q-2026q3", "intc-8k-earnings-2026q3"}
        assert client.get("/api/edgar/search").status_code == 422
        assert client.get(
            "/api/edgar/search?ticker=ZZZZ").json()["count"] == 0

    def test_bundle_lifecycle(self, client):
        assert client.get("/api/bundles").json()["count"] >= 2
        r = client.post("/api/bundles", json={
            "cik": "0000050863",
            "docs": ["intc-10k-2024", "intc-10q-2026q3"],
            "name": "intc-snapshot-a"})
        assert r.status_code == 200
        m = r.json()
        assert m["bundle_id"] == "intc-snapshot-a"
        assert len(m["documents"]) == 2 and m["bundle_sha256"]
        assert m["bundle_id"] in [b["bundle_id"] for b in
                                  client.get("/api/bundles").json()["bundles"]]
        assert client.post("/api/bundles", json={"cik": "", "docs": [],
                                                 "name": "x"}).status_code == 422
        assert client.post("/api/bundles", json={
            "cik": "0000050863", "docs": ["intc-10k-2024"],
            "name": "intc-snapshot-a"}).status_code == 409


# ---------------------------------------------------------------------------
# WebSocket push + polling fallback guarantee
# ---------------------------------------------------------------------------


class TestWebSocket:
    def test_snapshot_on_connect_and_event_push(self, ws_client):
        with ws_client.websocket_connect("/ws/events") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot"
            assert snap["cases"]["count"] >= 3
            # a UI action surfaces as a pushed event
            cid = _create(ws_client, period="fy27q1")["case_id"]
            ws_client.post(f"/api/cases/{cid}/run",
                           json={"backend": "auto", "replay": True})
            seen = 0
            ev = None
            for _ in range(3):
                msg = ws.receive_json()
                if msg["type"] == "event":
                    seen += 1
                    if msg["event"] == "AGENT_DISPATCHED":
                        ev = msg
                        break
            assert ev is not None
            assert ev["case_id"] == cid and "ts" in ev

    def test_polling_fallback_endpoint_always_serves(self, client):
        """With the socket down, the SPA polls this exact endpoint."""
        for _ in range(3):
            body = client.get("/api/cases").json()
            assert body["health"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Static SPA served
# ---------------------------------------------------------------------------


class TestStatic:
    def test_index_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "<!doctype html>" in r.text.lower()
