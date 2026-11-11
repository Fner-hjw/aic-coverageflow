"""UI endpoint: prefill proposal, and submitting the reviewed form = accept."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from ui import service
from ui.api import create_app
from tests.market_fixtures import CUTOFF, world


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(service, "_PREFILL_FETCH", world())
    return TestClient(create_app())


def _form_payload(proposal, period="fy2026q4", **assump_edits):
    """What app.js sends: form fields plus market/own_band/sensitivity and the proposal."""
    a = copy.deepcopy(proposal["assumptions"])
    a.update(assump_edits)
    return {"ticker": "INTC", "cik": proposal["cik"], "period": period,
            "bundle_id": "intc-2026-10", "cutoff_date": CUTOFF,
            "trigger_document": "intc-10q-2026q3",
            "report_policy": proposal["report_policy"],
            "assumptions": {k: a[k] for k in (
                "wacc", "terminal_growth", "exit_multiple_ebitda", "tax_rate",
                "forecast_bounds", "peer_block", "dcf_comps_tolerance",
                "market", "own_band", "sensitivity")},
            "prefill": {"proposal": proposal}}


def test_prefill_endpoint_returns_proposal_with_provenance(client):
    r = client.post("/api/mandate/prefill", json={"ticker": "intc", "cutoff_date": CUTOFF})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "proposed" and body["assumptions"]["market"]["price"] == 23.0
    assert body["provenance"]["assumptions.market.price"]["sha256"]
    assert body["missing"] == [] or isinstance(body["missing"], list)


def test_prefill_endpoint_peers_and_validation(client):
    r = client.post("/api/mandate/prefill", json={"ticker": "INTC", "cutoff_date": CUTOFF,
                                                 "peers": "AMD"})
    assert [p["ticker"] for p in r.json()["assumptions"]["peer_block"]["peers"]] == ["AMD"]
    assert client.post("/api/mandate/prefill", json={"ticker": "", "cutoff_date": CUTOFF}).status_code == 422
    assert client.post("/api/mandate/prefill", json={"ticker": "INTC", "cutoff_date": "x"}).status_code == 422
    assert client.post("/api/mandate/prefill", json={"ticker": "NOPE", "cutoff_date": CUTOFF}).status_code == 422


def test_prefill_does_not_create_a_case(client):
    before = client.get("/api/cases").json()["count"]
    client.post("/api/mandate/prefill", json={"ticker": "INTC", "cutoff_date": CUTOFF})
    assert client.get("/api/cases").json()["count"] == before


def test_submitting_the_reviewed_form_accepts_and_records_edits(client):
    proposal = client.post("/api/mandate/prefill",
                           json={"ticker": "INTC", "cutoff_date": CUTOFF}).json()
    body = _form_payload(proposal, exit_multiple_ebitda=17.5)
    r = client.post("/api/cases", json=body)
    assert r.status_code == 200, r.text
    case = client.get(f"/api/cases/{r.json()['case_id']}").json()
    m = case["mandate"]
    assert m["status"] == "accepted" and m["approval"]["by"] == "human"
    assert [(e["path"], e["final"]) for e in m["approval"]["edits"]] == [
        ("assumptions.exit_multiple_ebitda", 17.5)]
    assert m["provenance"]["assumptions.exit_multiple_ebitda"]["edited"] is True
    assert m["provenance"]["assumptions.market.price"]["sha256"]


def test_unedited_form_has_no_edits(client):
    proposal = client.post("/api/mandate/prefill",
                           json={"ticker": "INTC", "cutoff_date": CUTOFF}).json()
    r = client.post("/api/cases", json=_form_payload(proposal, period="fy2026q1"))
    assert r.status_code == 200, r.text
    m = client.get(f"/api/cases/{r.json()['case_id']}").json()["mandate"]
    assert m["approval"]["edits"] == []


def test_form_with_a_missing_price_is_refused(client):
    proposal = client.post("/api/mandate/prefill",
                           json={"ticker": "INTC", "cutoff_date": CUTOFF}).json()
    body = _form_payload(proposal)
    body["assumptions"]["market"] = {"as_of": CUTOFF, "price": None}
    r = client.post("/api/cases", json=body)
    assert r.status_code == 422
    assert any("market.price" in e for e in r.json()["errors"])
