#!/usr/bin/env python3
"""Evidence run 4 helper — assert outputs are stable across the perturbation.

Compares hashes-baseline.json with hashes-after.json: every published
artifact must be byte-identical before and after the post-cutoff injection
(rejected by the gate, so it can never reach the outputs) and its restore.

Run from the repository root:
    python examples/run4-perturbation/check_stability.py \\
        --workspace examples/run4-perturbation
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    args = ap.parse_args()

    ws = Path(args.workspace)
    base = json.loads((ws / "hashes-baseline.json").read_text())
    after = json.loads((ws / "hashes-after.json").read_text())
    identical = [k for k in base if after.get(k) == base[k]]
    changed = [k for k in base if after.get(k) != base[k]]
    ok = (base == after)
    print(json.dumps({
        "files_checked": len(base),
        "byte_identical_pre_post_perturbation": len(identical),
        "changed": changed,
        "STABLE_OUTPUTS": ok,
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
