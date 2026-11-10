"""Shared test helpers for stream A2 tests.

Synthetic case/mate fixture builders used by test_orchestrator / test_gates /
test_resume / test_budget / test_registry. Everything runs offline; all LLM
roles are satisfied by coverageflow._dispatch_test_stub; engines are injected
stub doubles; clocks are injected so nothing depends on wall time beyond the
case-clock accounting under test.
"""

from __future__ import annotations

import json
from pathlib import Path

from coverageflow import statestore

# ---------------------------------------------------------------------------
# mandate / manifest fixtures (shape per contracts/state-and-artifacts.md §3-4)
# ---------------------------------------------------------------------------

MANDATE = {
    "mandate_id": "man-0001",
    "case_id": "intc-fy2026q3-20261030",
    "ticker": "INTC",
    "cik": "0000050863",
    "cutoff_date": "2026-10-30",
    "report_policy": {
        "note_type": "quarterly_update",
        "length_words": [300, 1600],
        "sections": ["Summary", "Results", "Outlook", "Valuation", "Risks"],
    },
    "assumptions": {
        "as_of": "2026-10-30",
        "wacc": 0.10,
        "terminal_growth": 0.02,
        "exit_multiple_ebitda": 14.0,
        "discount_convention": "year_end",
        "tax_rate": 0.15,
        "forecast_bounds": {
            "segments": {
                "CCG": [-0.10, 0.12],
                "DCAI": [-0.05, 0.25],
                "NEX": [-0.05, 0.20],
                "Foundry": [-0.05, 0.30],
                "Altera": [-0.10, 0.15],
                "Mobileye": [-0.05, 0.20],
            },
            "margin_floor_gross": 0.30,
            "margin_ceiling_gross": 0.55,
        },
        "peer_block": {
            "as_of": "2026-10-30",
            "peers": [{"ticker": "AMD", "p_e_ttm": None, "ev_ebitda": None,
                       "p_s": None, "rev_growth_yoy": None,
                       "notional_note": "filled at mandate creation"}],
        },
        "dcf_comps_tolerance": 0.25,
    },
    "limits": {
        "role_tokens_max": 250000,
        "case_minutes": 45,
        "rounds_max": 4,
        "retries_max": 3,
        "approval_hours": 48,
    },
    "schema_version": "1.0",
}

MANIFEST = {
    "as_of_cutoff": "2026-10-30",
    "documents": [
        {
            "doc_id": "intc-10q-2026q3",
            "form": "10-Q",
            "filed": "2026-10-30",
            "period": "2026-09-26",
            "path": "inputs/intc-10q-2026q3.htm",
            "sha256": "f" * 64,
            "bytes": 1834512,
            "source_url": "https://www.sec.gov/Archives/example.htm",
        }
    ],
    "bundle_id": "intc-2026-10",
    "bundle_sha256": "a" * 64,
}


def materialize_inputs(workspace: Path, case_id: str) -> None:
    """Write a placeholder file for every document the case manifest declares.

    A case is only runnable once its source filings are in the inputs
    directory; ``Orchestrator.run`` blocks a CREATED case whose manifest
    points at absent files. The statement slices the offline stub declares are
    materialized too, so a replayed ingest (which writes no files) still
    leaves the extractor's payload references resolvable.
    """
    from coverageflow._dispatch_test_stub import _slice_text

    case_dir = Path(workspace) / "cases" / case_id
    manifest = json.loads((case_dir / "inputs" / "manifest.json").read_text())
    for doc in manifest.get("documents") or []:
        rel = (doc or {}).get("path")
        if not rel:
            continue
        target = case_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(f"<html>{doc.get('doc_id', 'doc')}</html>\n",
                              encoding="utf-8")
        doc_id = doc.get("doc_id")
        if not doc_id:
            continue
        slice_path = case_dir / "inputs" / "slices" / doc_id / "IS.txt"
        slice_path.parent.mkdir(parents=True, exist_ok=True)
        if not slice_path.exists():
            slice_path.write_text(_slice_text(doc_id), encoding="utf-8")


def make_case_dir(root: Path, case_id: str = "intc-fy2026q3-20261030",
                  mandate: dict | None = None, manifest: dict | None = None) -> Path:
    """Build a synthetic, valid case directory (mandate + inputs manifest).

    Every document the manifest declares is materialized as a small placeholder
    file: a case whose inputs are absent is not a runnable case.
    """
    mand = json.loads(json.dumps(mandate if mandate is not None else MANDATE))
    mand["case_id"] = case_id
    mfst = json.loads(json.dumps(manifest if manifest is not None else MANIFEST))
    case_dir = root / "cases" / case_id
    (case_dir / "inputs").mkdir(parents=True, exist_ok=True)
    statestore.atomic_write_json(case_dir / "mandate.json", mand)
    statestore.atomic_write_json(case_dir / "inputs" / "manifest.json", mfst)
    materialize_inputs(root, case_id)
    return case_dir
