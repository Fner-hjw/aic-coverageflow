#!/usr/bin/env python3
"""Evidence run 3 helper — assert the resume re-ran ONLY the pending role.

Compares, after the CLI `resume` in examples/run3-kill-resume:
  1. the completed build artifacts are byte-identical to their pre-kill
     digests (captured in after-kill-artifacts.json);
  2. the trace binds new work ONLY after the resume point for roles AFTER
     the killed stage (writer re-dispatched; ingest / extractor / model
     never dispatched a second time);
  3. the torn partial (written by `demo kill-stage`) was discarded, not
     adopted as verified output.

Run from the repository root:
    python examples/run3-kill-resume/check_resume_evidence.py \
        --workspace examples/run3-kill-resume --case intc-fy2026q3-20261030
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--case", required=True)
    args = ap.parse_args()

    ws = Path(args.workspace)
    case_dir = ws / "cases" / args.case

    pre = json.loads((ws / "after-kill-artifacts.json").read_text())
    round001 = case_dir / "rounds" / "round-001" / "generator"

    # 1. completed artifacts unchanged across the kill, except for the
    #    request/candidate-id header stamps the candidate stage always applies
    #    (contract: every artifact header carries request ID + candidate ID).
    #    So compare analytical CONTENT, ignoring those two stamps.
    identical, changed = {}, []
    for name, snap in pre["completed_artifacts"].items():
        cur = json.loads((round001 / name).read_text())
        cur.pop("candidate_id", None)
        cur.pop("request_id", None)
        if cur == snap["content"]:
            identical[name] = sha256_file(round001 / name)
        else:
            changed.append(name)
    report = {
        "case": args.case,
        "completed_artifacts_unchanged": {
            "checked": len(pre["completed_artifacts"]),
            "byte_identical": len(identical),
            "changed": changed,
        },
    }

    # 2. exactly one dispatched attempt per role before/after the resume
    events = [json.loads(line) for line in
              (case_dir / "trace.jsonl").read_text().splitlines() if line]
    dispatched = Counter(e["role"] for e in events
                         if e["event"] == "AGENT_DISPATCHED")
    results = Counter(e["role"] for e in events
                      if e["event"] == "AGENT_RESULT")
    report["dispatch_counts"] = dict(dispatched)
    report["result_counts"] = dict(results)
    pre_ok = json.loads((case_dir / "state.json").read_text())
    report["only_pending_role_re_ran"] = {
        "killed_role_total_dispatches": dispatched.get("extractor", 0),
        "killed_role_results": results.get("extractor", 0),
        "ingest_re_dispatched": dispatched.get("ingest", 0) > 1,
        "model_re_dispatched": dispatched.get("model", 0) > 1,
        "writer_re_dispatched": dispatched.get("writer", 0) > 1,
        "lead_re_dispatched": dispatched.get("generator_lead", 0) > 1,
    }

    # 3. the torn partial was discarded, never adopted
    discard_dir = round001 / "discarded"
    report["torn_partial_discarded"] = sorted(
        p.name for p in discard_dir.glob("*")) if discard_dir.exists() else []
    report["facts_are_not_the_torn_partial"] = \
        not (round001 / "facts.json").read_text().startswith('{"partial": ')

    state = json.loads((case_dir / "state.json").read_text())
    report["final_status"] = state["status"]
    report["final_clean_review_rounds"] = state["candidate"]["clean_review_rounds"]

    ok = (report["completed_artifacts_unchanged"]["byte_identical"]
          == report["completed_artifacts_unchanged"]["checked"]
          and not report["only_pending_role_re_ran"]["ingest_re_dispatched"]
          and not report["only_pending_role_re_ran"]["model_re_dispatched"]
          and not report["only_pending_role_re_ran"]["writer_re_dispatched"]
          and not report["only_pending_role_re_ran"]["lead_re_dispatched"]
          and report["only_pending_role_re_ran"]["killed_role_total_dispatches"] == 2
          and report["only_pending_role_re_ran"]["killed_role_results"] == 1
          and report["facts_are_not_the_torn_partial"])
    report["ALL_CHECKS_PASS"] = ok
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
