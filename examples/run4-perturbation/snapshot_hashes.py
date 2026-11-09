#!/usr/bin/env python3
"""Evidence run 4 helper — snapshot published-state hashes for stability checks.

Run from the repository root:
    python examples/run4-perturbation/snapshot_hashes.py \
        --workspace examples/run4-perturbation \
        --case intc-fy2026q3-20261030 --out hashes-baseline.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

TARGETS = [
    "inputs/manifest.json",
    "report/{report_version}.md",
    "result.json",
    "approvals",   # directory: hash of each file
    "rounds/round-001/numerical/gates.json",
    "rounds/round-001/generator/model.json",
    "rounds/round-001/generator/valuation.json",
]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    case_dir = Path(args.workspace) / "cases" / args.case
    result = json.loads((case_dir / "result.json").read_text())
    report_version = result["report_version"]

    out: dict[str, str] = {}
    for rel in TARGETS:
        rel = rel.format(report_version=report_version)
        path = case_dir / rel
        if path.is_dir():
            for f in sorted(path.glob("*.json")):
                out[f"{rel}/{f.name}"] = sha256_file(f)
            continue
        out[rel] = sha256_file(path)

    Path(args.out).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"case": args.case, "files_hashed": len(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
