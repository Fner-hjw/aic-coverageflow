#!/usr/bin/env python3
"""Evidence run 3 helper — kill mid-run, then verified CLI resume.

Produces the kill-half of examples/run3-kill-resume BEFORE the CLI takes
over: it runs the real Orchestrator with a dispatch that dies during the
*writer* role call (the same rehearsal the UI demo panel calls "kill run"),
leaving the case RUNNING with pending stage `writer`. The trace will show
REQUEST_BOUND + AGENT_DISPATCHED without an AGENT_RESULT for that call.

Run from the repository root:
    python examples/run3-kill-resume/kill_during_run.py \
        --workspace examples/run3-kill-resume --case intc-fy2026q3-20261030
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# running from the repository root: make the coverageflow package importable
sys.path.insert(0, os.getcwd())

from coverageflow import gates as gates_mod  # noqa: E402
from coverageflow import _dispatch_test_stub as stub  # noqa: E402
from coverageflow.orchestrator import Orchestrator  # noqa: E402


class KillDuringRun(RuntimeError):
    """Simulates the process dying mid-dispatch."""


class KillerDispatch:
    """Wraps the offline stub dispatch: raises on the Nth call of a role."""

    def __init__(self, inner, role: str, n: int):
        self.inner = inner
        self.kill_role = role
        self.kill_n = n

    def __call__(self, call):
        prior = len([1 for c in self.inner.calls if c.role == call.role]) + 1
        if call.role == self.kill_role and prior == self.kill_n:
            raise KillDuringRun(f"simulated kill during {call.role} dispatch")
        return self.inner(call)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--kill-role", default="extractor")
    ap.add_argument("--kill-n", type=int, default=1)
    args = ap.parse_args()

    ws = Path(args.workspace)
    case_dir = ws / "cases" / args.case
    round_dir = case_dir / "rounds" / "round-001"

    engine = gates_mod.GateEngine(
        schema_path=Path("references/model-schema.json"),
        build_model_fn=stub.stub_build_model,
        value_fn=stub.stub_value_company)
    dispatch = KillerDispatch(stub.StubDispatch(),
                              args.kill_role, args.kill_n)
    orch = Orchestrator(case_dir, dispatch=dispatch, gate_engine=engine)
    killed = False
    try:
        orch.run()
    except KillDuringRun as exc:
        killed = True
        print(json.dumps({"killed_during": args.kill_role,
                          "detail": str(exc)}, indent=2))
    if not killed:
        raise SystemExit(f"kill did not fire (role {args.kill_role})")

    # snapshot what exists after the kill: artifact digests + content with the
    # request/candidate-id header stamps removed (those stamps are applied by
    # the candidate stage — docs/contracts headers rule — and are not part of
    # the artifact's analytical content)
    gen_dir = round_dir / "generator"
    def strip_stamps(d):
        d = dict(d)
        d.pop("candidate_id", None)
        d.pop("request_id", None)
        return d
    present = {}
    for path in sorted(gen_dir.glob("*.json")):
        present[path.name] = {
            "sha256": sha256_file(path),
            "content": strip_stamps(json.loads(path.read_text())),
        }
    (ws / "after-kill-artifacts.json").write_text(
        json.dumps({"pending_stage": args.kill_role,
                    "completed_artifacts": present}, indent=2) + "\n")

    # trace tail after the kill: the dangling dispatch (no AGENT_RESULT)
    events = [json.loads(line) for line in (case_dir / "trace.jsonl").read_text().splitlines() if line]
    dangling = [e for e in events
                if e["event"] == "AGENT_DISPATCHED"
                and e["role"] == args.kill_role
                and not any(r["request_id"] == e["request_id"]
                            and r["event"] == "AGENT_RESULT"
                            for r in events)]
    print(json.dumps({"dangling_dispatch_requests":
                      [e["request_id"] for e in dangling],
                      "state": json.loads((case_dir / "state.json").read_text())["status"],
                      "pending_role": json.loads(
                          (case_dir / "state.json").read_text())["pending"].get("role"),
                      "pending_stage": json.loads(
                          (case_dir / "state.json").read_text())["pending"].get("stage")},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
