#!/usr/bin/env python
"""Estimate prompt size per role for a live case round (read-only).

Rebuilds the prompts a round would send (preamble + role card + references +
bound request) from the artifacts on disk and prints chars/4 token estimates.
The case is COPIED to a temp directory first; the original is never touched.

    python scripts/measure_prompts.py CASE_DIR [--round 11] [--baseline-ref fix/diagnosis]

With --baseline-ref the same measurement runs against that git ref (exported
to a temp dir) and a before/after table is printed. Carried findings are the
case's current ledger, not the ledger as it stood in that round.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def measure(code_root: Path, case: Path, rnd: int) -> dict[str, int]:
    sys.path.insert(0, str(code_root))
    from coverageflow import _prompt_assembly as pa, statestore
    from coverageflow import orchestrator as O
    from coverageflow._dispatch_test_stub import RoleCall
    from coverageflow.dispatch import build_bound_request, output_schema_for

    tmp = Path(tempfile.mkdtemp()) / "case"
    shutil.copytree(case, tmp)
    orch = O.Orchestrator(tmp, dispatch=None, gate_engine=object())
    state = orch.load()
    for f in (tmp / "rounds").rglob("*"):
        if f.is_file():
            orch._verified[str(f.relative_to(tmp))] = statestore.sha256_file(f)
    arts = O.Arts(round=rnd)

    def prompt(role: str, payload: dict) -> str:
        call = RoleCall(
            role=role, request_id="req-x", candidate_id="c", role_card={},
            payload=payload, output_schema=output_schema_for(role),
            budget={"tokens_max": 500000, "timeout_seconds": 600,
                    "cost_usd_max": 5}, write_scope=["x"])
        return pa.assemble_prompt(code_root / "prompts" / f"{role}.md",
                                  build_bound_request(call), root=code_root)

    def gen_payload(stage: str) -> dict:
        if hasattr(orch, "_generator_payload"):
            return orch._generator_payload(state, arts, stage)
        return {"case_id": state["case_id"], "round": rnd,   # legacy path
                "carried_findings": orch._carried(),
                "mandate": orch.mandate(), "bundle": orch._bundle_provenance(),
                "prior_artifacts": orch._stage_inputs(arts, stage)}

    out: dict[str, str] = {}
    for role in ("model", "writer"):
        out[role] = prompt(role, gen_payload(role))
    cid = state["candidate"]["candidate_id"]
    if (code_root / "prompts" / "generator_lead.md").is_file():
        p = gen_payload("candidate")
        p.update({"candidate_id": cid, "artifacts": orch._candidate_artifact_list(arts),
                  "evidence_index": orch._evidence_index(arts)})
        out["generator_lead"] = prompt("generator_lead", p)
    ca = {r["artifact"]: r["sha256"] for r in orch._candidate_artifact_list(arts)}
    for role in O.CRITIC_ROLES:
        out[role] = prompt(role, orch._critic_payload(state, role, rnd, arts, cid, ca))
    if (code_root / "prompts" / "critic_lead.md").is_file():
        reports = [json.loads((tmp / arts.rel("critic", f"{r}.json")).read_text())
                   for r in O.CRITIC_ROLES]
        out["critic_lead"] = prompt("critic_lead", {
            "case_id": state["case_id"], "round": rnd, "candidate_id": cid,
            "critic_reports": reports, "carried_findings": orch._carried(),
            "built_round": rnd})
    return {k: len(v) // 4 for k, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("case", type=Path)
    ap.add_argument("--round", type=int, default=11)
    ap.add_argument("--baseline-ref")
    ap.add_argument("--_json-root", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a._json_root:                       # child mode: print JSON only
        print(json.dumps(measure(Path(a._json_root), a.case, a.round)))
        return

    def run(root: Path) -> dict[str, int]:
        r = subprocess.run([sys.executable, __file__, str(a.case), "--round",
                            str(a.round), "--_json-root", str(root)],
                           check=True, capture_output=True, text=True)
        return json.loads(r.stdout.strip().splitlines()[-1])

    after = run(HERE)
    before: dict[str, int] = {}
    if a.baseline_ref:
        d = Path(tempfile.mkdtemp())
        arc = subprocess.run(["git", "-C", str(HERE), "archive", a.baseline_ref],
                             check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(d)], input=arc, check=True)
        before = run(d)
    roles = list(dict.fromkeys(list(before) + list(after)))
    print(f"{'role':16s}{'before':>10s}{'after':>10s}   (chars/4 tokens)")
    for r in roles:
        print(f"{r:16s}{before.get(r, '-'):>10}{after.get(r, '-'):>10}")
    print(f"{'TOTAL':16s}{sum(before.values()) or '-':>10}{sum(after.values()):>10}")


if __name__ == "__main__":
    main()
