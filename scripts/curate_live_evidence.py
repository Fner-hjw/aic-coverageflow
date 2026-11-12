#!/usr/bin/env python3
"""Copy the small, shareable evidence of live cases into examples/run6-amd-live/.

The live workspace is not committed (it holds the 2 MB filings per case and is
gitignored). This keeps, per case: the mandate, state and result, the findings
ledger, the trace, every round's gate report, and the last round's artifacts
(note, model, valuation, forecast, facts). The filings themselves are in
``data/amd-bundle`` and are referenced by the manifest.

Usage:  python scripts/curate_live_evidence.py WORKSPACE OUT_DIR LABEL=CASE_ID_OR_DIR ...
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

KEEP_TOP = ("mandate.json", "state.json", "result.json", "findings.json",
            "trace.jsonl", "evolution-input.json")


def curate(ws: Path, out: Path, label: str, case_id: str) -> dict:
    src = Path(case_id) if "/" in case_id else ws / "cases" / case_id
    dst = out / label
    dst.mkdir(parents=True, exist_ok=True)
    for name in KEEP_TOP:
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
    rounds = sorted(p for p in (src / "rounds").glob("round-*") if p.is_dir())
    for r in rounds:
        g = r / "numerical" / "gates.json"
        if g.is_file():
            (dst / "rounds" / r.name / "numerical").mkdir(parents=True, exist_ok=True)
            shutil.copy2(g, dst / "rounds" / r.name / "numerical" / "gates.json")
        for f in r.glob("critic/*.json"):
            (dst / "rounds" / r.name / "critic").mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst / "rounds" / r.name / "critic" / f.name)
    # the last built candidate, whole
    built = [r for r in rounds if (r / "generator" / "report.md").is_file()]
    if built:
        last = built[-1]
        (dst / "last-candidate").mkdir(exist_ok=True)
        for f in (last / "generator").iterdir():
            if f.is_file() and f.name != "ingest-report.json":
                shutil.copy2(f, dst / "last-candidate" / f.name)
    for f in (src / "export").glob("*") if (src / "export").is_dir() else []:
        if f.suffix in (".xlsx", ".html", ".pdf"):
            (dst / "export").mkdir(exist_ok=True)
            shutil.copy2(f, dst / "export" / f.name)
    st = json.loads((src / "state.json").read_text()) if (src / "state.json").is_file() else {}
    used = (st.get("budget") or {}).get("used") or {}
    return {"label": label, "case_id": case_id, "status": st.get("status"),
            "reason": (st.get("reason") or {}).get("code"),
            "rounds": used.get("rounds"),
            "mechanical_rounds": used.get("mechanical_rounds"),
            "elapsed_minutes": used.get("elapsed_minutes"),
            "tokens_by_role": used.get("tokens_by_role")}


def main(argv: list[str]) -> int:
    ws, out = Path(argv[1]), Path(argv[2])
    rows = []
    for spec in argv[3:]:
        label, case_id = spec.split("=", 1)
        rows.append(curate(ws, out, label, case_id))
    (out / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
