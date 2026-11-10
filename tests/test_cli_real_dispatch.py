"""CLI dispatch wiring: load_dispatch must return the REAL agent-layer
dispatch (coverageflow.dispatch.dispatch_role over a chosen backend), with the
scripted offline stub only as an explicit fallback.

Integration stream (A6). The flagship test records a scripted rehearsal run
with the REAL dispatch + a RecordingBackend (canned gate-satisfying
responses while recording the assembled prompts), then re-runs an identical
fresh case through `python -m coverageflow run --replay` so the ReplayBackend
answers from those recordings - proving the CLI drives the real dispatch
(prompt assembly, strict parsing, budget enforcement) end-to-end, offline.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from coverageflow import cli
from coverageflow import _dispatch_test_stub as stubmod
from coverageflow import statestore
from coverageflow.backends.base import AgentResult, HealthReport
from coverageflow.dispatch import RoleCall

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- canned BE


class CannedBackend:
    """Answers with the scripted stub defaults as VALID JSON wire text.

    The scripted stub builds the parsed artifact; the wire format a real
    backend must produce is the JSON serialization of it (the stub's
    ``str(parsed)`` repr would fail the real dispatch's strict parse).
    """

    name = "canned"

    def __init__(self, scripts=None):
        self._stub = stubmod.StubDispatch(scripts)

    def healthcheck(self):
        return HealthReport(backend=self.name, ok=True)

    def estimate_cost(self, text):
        return None

    def invoke(self, call, assembled_prompt):
        res = self._stub(call)
        if not res.ok:
            return res
        text = json.dumps(res.parsed)
        return AgentResult(ok=True, output_text=text, backend=self.name,
                           elapsed_seconds=0.01,
                           tokens_in=len(assembled_prompt) // 4,
                           tokens_out=len(text) // 4)


def real_dispatch_over(backend, workspace=None):
    """The same adapter the CLI's load_dispatch builds for live backends."""
    from coverageflow import dispatch as dispatch_mod

    class RealDispatch:
        name = backend.name

        def __init__(self, be):
            self._backend = be

        def __call__(self, call):
            result, _trace = dispatch_mod.dispatch_role(
                call, self._backend,
                root=cli._dispatch_root(workspace))
            return result

        def healthcheck(self):
            report = self._backend.healthcheck()
            return {"ok": report.ok, "backend": report.backend,
                    "detail": report.detail}

    return RealDispatch(backend)


def _new_case(ws: Path) -> str:
    from coverageflow import orchestrator as orch_mod
    case_id = orch_mod.new_case_id("INTC", "fy2026q3", "2026-10-30")
    cli.main(["--workspace", str(ws), "case", "new", "--ticker", "INTC",
              "--trigger-doc", "intc-10q-2026q3", "--cutoff", "2026-10-30"])
    return case_id


def _load(ws: Path, case_id: str) -> dict:
    return statestore.load_state(ws / "cases" / case_id)


class TestLoadDispatch:
    def test_default_is_the_offline_stub(self):
        dispatch, backend = cli.load_dispatch()
        assert isinstance(dispatch, stubmod.StubDispatch)
        assert backend == "stub (offline)"

    def test_explicit_stub_and_offline_aliases(self):
        for name in ("stub", "offline"):
            dispatch, backend = cli.load_dispatch(backend=name)
            assert isinstance(dispatch, stubmod.StubDispatch)
            assert backend == "stub (offline)"

    def test_replay_backend_requires_replay_dir(self):
        with pytest.raises(ValueError):
            cli.load_dispatch(backend="replay")

    def test_replay_builds_real_dispatch_over_recordings(self, tmp_path):
        (tmp_path / "replay").mkdir()
        record = {"role": "ingest", "request_id": "req-0001",
                  "candidate_id": None, "prompt_sha256": "0" * 64,
                  "prompt_path": "prompts/req-0001.md",
                  "assembled_prompt_sha256": "0" * 64,
                  "response_text": "{}", "backend": "replay",
                  "recorded_at": "2026-01-01T00:00:00Z"}
        (tmp_path / "replay" / "req-0001.json").write_text(
            json.dumps(record), encoding="utf-8")
        dispatch, backend = cli.load_dispatch(backend="replay",
                                              replay_dir=tmp_path / "replay")
        assert backend == "replay"
        assert dispatch.healthcheck()["ok"] is True
        assert not isinstance(dispatch, stubmod.StubDispatch)

    def test_unknown_backend_name_is_rejected(self):
        with pytest.raises(SystemExit):
            cli.load_dispatch(backend="does-not-exist")


class TestRealDispatchRunOverRecordings:
    def test_cli_run_uses_real_dispatch_with_replay(self, tmp_path):
        """Full offline loop: record with the real dispatch, replay via CLI.

        1. recording run: a REAL dispatch drives the orchestrator; every
           assembled prompt is recorded with its canned response
           (RecordingBackend).
        2. replay run: `run --replay` on an IDENTICAL fresh case answered by
           the ReplayBackend from those recordings -- the CLI exercises the
           real dispatch + backend seam end to end.
        """
        from coverageflow.backends.replay_backends import RecordingBackend
        from coverageflow.orchestrator import Orchestrator

        rec_ws = tmp_path / "rec-ws"
        rec_ws.mkdir()
        case_id = _new_case(rec_ws)
        case_dir = rec_ws / "cases" / case_id
        assert (case_dir / "state.json").is_file()

        # --- 1. recording run with the REAL dispatch -----------------------
        record_dir = case_dir / "replay"
        backend = RecordingBackend(CannedBackend(), record_dir=str(record_dir))
        orch = Orchestrator(case_dir,
                            dispatch=real_dispatch_over(backend, rec_ws),
                            gate_engine=cli.load_gate_engine(
                                stub_engines=True, workspace=rec_ws))
        summary = orch.run()
        state = _load(rec_ws, case_id)
        assert state["status"] == "PENDING_APPROVAL", summary
        recordings = sorted(p.name for p in record_dir.glob("*.json"))
        assert len(recordings) >= 10   # 2 full rounds of role calls

        # --- 2. replay run via the CLI ------------------------------------
        rep_ws = tmp_path / "rep-ws"
        rep_ws.mkdir()
        _new_case(rep_ws)
        rep_case_dir = rep_ws / "cases" / case_id
        rep_replay = rep_case_dir / "replay"
        rep_replay.mkdir()
        for name in recordings:
            shutil.copy(record_dir / name, rep_replay / name)

        rc = cli.main(["--workspace", str(rep_ws), "run", "--replay",
                       "--stub-engines"])
        assert rc == 0
        events = statestore.read_trace(rep_case_dir)
        agent_results = [e for e in events if e["event"] == "AGENT_RESULT"]
        assert agent_results, "no dispatch events recorded"
        # the real dispatch ran: the replay backend stamps backend + token
        # estimates the stub never reports (tokens_in/out were None under the
        # scripted stub; replay estimates chars/4)
        assert all(e["backend"] == "replay" for e in agent_results)
        assert all(isinstance(e["tokens_in"], int) and e["tokens_in"] > 0
                   for e in agent_results)
        assert all(isinstance(e["tokens_out"], int) and e["tokens_out"] > 0
                   for e in agent_results)
        assert all(e["detail"]["ok"] for e in agent_results)

        state2 = _load(rep_ws, case_id)
        assert state2["status"] == "PENDING_APPROVAL"
        # round 2 was a short-circuited rebuild (generator stages
        # hash-identical via proofs; only fresh critic context re-runs)
        assert state2["budget"]["used"]["rounds"] >= 1

        # determinism proof: the replayed candidate artifacts are identical
        def _artifact(ws_, rel):
            return (ws_ / "cases" / case_id / rel).read_bytes()
        for rel in ("rounds/round-001/generator/facts.json",
                    "rounds/round-001/generator/report.md",
                    "rounds/round-001/generator/model.json"):
            assert (rec_ws / "cases" / case_id / rel).is_file()
            assert (rep_case_dir / rel).is_file()
            assert _artifact(rec_ws, rel) == _artifact(rep_ws, rel)

    def test_replay_miss_is_a_hard_error(self, tmp_path):
        """Missing recordings never silently replay wrong context."""
        from coverageflow.backends.replay_backends import ReplayMissError

        ws = tmp_path / "ws"
        ws.mkdir()
        case_id = _new_case(ws)
        case_dir = ws / "cases" / case_id
        dispatch, backend = cli.load_dispatch(backend="replay",
                                              replay_dir=case_dir / "replay")
        call = RoleCall(role="ingest", request_id="req-9999", candidate_id=None,
                        role_card={"path": "prompts/ingest.md",
                                   "version": "1.1", "sha256": None},
                        payload={"case_id": case_id, "round": 1},
                        output_schema="references/schemas/1.0",
                        budget={"tokens_max": 60000, "timeout_seconds": 5},
                        write_scope=["rounds/round-001/generator/"
                                     "ingest-report.json"])
        with pytest.raises(ReplayMissError):
            dispatch(call)


def main():  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
