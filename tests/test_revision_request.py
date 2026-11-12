"""Human revision request (REVISION_REQUESTED) and the reasoning requirement.

Offline: the scripted stub dispatch and engines the orchestrator tests use.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import casekit
from coverageflow import _dispatch_test_stub as _stub
from coverageflow import ledger, orchestrator as orchmod, statestore
from tests.test_orchestrator import (_critic_findings, build_env, load_state)

REASON = "the valuation paragraph ignores the buyback; critics missed it"


def _pending(tmp_path, **kw):
    orch, dispatch, clk, case_dir = build_env(tmp_path, **kw)
    orch.run()
    assert load_state(case_dir)["status"] == "PENDING_APPROVAL"
    return orch, dispatch, case_dir


def _escalated(tmp_path):
    mandate = json.loads(json.dumps(casekit.MANDATE))
    mandate["limits"]["rounds_max"] = 2
    orch, dispatch, clk, case_dir = build_env(
        tmp_path, scripts={"critic_c1": _critic_findings()}, mandate=mandate)
    orch.run()
    assert load_state(case_dir)["status"] == "ESCALATED"
    return orch, dispatch, case_dir


def _finding(owner="writer", summary="Valuation ignores the buyback", **kw):
    return {"summary": summary, "owner": owner, **kw}


def _human_rows(case_dir):
    return [r for r in ledger.FindingsLedger(case_dir).all_findings()
            if r.get("source_critic") == "human"]


def _events(case_dir, name):
    return [e for e in statestore.read_trace(case_dir) if e["event"] == name]


# ---------------------------------------------------------------- reasoning
class TestReasoningRequired:
    @pytest.mark.parametrize("text", ["", "   ", "ok", "Approved.",
                                      "too short", None])
    def test_reject_needs_real_reasoning(self, tmp_path, text):
        orch, _, case_dir = _pending(tmp_path)
        with pytest.raises(ValueError, match="REJECTED needs written reasoning"):
            orch.record_approval("REJECTED", by="Sup", reasoning=text)
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"

    @pytest.mark.parametrize("text", ["", "ok", "fine."])
    def test_publish_with_exceptions_needs_real_reasoning(self, tmp_path, text):
        orch, _, case_dir = _escalated(tmp_path)
        ids = load_state(case_dir)["reason"]["detail"]["blocking_finding_ids"]
        with pytest.raises(ValueError, match="PUBLISHED_WITH_EXCEPTIONS needs"):
            orch.record_approval("PUBLISHED_WITH_EXCEPTIONS", by="Sup",
                                 reasoning=text, acknowledge_findings=ids)
        assert load_state(case_dir)["status"] == "ESCALATED"

    @pytest.mark.parametrize("text", ["", "ok", "redo it"])
    def test_revision_needs_real_reasoning(self, tmp_path, text):
        orch, _, case_dir = _pending(tmp_path)
        with pytest.raises(ValueError, match="REVISION_REQUESTED needs"):
            orch.record_approval("REVISION_REQUESTED", by="Sup", reasoning=text,
                                 revision_findings=[_finding()])
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"
        assert not _human_rows(case_dir)

    def test_plain_approval_keeps_reasoning_optional(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        out = orch.record_approval("APPROVED", by="Sup")
        assert out["terminal"] == "APPROVED"

    def test_whitespace_does_not_pad_the_reason(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        with pytest.raises(ValueError):
            orch.record_approval("REJECTED", by="Sup", reasoning="no" + " " * 30)


# ---------------------------------------------------------------- the request
class TestRequestRevision:
    def test_from_pending_approval(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        before = load_state(case_dir)
        assert before["candidate"]["clean_review_rounds"] == 2
        out = orch.record_approval(
            "REVISION_REQUESTED", by="Sup", reasoning=REASON,
            revision_findings=[_finding("writer", severity="critical")])
        st = load_state(case_dir)
        assert out["status"] == "RUNNING" and out["terminal"] is None
        assert out["revision_requests_remaining"] == 1
        assert st["status"] == "RUNNING"
        assert st["revision_requests"] == 1
        assert st["pending"]["deadline"] is None
        assert st["pending"]["role"] == "generator"
        assert st["candidate"]["clean_review_rounds"] == 0
        assert st["candidate"]["stall"] == {"history": [], "streak": 0}
        assert st["reason"] is None
        assert not (case_dir / "result.json").exists()

    def test_from_escalated(self, tmp_path):
        orch, _, case_dir = _escalated(tmp_path)
        open_before = {r["finding_id"] for r in
                       ledger.FindingsLedger(case_dir).blocking_findings()}
        orch.request_revision("Sup", REASON, [_finding("model")])
        st = load_state(case_dir)
        assert st["status"] == "RUNNING"
        assert st["pending"]["round"] > 0
        # the findings that escalated the case ride along with the new one
        rows = ledger.FindingsLedger(case_dir).repairable_findings()
        assert open_before <= {r["finding_id"] for r in rows}
        assert any(r["source_critic"] == "human" for r in rows)

    def test_human_finding_enters_the_ledger(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        out = orch.request_revision("Sup", REASON, [
            _finding("writer", required_evidence="cite the repurchase note"),
            _finding("model", "WACC too low", severity="critical",
                     affected_artifacts=["forecast_assumptions.json"])])
        rows = _human_rows(case_dir)
        assert [r["finding_id"] for r in rows] == out["finding_ids"]
        assert all(r["finding_id"].startswith("fnd-") for r in rows)
        w, m = rows
        assert (w["owner"], w["source_critic"], w["raised_by"]) == (
            "writer", "human", "Sup")
        assert w["severity"] == "major" and w["status"] == "open"
        assert w["affected_artifacts"] == ["report.md"]
        assert w["required_evidence"] == "cite the repurchase note"
        assert w["dedup_key"] == "human|1|report.md"
        assert (m["owner"], m["severity"]) == ("model", "critical")
        assert m["dedup_key"] == "human|2|forecast_assumptions.json"

    def test_trace_events_and_recorded_grant(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        before = load_state(case_dir)["budget"]["rounds_max"]
        out = orch.request_revision("Sup", REASON, [_finding()])
        ev = _events(case_dir, "REVISION_REQUESTED")
        assert len(ev) == 1
        d = ev[0]["detail"]
        assert d["by"] == "Sup" and d["reasoning"] == REASON
        assert d["finding_ids"] == out["finding_ids"]
        assert d["budget_grant"]["rounds_max_after"] == before + 2
        assert d["revision_request"] == 1 and d["from"] == "PENDING_APPROVAL"
        opened = [e for e in _events(case_dir, "FINDING_OPENED")
                  if e["detail"].get("source") == "human"]
        assert len(opened) == 1
        trans = [e for e in _events(case_dir, "STATE_TRANSITION")
                 if e["detail"].get("revision_requested")]
        assert trans[0]["detail"]["from"] == "PENDING_APPROVAL"
        assert trans[0]["detail"]["to"] == "RUNNING"
        st = load_state(case_dir)
        grants = st["budget"]["used"]["grants"]
        assert grants[-1]["reason"] == "REVISION_REQUESTED"
        assert st["budget"]["rounds_max"] == before + 2

    def test_approval_history_records_it_without_closing(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        out = orch.request_revision("Sup", REASON, [_finding()])
        rec = statestore.read_json(
            case_dir / "approvals" / f"{out['decision_id']}.json")
        assert rec["decision"] == "REVISION_REQUESTED"
        assert rec["reasoning"] == REASON
        assert rec["finding_ids"] == out["finding_ids"]
        assert rec["budget_grant"]["rounds_max_after"] > 0
        assert {e["detail"]["decision"] for e in
                _events(case_dir, "APPROVAL_RECORDED")} == {"REVISION_REQUESTED"}
        assert not list((case_dir / "report").glob("v*.md")) \
            if (case_dir / "report").exists() else True

    @pytest.mark.parametrize("owner,stage", [("extractor", "extractor"),
                                             ("model", "model"),
                                             ("writer", "writer")])
    def test_rebuild_starts_at_the_stage_the_owner_names(
            self, tmp_path, owner, stage):
        orch, _, case_dir = _pending(tmp_path)
        out = orch.request_revision("Sup", REASON, [_finding(owner)])
        assert out["rebuild_from"] == stage
        assert load_state(case_dir)["pending"]["rebuild_from"] == stage
        packet = next(case_dir.glob("rounds/*/revision-packet-1.json"))
        body = statestore.read_json(packet)
        assert body["rebuild_from"] == stage
        assert [f["source_critic"] for f in body["open_findings"]] == ["human"]

    def test_earliest_owner_wins(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        out = orch.request_revision("Sup", REASON, [_finding("writer"),
                                                    _finding("extractor", "x")])
        assert out["rebuild_from"] == "extractor"

    def test_only_at_the_human_gate(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        with pytest.raises(RuntimeError, match="ESCALATED or PENDING_APPROVAL"):
            orch.request_revision("Sup", REASON, [_finding()])

    def test_needs_a_finding(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        with pytest.raises(ValueError, match="at least one finding"):
            orch.request_revision("Sup", REASON, [])
        with pytest.raises(ValueError, match="summary is required"):
            orch.request_revision("Sup", REASON, [_finding(summary="  ")])
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"

    @pytest.mark.parametrize("owner,route", [("system", "needs_system_change"),
                                             ("mandate", "needs_mandate_change")])
    def test_stop_owners_are_refused(self, tmp_path, owner, route):
        orch, _, case_dir = _pending(tmp_path)
        with pytest.raises(ValueError, match=route):
            orch.request_revision("Sup", REASON, [_finding(owner)])
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"
        assert not _human_rows(case_dir)

    def test_unknown_owner_and_minor_severity_are_refused(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        with pytest.raises(ValueError, match="owner must be one of"):
            orch.request_revision("Sup", REASON, [_finding("nobody")])
        with pytest.raises(ValueError, match="severity"):
            orch.request_revision("Sup", REASON, [_finding(severity="minor")])

    def test_expired_approval_blocks_instead(self, tmp_path):
        orch, dispatch, clk, case_dir = build_env(tmp_path)
        orch.run()
        clk.advance(60 * 3600)
        with pytest.raises(RuntimeError, match="APPROVAL_EXPIRED"):
            orch.request_revision("Sup", REASON, [_finding()])
        assert load_state(case_dir)["status"] == "BLOCKED"


# ---------------------------------------------------------------- the loop
class TestRevisionRun:
    def test_human_finding_reaches_the_generator_and_closes(self, tmp_path):
        orch, dispatch, case_dir = _pending(tmp_path)
        out = orch.request_revision("Sup", REASON, [_finding("writer")])
        fid = out["finding_ids"][0]
        orch.run()
        st = load_state(case_dir)
        assert st["status"] == "PENDING_APPROVAL"
        assert st["candidate"]["clean_review_rounds"] == 2
        carried = [c.payload for c in dispatch.calls_of("writer")][-1]
        ids = [f["finding_id"] for f in carried["carried_findings"]]
        assert fid in ids
        carried_row = next(f for f in carried["carried_findings"]
                           if f["finding_id"] == fid)
        assert carried_row["source_critic"] == "human"
        # the critic who owns the area was shown it and verified it closed
        assert any(fid in [f["finding_id"] for f in c.payload["carried_findings"]]
                   for c in dispatch.calls_of("critic_c1"))
        row = ledger.FindingsLedger(case_dir).by_id(fid)
        assert row["status"] == "closed"
        assert row["closed_by"]["critic"] == "critic_c1"
        assert row["closed_by"]["verified_evidence"]["sha256"]
        assert _events(case_dir, "FINDING_VERIFIED_CLOSED")

    def test_model_finding_is_checked_by_critic_c2(self, tmp_path):
        orch, dispatch, case_dir = _pending(tmp_path)
        fid = orch.request_revision("Sup", REASON,
                                    [_finding("model")])["finding_ids"][0]
        orch.run()
        assert any(fid in [f["finding_id"] for f in c.payload["carried_findings"]]
                   for c in dispatch.calls_of("critic_c2"))
        assert not any(fid in [f["finding_id"] for f in c.payload["carried_findings"]]
                       for c in dispatch.calls_of("critic_c1"))
        assert ledger.FindingsLedger(case_dir).by_id(fid)["status"] == "closed"

    def test_not_closed_by_silence(self, tmp_path):
        def silent(payload):
            rep = _stub._default_critic("critic_c1")(payload)
            rep["verified_resolutions"] = []
            return rep

        orch, dispatch, case_dir = _pending(tmp_path)
        fid = orch.request_revision("Sup", REASON,
                                    [_finding("writer")])["finding_ids"][0]
        dispatch.scripts["critic_c1"] = silent
        orch.run()
        # a critic that says nothing about a carried finding is an invalid
        # submission; the finding is still open and the case did not advance
        assert ledger.FindingsLedger(case_dir).by_id(fid)["status"] == "open"
        assert load_state(case_dir)["status"] != "PENDING_APPROVAL"

    def test_a_critic_can_keep_it_open_by_re_emitting_it(self, tmp_path):
        def re_emit(payload):
            fid = payload["carried_findings"][0]["finding_id"]
            return _critic_findings() | {
                "findings": [{**_critic_findings()["findings"][0],
                              "finding_id": fid}]}

        orch, dispatch, case_dir = _pending(tmp_path)
        fid = orch.request_revision("Sup", REASON,
                                    [_finding("writer")])["finding_ids"][0]
        dispatch.scripts["critic_c1"] = [re_emit]
        orch.run()
        # the re-emit refreshed the human row rather than opening a twin
        assert len(_human_rows(case_dir)) == 1
        # round after round it is carried again until a critic closes it
        assert ledger.FindingsLedger(case_dir).by_id(fid)["status"] == "closed"
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"

    def test_open_human_finding_is_listed_when_published_with_exceptions(
            self, tmp_path):
        def silent_forever(payload):
            fid = payload["carried_findings"][0]["finding_id"]
            f = _critic_findings()["findings"][0]
            return _critic_findings() | {"findings": [{**f, "finding_id": fid}]}

        mandate = json.loads(json.dumps(casekit.MANDATE))
        mandate["limits"]["rounds_max"] = 2
        orch, dispatch, clk, case_dir = build_env(tmp_path, mandate=mandate)
        orch.run()
        fid = orch.request_revision("Sup", REASON,
                                    [_finding("writer")])["finding_ids"][0]
        dispatch.scripts["critic_c1"] = silent_forever
        orch.run()
        assert load_state(case_dir)["status"] == "ESCALATED"
        out = orch.record_approval(
            "PUBLISHED_WITH_EXCEPTIONS", by="Sup",
            reasoning="the human objection is addressed in the next note",
            acknowledge_findings=[r["finding_id"] for r in
                                  ledger.FindingsLedger(case_dir).blocking_findings()])
        assert fid in [e["finding_id"] for e in
                       out["result"]["reason"]["detail"]["exceptions"]]
        note = next(p for p in (case_dir / "report").glob("v*.md")
                    if not p.name.endswith(".clean.md")).read_text()
        assert fid in note


class TestLimit:
    def test_two_requests_then_the_gate_closes(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        orch.request_revision("Sup", REASON, [_finding("writer")])
        orch.run()
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"
        out = orch.request_revision("Sup", REASON, [_finding("writer", "again")])
        assert out["revision_requests_remaining"] == 0
        orch.run()
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"
        with pytest.raises(RuntimeError, match="revision limit reached"):
            orch.request_revision("Sup", REASON, [_finding("writer", "third")])
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"
        assert load_state(case_dir)["revision_requests"] == orchmod.MAX_REVISION_REQUESTS
        # the other doors stay open
        out = orch.record_approval("REJECTED", by="Sup", reasoning=REASON)
        assert out["terminal"] == "REJECTED"
        assert len(_events(case_dir, "REVISION_REQUESTED")) == 2

    def test_each_request_grants_two_rounds(self, tmp_path):
        orch, _, case_dir = _pending(tmp_path)
        base = load_state(case_dir)["budget"]["rounds_max"]
        orch.request_revision("Sup", REASON, [_finding()])
        orch.run()
        orch.request_revision("Sup", REASON, [_finding(summary="again")])
        grants = load_state(case_dir)["budget"]["used"]["grants"]
        assert [g["revision_request"] for g in grants] == [1, 2]
        assert load_state(case_dir)["budget"]["rounds_max"] == base + 4

    def test_escalated_case_gets_rounds_to_use(self, tmp_path):
        orch, dispatch, case_dir = _escalated(tmp_path)
        orch.request_revision("Sup", REASON, [_finding("writer")])
        dispatch.scripts["critic_c1"] = None      # the critic stops objecting
        orch.run()
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"


# ---------------------------------------------------------------- API / CLI
def _far_deadline(case_dir):
    # the fake clock sits in 1970; the service reads the wall clock
    st = load_state(case_dir)
    st["pending"]["deadline"] = "2999-01-01T00:00:00Z"
    statestore.write_state(case_dir, st)


class TestApi:
    @pytest.fixture()
    def api(self, tmp_path, monkeypatch):
        from ui.api import create_app
        orch, dispatch, case_dir = _pending(tmp_path)
        _far_deadline(case_dir)
        monkeypatch.setenv("COVERAGEFLOW_WORKSPACE", str(tmp_path))
        return TestClient(create_app()), orch, case_dir

    def test_revision_through_the_approval_endpoint(self, api):
        client, orch, case_dir = api
        cid = case_dir.name
        r = client.post(f"/api/cases/{cid}/approval", json={
            "decision": "REVISION_REQUESTED", "by": "Reviewer",
            "reasoning": REASON,
            "findings": [{"summary": "Valuation ignores the buyback",
                          "owner": "writer", "severity": "critical"}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "RUNNING"
        assert body["revision"] == {"used": 1, "max": 2, "remaining": 1,
                                    "rounds_per_request": 2}
        assert body["finding_ids"] and body["decision_id"].startswith("apr-")
        detail = client.get(f"/api/cases/{cid}").json()
        assert detail["state"]["status"] == "RUNNING"
        assert detail["revision"]["remaining"] == 1
        assert detail["approvals"][-1]["decision"] == "REVISION_REQUESTED"
        human = [f for f in detail["findings"] if f["source_critic"] == "human"]
        assert human and human[0]["owner"] == "writer"

    def test_api_refuses_short_reasoning_and_bad_owner(self, api):
        client, orch, case_dir = api
        cid = case_dir.name
        r = client.post(f"/api/cases/{cid}/approval", json={
            "decision": "REJECTED", "by": "Reviewer", "reasoning": "ok"})
        assert r.status_code == 422 and "REJECTED needs" in r.text
        r = client.post(f"/api/cases/{cid}/approval", json={
            "decision": "REVISION_REQUESTED", "by": "Reviewer",
            "reasoning": REASON,
            "findings": [{"summary": "engine bug", "owner": "system"}]})
        assert r.status_code == 422 and "needs_system_change" in r.text
        r = client.post(f"/api/cases/{cid}/approval", json={
            "decision": "REVISION_REQUESTED", "by": "Reviewer",
            "reasoning": REASON, "findings": []})
        assert r.status_code == 422
        assert load_state(case_dir)["status"] == "PENDING_APPROVAL"

    def test_api_limit_is_a_conflict(self, api):
        client, orch, case_dir = api
        cid = case_dir.name
        body = {"decision": "REVISION_REQUESTED", "by": "Reviewer",
                "reasoning": REASON,
                "findings": [{"summary": "fix it", "owner": "writer"}]}
        for _ in range(2):
            _far_deadline(case_dir)
            assert client.post(f"/api/cases/{cid}/approval", json=body
                               ).status_code == 200
            orch.run()
        _far_deadline(case_dir)
        r = client.post(f"/api/cases/{cid}/approval", json=body)
        assert r.status_code == 409 and "revision limit" in r.text


class TestCli:
    def test_finding_flag_parsing(self):
        from coverageflow import cli
        assert cli._parse_revision_finding("writer:Fix the table") == {
            "owner": "writer", "summary": "Fix the table"}
        assert cli._parse_revision_finding("model/critical:WACC: too low") == {
            "owner": "model", "severity": "critical",
            "summary": "WACC: too low"}
        with pytest.raises(SystemExit):
            cli._parse_revision_finding("no separator")

    def test_decision_is_a_choice(self):
        from coverageflow import cli
        args = cli.build_parser().parse_args(
            ["approve", "--case", "c", "--decision", "REVISION_REQUESTED",
             "--by", "S", "--reason", REASON,
             "--finding", "writer:one", "--finding", "model:two"])
        assert args.finding == ["writer:one", "model:two"]
