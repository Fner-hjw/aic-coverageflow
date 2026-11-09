#!/usr/bin/env python3
"""Evidence run 4 helper — inject / restore a post-cutoff document.

Inject (rehearsal): appends a synthetic SEC filing DATED AFTER the case
cutoff to the case's input manifest so the demo perturbation can prove the
cutoff gate rejects it structurally. --restore puts the frozen manifest
back byte-for-byte (this is a scripted rehearsal: the manifest is otherwise
immutable per case).

Run from the repository root:
    python examples/run4-perturbation/inject_post_cutoff_doc.py \
        --workspace examples/run4-perturbation --case intc-fy2026q3-20261030
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

POST_CUTOFF_DOC = {
    "doc_id": "intc-8k-2026-11-05-postcutoff-injection",
    "form": "8-K",
    "filed": "2026-11-05",
    "period": "2026-11-05",
    "path": "inputs/intc-8k-2026-11-05.htm",
    "sha256": hashlib.sha256(b"post-cutoff injection (rehearsal)").hexdigest(),
    "bytes": 0,
    "source_url": None
}

BACKUP_SUFFIX = ".frozen-backup"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    case_dir = Path(args.workspace) / "cases" / args.case
    manifest_path = case_dir / "inputs" / "manifest.json"
    backup_path = manifest_path.with_name(manifest_path.name + BACKUP_SUFFIX)

    if args.restore:
        if not backup_path.exists():
            raise SystemExit("nothing to restore")
        shutil.copyfile(backup_path, manifest_path)
        backup_path.unlink()
        print(json.dumps({"restored": str(manifest_path),
                         "sha256": hashlib.sha256(
                             manifest_path.read_bytes()).hexdigest()}))
        return 0

    if backup_path.exists():
        raise SystemExit("backup already exists (inject once)")
    shutil.copyfile(manifest_path, backup_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["documents"].append(POST_CUTOFF_DOC)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({
        "injected": POST_CUTOFF_DOC["doc_id"],
        "filed": POST_CUTOFF_DOC["filed"],
        "cutoff_in_mandate": json.loads(
            (case_dir / "mandate.json").read_text())["cutoff_date"],
        "note": "scripted rehearsal only; --restore returns the frozen manifest",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
