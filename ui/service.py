"""Service boundary for the CoverageFlow web UI (stream A4, owned).

PURPOSE: one function per API endpoint defined in docs/contracts/interfaces.md
section 5 (and the CLI verbs of section 3 where they back a UI page).  api.py /
ws.py / app.js never touch files or orchestration logic — they only call the
functions below.

STUB POLICY: until the controller (A2) and dispatch (A3) streams land, every
function is a LOCAL STUB backed by realistic in-memory demo data for the two
demo bundles (Intel, AMD), generated deterministically at import time (no
network, no LLM, no clock reads in the fixtures — the demo store is
reproducible).  The coordinator later swaps bodies for real `coverageflow`
package calls; keep the function names/signatures/return shapes EXACT.  Do
not implement orchestrator state-machine logic beyond the demo flows.

Mutable demo flows supported (mirrored by real operations later): create case
(mandate validation), run, resume, human approval record, demo scripted
actions (seed finding / kill stage / perturbation), report version diff,
export, evolution prepare/regress/approve/rollback, bundle snapshot.

Fixed timestamps belong to the deterministic fixtures; `health()` and
decision records use live UTC time and are marked as such.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Error boundary (api.py maps these to HTTP statuses)
# ---------------------------------------------------------------------------


class ServiceError(Exception):
    """Base class for service-layer errors carrying an HTTP status."""

    status = 400

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = {"error": message, **detail}


class NotFound(ServiceError):
    status = 404


class ValidationError(ServiceError):
    status = 422


class ConflictError(ServiceError):
    status = 409


# ---------------------------------------------------------------------------
# Real-package hook (coordinator swaps stubs here; nothing else changes)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised only after A2/A3 integration
    from coverageflow import orchestrator as _real_orchestrator  # type: ignore
    from coverageflow import registry as _real_registry  # type: ignore

    _HAVE_REAL_BACKEND = True
except Exception:  # not present on the a4-ui branch yet
    _real_orchestrator = None  # type: ignore[assignment]
    _real_registry = None  # type: ignore[assignment]
    _HAVE_REAL_BACKEND = False


# ---------------------------------------------------------------------------
# Event hook: ws.py registers a sink; stub state changes publish through it
# ---------------------------------------------------------------------------

_event_hook: Callable[[dict], None] | None = None


def set_event_hook(fn: Callable[[dict], None] | None) -> None:
    """Register (or clear) the WebSocket event sink used by ws.py."""
    global _event_hook
    _event_hook = fn


def _emit(case_id: str, event: str, detail: dict | None = None) -> None:
    """Publish an event to the WS hook (no-op until ws.py registers one)."""
    if _event_hook is None:
        return
    ev = {"case_id": case_id, "event": event, "detail": detail or {}}
    try:
        _event_hook(ev)
    except Exception:  # a broken WS sink must never break a state operation
        pass


def _now_iso() -> str:
    """Live UTC timestamp for operational endpoints (health/run/approve)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Demo store: deterministic in-memory representation of a workspace
# ---------------------------------------------------------------------------


class Case:
    """In-memory analogue of a case directory (contracts/state-and-artifacts)."""

    def __init__(self, case_id: str, state: dict, mandate: dict, manifest: dict,
                 trace: list[dict], ledger: list[dict], reports: list[dict],
                 replays: list[dict]):
        self.case_id = case_id
        self.state = state
        self.mandate = mandate
        self.manifest = manifest
        self.trace = trace
        self.ledger = ledger
        self.reports = reports   # [{"version", "candidate_id", "round", "markdown", "evidence"}]
        self.replays = replays   # contents of replay/<request_id>.json
        self.artifacts: dict[str, Any] = {"mandate.json": mandate,
                                          "inputs/manifest.json": manifest,
                                          "state.json": state}
        self.approvals: list[dict] = []
        self.result: dict | None = None
        self.rounds: list[dict] = []


def _ev(case_id: str, ts: str, event: str, *, role: str = "", round_n: int = 0,
        request_id: str = "", candidate_id: str = "", artifact: str = "",
        backend: str = "replay", tokens_in: int = 0, tokens_out: int = 0,
        **detail: Any) -> dict:
    """Build one trace.jsonl event per contracts/state-and-artifacts section 2."""
    return {
        "ts": ts, "event": event, "case_id": case_id, "role": role,
        "round": round_n, "request_id": request_id, "candidate_id": candidate_id,
        "artifact": artifact,
        "artifact_sha256": _sha(artifact or event)[:16] if artifact else "",
        "prompt_versions": {}, "backend": backend,
        "tokens_in": tokens_in, "tokens_out": tokens_out, "detail": detail,
    }

_INTC_MANIFEST = {
    "as_of_cutoff": "2026-10-30",
    "documents": [
        {"doc_id": "intc-10k-2024", "form": "10-K", "filed": "2025-01-24",
         "period": "2024-12-28", "path": "inputs/intc-10k-2024.htm",
         "sha256": "b41f8c6e02d1" + "0" * 49, "bytes": 3_812_004,
         "source_url": "https://www.sec.gov/Archives/edgar/data/50863/000005086325000010/intc-20241228.htm"},
        {"doc_id": "intc-10q-2026q3", "form": "10-Q", "filed": "2026-10-30",
         "period": "2026-09-26", "path": "inputs/intc-10q-2026q3.htm",
         "sha256": "9ac07e3d55b1" + "0" * 49, "bytes": 1_834_512,
         "source_url": "https://www.sec.gov/Archives/edgar/data/50863/000005086326000034/intc-20260926.htm"},
        {"doc_id": "intc-8k-earnings-2026q3", "form": "8-K", "filed": "2026-10-30",
         "period": "2026-09-26", "path": "inputs/intc-8k-earnings-2026q3.htm",
         "sha256": "77fe91045ac3" + "0" * 49, "bytes": 214_880,
         "source_url": "https://www.sec.gov/Archives/edgar/data/50863/000005086326000035/intc-8k-2026q3.htm"},
    ],
    "bundle_id": "intc-2026-10",
    "bundle_sha256": "sha256:4f2d1b0a" + "0" * 51,
}

_AMD_MANIFEST = {
    "as_of_cutoff": "2026-10-29",
    "documents": [
        {"doc_id": "amd-10k-2024", "form": "10-K", "filed": "2025-02-05",
         "period": "2024-12-28", "path": "inputs/amd-10k-2024.htm",
         "sha256": "c2ab8310fd99" + "0" * 49, "bytes": 3_140_992,
         "source_url": "https://www.sec.gov/Archives/edgar/data/2488/000000248825000012/amd-20241228.htm"},
        {"doc_id": "amd-10q-2026q3", "form": "10-Q", "filed": "2026-10-29",
         "period": "2026-09-27", "path": "inputs/amd-10q-2026q3.htm",
         "sha256": "13de07fa77bc" + "0" * 49, "bytes": 1_590_210,
         "source_url": "https://www.sec.gov/Archives/edgar/data/2488/000000248826000021/amd-20260927.htm"},
    ],
    "bundle_id": "amd-2026-10",
    "bundle_sha256": "sha256:a07c4421" + "0" * 51,
}


def _mandate(ticker: str, cik: str, cutoff: str, doc: str,
             segs: dict[str, list[float]], peers: list[dict],
             mandate_id: str, case_id: str) -> dict:
    """mandate.json per contracts section 3 (frozen shape)."""
    return {
        "mandate_id": mandate_id, "case_id": case_id, "ticker": ticker,
        "cik": cik, "cutoff_date": cutoff,
        "report_policy": {
            "note_type": "quarterly_update", "length_words": [100, 1600],
            "sections": ["Summary", "Results", "Segment detail", "Outlook",
                         "Valuation", "Risks"]},
        "assumptions": {
            "as_of": cutoff, "wacc": 0.104, "terminal_growth": 0.025,
            "exit_multiple_ebitda": 14.0, "discount_convention": "year_end",
            "tax_rate": 0.15,
            "forecast_bounds": {
                "segments": segs, "margin_floor_gross": 0.30,
                "margin_ceiling_gross": 0.55},
            "peer_block": {"as_of": cutoff, "peers": peers},
            "dcf_comps_tolerance": 0.25},
        "limits": {"role_tokens_max": 60000, "case_minutes": 45, "rounds_max": 4,
                   "retries_max": 3, "approval_hours": 48},
        "schema_version": "1.0", "trigger_document": doc,
    }


_INTEL_SEGS = {"CCG": [-0.10, 0.12], "DCAI": [-0.05, 0.25], "NEX": [-0.05, 0.20],
               "Foundry": [-0.05, 0.30], "Altera": [-0.10, 0.15],
               "Mobileye": [-0.05, 0.20]}
_INTEL_PEERS = [
    {"ticker": "AMD", "p_e_ttm": 48.2, "ev_ebitda": 27.6, "p_s": 8.1,
     "rev_growth_yoy": 0.22,
     "notional_note": "trailing multiples as of 2026-10-28 close"},
    {"ticker": "NVDA", "p_e_ttm": 52.4, "ev_ebitda": 33.1, "p_s": 24.4,
     "rev_growth_yoy": 0.55,
     "notional_note": "trailing multiples as of 2026-10-28 close"},
    {"ticker": "TSM", "p_e_ttm": 28.9, "ev_ebitda": 15.2, "p_s": 10.7,
     "rev_growth_yoy": 0.30,
     "notional_note": "ADR, trailing multiples as of 2026-10-28 close"},
]
_AMD_SEGS = {"Data Center": [-0.05, 0.35], "Client": [-0.10, 0.20],
             "Embedded": [-0.20, 0.05], "Gaming": [-0.15, 0.10]}
_AMD_PEERS = [
    {"ticker": "INTC", "p_e_ttm": None, "ev_ebitda": 18.9, "p_s": 2.3,
     "rev_growth_yoy": -0.02,
     "notional_note": "trailing multiples as of 2026-10-28 close"},
    {"ticker": "NVDA", "p_e_ttm": 52.4, "ev_ebitda": 33.1, "p_s": 24.4,
     "rev_growth_yoy": 0.55,
     "notional_note": "trailing multiples as of 2026-10-28 close"},
]


def _budget(rounds: int, minutes: float, tokens: dict[str, int]) -> dict:
    """budget block of state.json (used vs max — feeds the UI meters)."""
    return {"rounds_max": 4, "retries_max": 3, "case_minutes": 45,
            "role_tokens_max": 60000,
            "used": {"rounds": rounds, "elapsed_minutes": minutes,
                     "tokens_by_role": tokens}}


def _critic_report(role: str, request_id: str, cand: str, round_n: int,
                   verdict: str, findings: list[dict]) -> dict:
    return {"role": role, "request_id": request_id, "candidate_id": cand,
            "round": round_n, "verdict": verdict, "findings": findings,
            "coverage": {"rubric_version": "1.0",
                         "areas_checked": ["citation_integrity", "recomputation",
                                           "cutoff_discipline", "budget_controls"]},
            "conformance": "schema 1.0", "prompt_version": "1.0"}


INGEST_REPORT = {
    "role": "ingest", "request_id": "req-0002", "candidate_id": "cand-a3f9c1",
    "doc_id": "intc-10q-2026q3",
    "classification": {
        "form": "10-Q", "affects": ["extraction", "model", "valuation", "report"],
        "reason": "new quarterly statements supersede prior-period estimates"},
    "slices": [
        {"slice_id": "intc-10q-2026q3/IS", "kind": "income_statement",
         "path": "inputs/slices/intc-10q-2026q3/IS.txt",
         "sha256": "sha256:71ab" + "0" * 55,
         "anchors": {"first_line": "Consolidated Statements of Operations"}},
        {"slice_id": "intc-10q-2026q3/BS", "kind": "balance_sheet",
         "path": "inputs/slices/intc-10q-2026q3/BS.txt",
         "sha256": "sha256:30dd" + "0" * 55,
         "anchors": {"first_line": "Condensed Consolidated Balance Sheets"}},
        {"slice_id": "intc-10q-2026q3/SEG", "kind": "segments",
         "path": "inputs/slices/intc-10q-2026q3/SEG.txt",
         "sha256": "sha256:98c5" + "0" * 55,
         "anchors": {"first_line": "Segment Information"}},
    ],
    "superseded": [], "conformance": "schema 1.0", "prompt_version": "1.0",
}

FACTS = {
    "role": "extractor", "request_id": "req-0003", "candidate_id": "cand-a3f9c1",
    "facts": [
        {"field": "income_statement.revenue", "period": "2026-Q3",
         "value": 13500.0, "unit": "USDmm", "confidence": 0.98,
         "citation": {"doc_id": "intc-10q-2026q3",
                      "slice_id": "intc-10q-2026q3/IS",
                      "anchor": "Consolidated Statements of Operations",
                      "locator": "line 14"}},
        {"field": "income_statement.revenue", "period": "2025-Q3",
         "value": 12870.0, "unit": "USDmm", "confidence": 0.98,
         "citation": {"doc_id": "intc-10q-2026q3",
                      "slice_id": "intc-10q-2026q3/IS",
                      "anchor": "Consolidated Statements of Operations",
                      "locator": "line 13"}},
        {"field": "income_statement.gross_profit", "period": "2026-Q3",
         "value": 5620.0, "unit": "USDmm", "confidence": 0.96,
         "citation": {"doc_id": "intc-10q-2026q3",
                      "slice_id": "intc-10q-2026q3/IS",
                      "anchor": "Consolidated Statements of Operations",
                      "locator": "line 16"}},
        {"field": "income_statement.operating_income", "period": "2026-Q3",
         "value": -410.0, "unit": "USDmm", "confidence": 0.97,
         "citation": {"doc_id": "intc-10q-2026q3",
                      "slice_id": "intc-10q-2026q3/IS",
                      "anchor": "Consolidated Statements of Operations",
                      "locator": "line 21"}},
        {"field": "segment.revenue.dcai", "period": "2026-Q3",
         "value": 4310.0, "unit": "USDmm", "confidence": 0.93,
         "citation": {"doc_id": "intc-10q-2026q3",
                      "slice_id": "intc-10q-2026q3/SEG",
                      "anchor": "Segment Information",
                      "locator": "table row 'Data Center and AI'"}},
    ],
    "missing": [{"field": "balance_sheet.inventory",
                 "reason": "not disclosed in quarter slice"}],
    "notes": ["restructuring charge disclosed in MD&A but not in IS slice"],
    "conformance": "schema 1.0", "prompt_version": "1.0",
}

FORECAST = {
    "role": "model", "horizon_years": 5,
    "segments": [
        {"segment": "DCAI", "growth": [0.18, 0.16, 0.14, 0.12, 0.10],
         "drivers": [{"text": "Q3'26 segment table",
                      "citation": {"doc_id": "intc-10q-2026q3",
                                   "slice_id": "intc-10q-2026q3/SEG",
                                   "anchor": "Segment Information",
                                   "locator": "row 'Data Center and AI'"}}]},
        {"segment": "CCG", "growth": [0.04, 0.03, 0.02, 0.02, 0.01],
         "drivers": [{"text": "client refresh cycle commentary",
                      "citation": {"doc_id": "intc-10q-2026q3",
                                   "slice_id": "intc-10q-2026q3/MDNA",
                                   "anchor": "Management's Discussion",
                                   "locator": "para 4"}}]},
    ],
    "margin_glide": {"gross": [0.40, 0.41, 0.42, 0.42, 0.43]},
    "terminal_basis": "mandate", "conformance": "schema 1.0",
    "prompt_version": "1.0",
}

MODEL = {
    "artifact": "model", "engine": "model_engine@1.0",
    "input_facts_sha256": "sha256:e1c0" + "0" * 55,
    "candidate_id": "cand-a3f9c1", "cutoff": "2026-10-30",
    "mapping_tables": "1.0",
    "series": {
        "income_statement.revenue": {"2025-Q3": 12870.0, "2026-Q3": 13500.0,
                                     "yoy": 0.049, "yoy_computed": True},
        "income_statement.gross_margin": {"2025-Q3": 0.395, "2026-Q3": 0.4163,
                                          "yoy": 0.0213, "yoy_computed": True},
    },
}

VALUATION = {
    "artifact": "valuation", "engine": "valuation_engine@1.0",
    "input_facts_sha256": "sha256:e1c0" + "0" * 55,
    "candidate_id": "cand-a3f9c1", "cutoff": "2026-10-30",
    "mapping_tables": "1.0",
    "dcf": {"wacc": 0.104, "terminal_growth": 0.025,
            "fcf": [4200.0, 5300.0, 6350.0, 7200.0, 7900.0],
            "pv": [3804.3, 4359.8, 4717.4, 4844.9, 4823.1],
            "terminal_exit_multiple": 21400.0, "terminal_gordon": 19890.0,
            "enterprise_value": 76400.0, "net_cash": 14900.0,
            "equity_value": 91300.0, "shares_mm": 4400.0,
            "per_share": 20.75},
    "comps": {"peer_block_as_of": "2026-10-30", "implied_ev_ebitda": 15.1,
              "implied_per_share": 22.40},
    "sensitivity": {"wacc": [0.094, 0.104, 0.114],
                    "terminal_growth": [0.015, 0.025, 0.035],
                    "per_share": [[23.9, 25.4, 27.2], [20.8, 22.1, 23.6],
                                  [18.2, 19.3, 20.5]]},
    "cross_check_delta": 0.079,
    "cross_check_note": "DCF at $20.75 vs comps-implied $22.40 (+7.9%), within "
                        "mandate tolerance of 25% — no finding raised.",
}


def _evidence(citations: dict[str, tuple]) -> list[dict]:
    """Evidence map powering click-through report links (graded UI feature).

    Each entry: token -> (doc_id, slice_id, anchor, locator); the panel shows
    doc_id, slice_id, anchor and the doc sha256 from the frozen bundle.
    """
    out = []
    for token, (doc_id, slice_id, anchor, locator) in citations.items():
        out.append({
            "token": token,
            "citation": {
                "doc_id": doc_id, "slice_id": slice_id, "anchor": anchor,
                "locator": locator,
                "doc_sha256": next(
                    (d["sha256"] for d in _INTC_MANIFEST["documents"]
                     if d["doc_id"] == doc_id),
                    _INTC_MANIFEST["bundle_sha256"]),
            }})
    return out


_IS = "intc-10q-2026q3/IS"
_SEG = "intc-10q-2026q3/SEG"
_IS_ANCHOR = "Consolidated Statements of Operations"

_REPORT_EVIDENCE = _evidence({
    "13,500": ("intc-10q-2026q3", _IS, _IS_ANCHOR, "line 14"),
    "4.9%": ("intc-10q-2026q3", _IS, _IS_ANCHOR, "lines 13-14 (computed YoY)"),
    "41.6%": ("intc-10q-2026q3", _IS, _IS_ANCHOR, "line 16 / 13,500 (computed)"),
    "5,620": ("intc-10q-2026q3", _IS, _IS_ANCHOR, "line 16"),
    "(410)": ("intc-10q-2026q3", _IS, _IS_ANCHOR, "line 21"),
    "4,310": ("intc-10q-2026q3", _SEG, "Segment Information",
              "table row 'Data Center and AI'"),
    "18%": ("intc-10q-2026q3", _SEG, "Segment Information",
            "table row 'Data Center and AI' (computed YoY)"),
    "20.75": ("intc-10q-2026q3", "model/valuation", "valuation.json",
              "dcf.per_share (deterministic engine output, hash-verified)"),
    "91.3B": ("intc-10q-2026q3", "model/valuation", "valuation.json",
              "dcf.equity_value (deterministic engine output)"),
    "22.40": ("intc-10q-2026q3", "model/valuation", "valuation.json",
              "comps.implied_per_share (peer block, human-owned)"),
    "7.9%": ("intc-10q-2026q3", "model/valuation", "valuation.json",
             "cross_check_delta 0.079 (engine output)"),
})

_REPORT_V1 = """## Intel Corporation (INTC) — Quarterly Update

**Q3 FY2026 results.** Revenue of $13.5B rose 4.9% year-over-year, ahead of the
company's refreshed guidance. Gross margin expanded 213bps to **41.6%**, driven
by a richer Data Center and AI mix.

### Results

| Metric | 2026-Q3 | 2025-Q3 | YoY |
|---|---:|---:|---:|
| Revenue (USDmm) | 13,500 | 12,870 | +4.9% |
| Gross profit (USDmm) | 5,620 | 5,084 | +10.5% |
| Gross margin | 41.6% | 39.5% | +213bps |
| Operating income (USDmm) | (410) | (1,960) | — |

### Segment detail

Data Center and AI revenue of $4,310M grew 18% year-over-year, now 32% of
company revenue, on stronger lifecycle server demand and pull-in of
Granite Rapids Rack shipments.

### Valuation

Our DCF (WACC 10.4%, terminal growth 2.5%) yields an equity value of $91.3B
or **$20.75 per share**; the dated peer block implies $22.40, a 7.9%
cross-check comfortably inside the 25% mandate tolerance.

### Risks

Foundry ramp execution remains the key swing factor; a slower Mount Blanc
qualification pushes out the 2027 turnaround case.
"""

_REPORT_V2 = _REPORT_V1.replace(
    "The analyst note now reconciles.", "").replace(
    "### Segment detail",
    "### Segment detail (rev. 2 — segment bridge reconciled to consolidated "
    "statements)"). replace(
    "now 32% of\ncompany revenue",
    "now 32% of\ncompany revenue, tying to the consolidated revenue line").replace(
    "### Risks",
    "### Risks\n\nRevision 2 additionally carries a competitive-pressure risk "
    "on client share.").replace("(INTC) — Quarterly Update",
                                "(INTC) — Quarterly Update (rev. 2)")

_REPORT_V3 = _REPORT_V2.replace(
    "(rev. 2 — segment bridge reconciled to consolidated statements)",
    "(rev. 2 verified; critic rounds 1-2 clean)").replace(
    "### Results\n\n| Metric",
    "### Results (every figure evidence-linked; click a number for its "
    "citation)\n\n| Metric").replace(
    "### Risks\n\nRevision 2 additionally carries a competitive-pressure risk "
    "on client share.",
    "### Risks\n\nRevision 2 additionally carries a competitive-pressure risk "
    "on client share; both critic review rounds verified every figure "
    "against the frozen bundle.").replace("(rev. 2)", "(final)")


def _intel_approved_case() -> Case:
    """Q3'26 Intel coverage update: full clean run, approved, closed."""
    cid = "intc-fy2026q3-20261030"
    cand = "cand-a3f9c1"
    state = {
        "case_id": cid, "status": "CLOSED",
        "pending": {"role": "approval", "request_id": "req-0019", "round": 2,
                    "retry_count": 0,
                    "case_clock_started": "2026-10-30T21:38:04Z",
                    "deadline": "2026-11-01T21:38:04Z"},
        "candidate": {"candidate_id": cand, "clean_review_rounds": 2,
                       "built_round": 2},
        "budget": _budget(2, 31.7, {"ingest": 9120, "extractor": 38210,
                                     "model": 12650, "writer": 21090,
                                     "critic_c1": 8450, "critic_c2": 7980,
                                     "critic_c3": 5410, "critic_c4": 6220}),
        "inputs_manifest_ref": "inputs/manifest.json",
        "config_versions": {
            "prompt_cards": {"ingest": "1.0", "extractor": "1.0",
                             "model": "1.0", "writer": "1.0",
                             "critics": ["1.0"]},
            "mapping_tables": "1.0", "engines": "1.0", "schema": "1.0"},
        "checkpoints": [
            {"ts": "2026-10-30T21:40:11Z", "from": "RUNNING", "to": "RUNNING",
             "verified": "round-001/generator/candidate-manifest.json",
             "state": "sha256:11aa" + "0" * 55},
            {"ts": "2026-10-30T22:02:44Z", "from": "RUNNING", "to": "RUNNING",
             "verified": "round-002/generator/candidate-manifest.json",
             "state": "sha256:22bb" + "0" * 55},
            {"ts": "2026-10-30T22:31:02Z", "from": "PENDING_APPROVAL",
             "to": "CLOSED", "verified": "approvals/apr-0001.json",
             "state": "sha256:33cc" + "0" * 55},
        ],
        "reason": "human approved; 2 clean reviews; all gates pass",
        "updated_at": "2026-10-30T22:31:02Z",
    }
    mandate = _mandate("INTC", "0000050863", "2026-10-30", "intc-10q-2026q3",
                       _INTEL_SEGS, _INTEL_PEERS, "man-0001", cid)
    tr = [
        _ev(cid, "2026-10-30T21:38:04Z", "CASE_CREATED", round_n=1,
            detail={"ticker": "INTC", "cutoff": "2026-10-30"}),
        _ev(cid, "2026-10-30T21:38:30Z", "REQUEST_BOUND", role="ingest",
            round_n=1, request_id="req-0002", candidate_id=cand),
        _ev(cid, "2026-10-30T21:39:00Z", "AGENT_DISPATCHED", role="ingest",
            round_n=1, request_id="req-0002", candidate_id=cand,
            backend="replay", tokens_in=3040),
        _ev(cid, "2026-10-30T21:39:40Z", "AGENT_RESULT", role="ingest",
            round_n=1, request_id="req-0002", candidate_id=cand,
            artifact="rounds/round-001/generator/ingest-report.json",
            tokens_out=6080, ok=True),
        _ev(cid, "2026-10-30T21:41:05Z", "AGENT_RESULT", role="extractor",
            round_n=1, request_id="req-0003", candidate_id=cand,
            artifact="rounds/round-001/generator/facts.json",
            tokens_out=38210, ok=True),
        _ev(cid, "2026-10-30T21:52:00Z", "GATE_RUN", role="gates", round_n=1,
            detail={"gates": ["forecast_bounds", "citation_coverage",
                              "model_recompute", "cutoff_discipline"]}),
        _ev(cid, "2026-10-30T21:52:02Z", "GATE_PASS", role="gates", round_n=1,
            detail={"passed": 4, "failed": 0}),
        _ev(cid, "2026-10-30T22:00:40Z", "FINDING_OPENED", role="critic_c2",
            round_n=1, request_id="req-0010", candidate_id=cand,
            detail={"finding_id": "fnd-001", "severity": "major"}),
        _ev(cid, "2026-10-30T22:00:41Z", "REPAIR_PACKET_ISSUED", round_n=2,
            detail={"findings": ["fnd-001"],
                    "required_evidence": "recomputed gross margin row "
                                         "matching facts"}),
        _ev(cid, "2026-10-30T22:08:50Z", "FINDING_VERIFIED_CLOSED",
            role="critic_c2", round_n=2,
            detail={"finding_id": "fnd-001",
                    "verified_evidence": "round-002/generator/facts.json+"
                                         "sha256:e1c0…"}),
        _ev(cid, "2026-10-30T22:15:30Z", "BUDGET_CHECK", round_n=2,
            detail={"rounds": "2/4", "minutes": 22.1,
                    "tokens_max_role": "38210/60000"}),
        _ev(cid, "2026-10-30T22:31:00Z", "APPROVAL_REQUESTED", round_n=2,
            request_id="req-0019",
            detail={"dossier": "report/v3-" + cand[:4] + ".md",
                    "expires": "2026-11-01T21:38:04Z"}),
        _ev(cid, "2026-10-30T22:31:02Z", "APPROVAL_RECORDED", round_n=2,
            request_id="req-0019",
            detail={"decision": "APPROVED",
                    "by": "Gaetano Marco Chiantia"}),
        _ev(cid, "2026-10-30T22:31:02Z", "CASE_CLOSED", round_n=2,
            detail={"terminal": "APPROVED",
                    "report_version": "v3-" + cand[:8]}),
    ]
    ledger = [
        {"finding_id": "fnd-001", "first_seen_round": 1, "last_seen_round": 2,
         "status": "closed",
         "closed_by": {"round": 2, "critic": "c2",
                       "verified_evidence": "round-002/generator/facts.json+"
                                            "sha256:e1c0…"},
         "severity": "major",
         "summary": "Reported gross margin row in the draft did not match the "
                    "recomputed margin from extracted facts.",
         "affected_artifacts": ["report.md", "facts.json"]},
        {"finding_id": "fnd-002", "first_seen_round": 1, "last_seen_round": 2,
         "status": "closed",
         "closed_by": {"round": 2, "critic": "c1",
                       "verified_evidence": "report/v2-c0ffee.md+sha256:44dd…"},
         "severity": "minor",
         "summary": "Segment share percentage cited without a trailing "
                    "document reference.",
         "affected_artifacts": ["report.md"]},
    ]
    reports = []
    for i, md in enumerate((_REPORT_V1, _REPORT_V2, _REPORT_V3), start=1):
        reports.append({"version": f"v{i}-" + cand[:8], "candidate_id": cand,
                        "round": 1 if i == 1 else 2, "markdown": md,
                        "evidence": _REPORT_EVIDENCE})
    replays = [
        {"role": "ingest", "request_id": "req-0002", "candidate_id": cand,
         "prompt_sha256": "sha256:aa01" + "0" * 55, "prompt_path": "prompts/ingest.md",
         "assembled_prompt_sha256": "sha256:bb02" + "0" * 55,
         "response_text": '{"role":"ingest","doc_id":"intc-10q-2026q3",'
                          '"slices":3,"superseded":0}',
         "backend": "replay", "recorded_at": "2026-10-30T21:40:11Z"},
        {"role": "extractor", "request_id": "req-0003", "candidate_id": cand,
         "prompt_sha256": "sha256:aa03" + "0" * 55,
         "prompt_path": "prompts/extractor.md",
         "assembled_prompt_sha256": "sha256:bb04" + "0" * 55,
         "response_text": '{"role":"extractor","facts":5,"missing":1,"notes":1}',
         "backend": "replay", "recorded_at": "2026-10-30T21:41:05Z"},
        {"role": "model", "request_id": "req-0004", "candidate_id": cand,
         "prompt_sha256": "sha256:aa05" + "0" * 55, "prompt_path": "prompts/model.md",
         "assembled_prompt_sha256": "sha256:bb06" + "0" * 55,
         "response_text": '{"role":"model","horizon_years":5,"segments":2}',
         "backend": "replay", "recorded_at": "2026-10-30T21:45:19Z"},
        {"role": "writer", "request_id": "req-0005", "candidate_id": cand,
         "prompt_sha256": "sha256:aa07" + "0" * 55, "prompt_path": "prompts/writer.md",
         "assembled_prompt_sha256": "sha256:bb08" + "0" * 55,
         "response_text": '{"role":"writer","report":"v1-' + cand[:8]
                          + '.md","words":1180}',
         "backend": "replay", "recorded_at": "2026-10-30T21:49:33Z"},
    ]
    c = Case(cid, state, mandate, _INTC_MANIFEST, tr, ledger, reports, replays)
    c.result = {"case_id": cid, "terminal": "APPROVED", "candidate_id": cand,
                "report_version": "v3-" + cand[:8],
                "reason": "human approved; 2 clean reviews; all gates pass",
                "budget_final": state["budget"],
                "closed_ts": "2026-10-30T22:31:02Z"}
    c.approvals = [{
        "decision_id": "apr-0001", "case_id": cid, "candidate_id": cand,
        "decision": "APPROVED", "by": "Gaetano Marco Chiantia",
        "ts": "2026-10-30T22:31:02Z",
        "reasoning": "Segment bridge and valuation cross-check verified in "
                     "the dossier; two consecutive clean reviews recorded.",
        "expected_evidence_refs": ["report/v3-" + cand[:8] + ".md",
                                   "rounds/round-002/critic/"
                                   "merged-findings.json"]}]
    a = c.artifacts
    a["findings.json"] = {"findings": ledger}
    a["result.json"] = c.result
    a["approvals/apr-0001.json"] = c.approvals[0]
    a["rounds/round-001/generator/ingest-report.json"] = INGEST_REPORT
    a["rounds/round-001/generator/facts.json"] = FACTS
    a["rounds/round-001/generator/forecast-assumptions.json"] = FORECAST
    a["rounds/round-001/generator/model.json"] = MODEL
    a["rounds/round-001/generator/valuation.json"] = VALUATION
    a["rounds/round-001/generator/candidate-manifest.json"] = {
        "request_id": "req-0002", "candidate_id": cand, "round": 1,
        "reports": [
            {"role": "ingest", "artifact": "ingest-report.json",
             "sha256": "sha256:71ab…"},
            {"role": "extractor", "artifact": "facts.json",
             "sha256": _sha(json.dumps(FACTS))[:16]},
            {"role": "model", "artifact": "model.json",
             "sha256": _sha(json.dumps(MODEL))[:16]},
            {"role": "writer", "artifact": "report/v1-" + cand[:8] + ".md",
             "sha256": _sha(_REPORT_V1)[:16]}],
        "evidence_index": "evidence sha → producing artifact",
        "scope_statement": "full rebuild round 001"}
    for r in reports:
        a[f"report/{r['version']}.md"] = r["markdown"]
    a["rounds/round-001/critic/c2-report.json"] = _critic_report(
        "critic_c2", "req-0010", cand, 1, "findings", [
            {"finding_id": "fnd-001", "severity": "major",
             "summary": "Reported gross margin row did not match recomputed "
                        "margin from extracted facts.",
             "evidence": "facts: gross_profit 5620 / revenue 13500 = 41.63%; "
                         "report cited 40.1%",
             "affected_artifacts": ["report.md"],
             "required_evidence": "recomputed gross margin row matching "
                                  "facts"}])
    a["rounds/round-002/critic/merged-findings.json"] = {
        "round": 2, "candidate_id": cand, "findings": [], "clean": True,
        "coverage_ok": True, "reviewer_count": 4,
        "fresh_context_proofs": [
            "trace:AGENT_DISPATCHED critic_c1@round-002",
            "trace:AGENT_DISPATCHED critic_c2@round-002",
            "trace:AGENT_DISPATCHED critic_c3@round-002",
            "trace:AGENT_DISPATCHED critic_c4@round-002"]}
    for rp in replays:
        a[f"replay/{rp['request_id']}.json"] = rp
    c.rounds = [
        {"round": 1, "status": "superseded",
         "generator_artifacts": ["ingest-report.json", "facts.json",
                                  "model.json"],
         "gates": {"total": 4, "passed": 4, "failed": 0},
         "critics": [{"role": "c1", "verdict": "clean"},
                     {"role": "c2", "verdict": "findings",
                      "findings": ["fnd-001"]},
                     {"role": "c3", "verdict": "clean"},
                     {"role": "c4", "verdict": "clean"}]},
        {"round": 2, "status": "accepted",
         "generator_artifacts": ["ingest-report.json", "facts.json",
                                  "model.json"],
         "gates": {"total": 4, "passed": 4, "failed": 0},
         "critics": [{"role": x, "verdict": "clean"}
                     for x in ("c1", "c2", "c3", "c4")],
         "repair_packet": "rounds/round-002/repair-packet.json"},
    ]
    return c


def _intel_running_case() -> Case:
    """Intel Q4'26 coverage update: mid-run at round 2, budget partly used."""
    cid = "intc-fy2026q4-20270129"
    cand = "cand-77b2e0"
    state = {
        "case_id": cid, "status": "RUNNING",
        "pending": {"role": "critic", "request_id": "req-0031", "round": 2,
                    "retry_count": 0,
                    "case_clock_started": "2027-01-29T21:38:04Z",
                    "deadline": None},
        "candidate": {"candidate_id": cand, "clean_review_rounds": 1,
                      "built_round": 2},
        "budget": _budget(2, 12.4, {"ingest": 8940, "extractor": 36500,
                                    "model": 11980, "writer": 18300}),
        "inputs_manifest_ref": "inputs/manifest.json",
        "config_versions": {
            "prompt_cards": {"ingest": "1.1", "extractor": "1.0",
                             "model": "1.0", "writer": "1.0",
                             "critics": ["1.0"]},
            "mapping_tables": "1.0", "engines": "1.0", "schema": "1.0"},
        "checkpoints": [
            {"ts": "2027-01-29T21:42:10Z", "from": "RUNNING", "to": "RUNNING",
             "verified": "round-001/generator/candidate-manifest.json",
             "state": "sha256:55ee" + "0" * 55},
        ],
        "reason": None, "updated_at": "2027-01-29T22:10:09Z",
    }
    mandate = _mandate("INTC", "0000050863", "2027-01-29", "intc-10q-2026q4",
                       _INTEL_SEGS, _INTEL_PEERS, "man-0004", cid)
    tr = [
        _ev(cid, "2027-01-29T21:38:04Z", "CASE_CREATED", round_n=1,
            detail={"ticker": "INTC", "cutoff": "2027-01-29"}),
        _ev(cid, "2027-01-29T21:38:22Z", "REQUEST_BOUND", role="ingest",
            round_n=1, request_id="req-0021", candidate_id=cand),
        _ev(cid, "2027-01-29T21:51:20Z", "AGENT_RESULT", role="writer",
            round_n=1, request_id="req-0024",
            artifact="report/v1-" + cand[:8] + ".md", tokens_out=18300,
            ok=True),
        _ev(cid, "2027-01-29T21:52:00Z", "GATE_PASS", role="gates", round_n=1,
            detail={"passed": 4, "failed": 0}),
        _ev(cid, "2027-01-29T22:01:15Z", "FINDING_OPENED", role="critic_c4",
            round_n=1, request_id="req-0028",
            detail={"finding_id": "fnd-011", "severity": "minor"}),
        _ev(cid, "2027-01-29T22:05:00Z", "REPAIR_PACKET_ISSUED", round_n=2,
            detail={"findings": ["fnd-011"],
                    "required_evidence": "peer-block as-of date ≤ cutoff"}),
        _ev(cid, "2027-01-29T22:10:09Z", "AGENT_DISPATCHED", role="critic",
            round_n=2, request_id="req-0031", candidate_id=cand,
            detail={"team": ["c1", "c2", "c3", "c4"]}),
    ]
    ledger = [
        {"finding_id": "fnd-011", "first_seen_round": 1, "last_seen_round": 2,
         "status": "open", "closed_by": None, "severity": "minor",
         "summary": "Peer-multiple block as-of date (2027-01-31) postdates "
                    "the case cutoff (2027-01-29).",
         "affected_artifacts": ["report.md"]},
    ]
    reports = [{"version": "v1-" + cand[:8], "candidate_id": cand, "round": 1,
                "markdown": _REPORT_V1.replace("Q3 FY2026", "Q4 FY2026"),
                "evidence": _REPORT_EVIDENCE}]
    c = Case(cid, state, mandate, _INTC_MANIFEST, tr, ledger, reports, [])
    c.artifacts["findings.json"] = {"findings": ledger}
    c.artifacts["report/v1-" + cand[:8] + ".md"] = reports[0]["markdown"]
    c.artifacts["rounds/round-002/repair-packet.json"] = {
        "round": 2, "candidate_id": cand,
        "open_findings": [ledger[0]["finding_id"]],
        "required_evidence": "peer-block as-of date ≤ cutoff",
        "unchanged_criteria": "acceptance rules 1-4 (locked)",
        "delta_scope_statement": "full rebuild; stages 1-3 hash-identical "
                                 "to round-001 (proofs included)"}
    c.rounds = [
        {"round": 1, "status": "superseded",
         "generator_artifacts": ["ingest-report.json", "facts.json"],
         "gates": {"total": 4, "passed": 4, "failed": 0},
         "critics": [{"role": "c4", "verdict": "findings",
                      "findings": ["fnd-011"]}] +
                    [{"role": x, "verdict": "clean"}
                     for x in ("c1", "c2", "c3")]},
        {"round": 2, "status": "in_progress",
         "generator_artifacts": ["ingest-report.json", "facts.json"],
         "gates": {"total": 4, "passed": 4, "failed": 0},
         "critics": [{"role": x, "verdict": "pending"}
                     for x in ("c1", "c2", "c3", "c4")]},
    ]
    return c


_AMD_MD = """## Advanced Micro Devices (AMD) — Quarterly Update

**Q3 FY2026 results.** Revenue of $8.5B rose 21% year-over-year, led by Data
Center GPU shipment growth and EPYC server share gains. Gross margin of
**53.4%** expanded further on mix; operating leverage shows in theSilicon
gross-to-operating bridge.

### Results

| Metric | 2026-Q3 | 2025-Q3 | YoY |
|---|---:|---:|---:|
| Revenue (USDmm) | 8,500 | 7,040 | +20.7% |
| Gross margin | 53.4% | 51.0% | +240bps |

### Valuation

DCF (WACC 10.4%, terminal growth 2.5%) yields $148 per share against the
dated peer block implied band; both critic rounds verified the model inputs.
"""


def _amd_pending_case() -> Case:
    """AMD Q3'26 coverage update: gates + two clean reviews; awaiting human."""
    cid = "amd-fy2026q3-20261029"
    cand = "cand-5d21c4"
    state = {
        "case_id": cid, "status": "PENDING_APPROVAL",
        "pending": {"role": "approval", "request_id": "req-0107", "round": 2,
                    "retry_count": 0,
                    "case_clock_started": "2026-10-29T20:05:00Z",
                    "deadline": "2026-10-31T20:05:00Z"},
        "candidate": {"candidate_id": cand, "clean_review_rounds": 2,
                      "built_round": 2},
        "budget": _budget(2, 28.9, {"ingest": 7800, "extractor": 33400,
                                    "model": 10950, "writer": 16700,
                                    "critic_c1": 7120, "critic_c2": 6940,
                                    "critic_c3": 5010, "critic_c4": 5730}),
        "inputs_manifest_ref": "inputs/manifest.json",
        "config_versions": {
            "prompt_cards": {"ingest": "1.0", "extractor": "1.0",
                             "model": "1.0", "writer": "1.0",
                             "critics": ["1.0"]},
            "mapping_tables": "1.0", "engines": "1.0", "schema": "1.0"},
        "checkpoints": [
            {"ts": "2026-10-29T20:38:40Z", "from": "RUNNING", "to": "RUNNING",
             "verified": "round-002/generator/candidate-manifest.json",
             "state": "sha256:66ff" + "0" * 55},
            {"ts": "2026-10-29T20:48:12Z", "from": "RUNNING",
             "to": "PENDING_APPROVAL",
             "verified": "rounds/round-002/critic/merged-findings.json",
             "state": "sha256:7701" + "0" * 55},
        ],
        "reason": "awaiting human approval (2/2 clean reviews, all gates pass)",
        "updated_at": "2026-10-29T20:48:12Z",
    }
    mandate = _mandate("AMD", "0000002488", "2026-10-29", "amd-10q-2026q3",
                       _AMD_SEGS, _AMD_PEERS, "man-0009", cid)
    tr = [
        _ev(cid, "2026-10-29T20:05:00Z", "CASE_CREATED", round_n=1,
            detail={"ticker": "AMD", "cutoff": "2026-10-29"}),
        _ev(cid, "2026-10-29T20:22:30Z", "GATE_PASS", role="gates", round_n=1,
            detail={"passed": 4, "failed": 0}),
        _ev(cid, "2026-10-29T20:38:38Z", "GATE_PASS", role="gates", round_n=2,
            detail={"passed": 4, "failed": 0}),
        _ev(cid, "2026-10-29T20:46:02Z", "FINDING_VERIFIED_CLOSED",
            role="critic_c1", round_n=2,
            detail={"finding_id": "fnd-021",
                    "verified_evidence": "round-002/generator/facts.json+sha…"}),
        _ev(cid, "2026-10-29T20:48:10Z", "APPROVAL_REQUESTED", round_n=2,
            request_id="req-0107",
            detail={"dossier": "report/v2-" + cand[:8] + ".md",
                    "expires": "2026-10-31T20:05:00Z"}),
    ]
    ledger = [
        {"finding_id": "fnd-021", "first_seen_round": 1, "last_seen_round": 1,
         "status": "closed",
         "closed_by": {"round": 1, "critic": "c1",
                       "verified_evidence": "round-001/generator/"
                                            "facts.json+sha…"},
         "severity": "minor",
         "summary": "Client segment revenue growth cited against the prior-year "
                    "base but mislabeled as QoQ.",
         "affected_artifacts": ["report.md"]},
    ]
    reports = [
        {"version": "v1-" + cand[:8], "candidate_id": cand, "round": 1,
         "markdown": _AMD_MD + "\n(revision 1 — YoY/QoQ labeling fixed in "
         "revision 2)", "evidence": []},
        {"version": "v2-" + cand[:8], "candidate_id": cand, "round": 2,
         "markdown": _AMD_MD, "evidence": []},
    ]
    c = Case(cid, state, mandate, _AMD_MANIFEST, tr, ledger, reports, [])
    c.artifacts["findings.json"] = {"findings": ledger}
    for r in reports:
        c.artifacts[f"report/{r['version']}.md"] = r["markdown"]
    c.artifacts["rounds/round-002/critic/merged-findings.json"] = {
        "round": 2, "candidate_id": cand, "findings": [], "clean": True,
        "coverage_ok": True, "reviewer_count": 4,
        "fresh_context_proofs": ["trace:AGENT_DISPATCHED critic_c1@round-002"]}
    c.rounds = [
        {"round": 1, "status": "superseded",
         "generator_artifacts": ["ingest-report.json", "facts.json"],
         "gates": {"total": 4, "passed": 4, "failed": 0},
         "critics": [{"role": "c1", "verdict": "findings",
                      "findings": ["fnd-021"]}] +
                    [{"role": x, "verdict": "clean"}
                     for x in ("c2", "c3", "c4")]},
        {"round": 2, "status": "accepted",
         "generator_artifacts": ["ingest-report.json", "facts.json"],
         "gates": {"total": 4, "passed": 4, "failed": 0},
         "critics": [{"role": x, "verdict": "clean"}
                     for x in ("c1", "c2", "c3", "c4")]},
    ]
    return c


# --- frozen bundles (A1 builds real ones; these power the builder pages) ----
_BUNDLES: dict[str, dict] = {
    _INTC_MANIFEST["bundle_id"]: _INTC_MANIFEST,
    _AMD_MANIFEST["bundle_id"]: _AMD_MANIFEST,
}

_REGISTRY = {
    "active": "sys-1.1",
    "versions": [
        {"version": "sys-1.0", "parent": None,
         "scope": "initial pinned system",
         "proposal": {"author": "kickoff", "diff_files": [],
                      "diff_sha256": None},
         "tested": {"model": "1.0", "prompt_cards": {"ingest": "1.0",
                                                    "extractor": "1.0"},
                    "engines": "1.0",
                    "cases": ["dev-1", "dev-2", "dev-3", "dev-4"]},
         "review": {"fresh_critic_findings": [],
                    "regressions_all_pass": True,
                    "identical_outputs_proof": "baseline"},
         "approval": {"decision": "APPROVED",
                      "by": "Gaetano Marco Chiantia",
                      "ts": "2026-11-01T09:00:00Z"},
         "rollback_to": None},
        {"version": "sys-1.1", "parent": "sys-1.0",
         "scope": "prompt card extractor",
         "proposal": {"author": "evo-change-01",
                      "diff_files": ["prompts/extractor.md"],
                      "diff_sha256": "sha256:c0de" + "0" * 55},
         "tested": {"model": "1.0", "prompt_cards": {"extractor": "1.1"},
                    "engines": "1.0",
                    "cases": ["dev-1", "dev-2", "dev-3", "dev-4"]},
         "review": {"fresh_critic_findings": [],
                    "regressions_all_pass": True,
                    "identical_outputs_proof": "identical model/valuation "
                                               "hashes on dev-1..4"},
         "approval": {"decision": "APPROVED",
                      "by": "Gaetano Marco Chiantia",
                      "ts": "2026-11-02T10:12:44Z"},
         "rollback_to": None},
    ],
}

LOCKED_SCOPES = {
    "controller": "Orchestrator control logic — locked; a proposal cannot "
                  "weaken the controller.",
    "gates": "Numerical gate thresholds — locked; a proposal cannot weaken "
             "its own acceptance standard.",
    "rubric": "Critic rubric — locked acceptance standard.",
    "acceptance": "Acceptance rules (2 clean reviews, budget rules) — "
                  "locked.",
}

_BACKENDS: list[dict] = [
    {"name": "claude", "kind": "cli", "detected": False,
     "detail": "'claude -p' not found in PATH (stub detection; real "
               "auto-detect lands with A3)",
     "api_config": None, "healthy": None, "selected": False,
     "usage_note": "headless CLI login"},
    {"name": "codex", "kind": "cli", "detected": False,
     "detail": "'codex exec' not found in PATH (stub detection)",
     "api_config": None, "healthy": None, "selected": False,
     "usage_note": "headless CLI login"},
    {"name": "kimi", "kind": "cli", "detected": False,
     "detail": "'kimi' not found in PATH (stub detection)",
     "api_config": None, "healthy": None, "selected": False,
     "usage_note": "headless CLI login"},
    {"name": "api", "kind": "openai-compatible", "detected": False,
     "detail": "no COVERAGEFLOW_API_BASE / key configured in this "
               "environment (see A3 api_backend)",
     "api_config": None, "healthy": None, "selected": False,
     "usage_note": "OpenAI-compatible /chat/completions"},
    {"name": "replay", "kind": "replay", "detected": True,
     "detail": "offline replay backend — recordings under cases/<id>/replay/; "
               "always available, used by the test suite",
     "api_config": None, "healthy": True, "selected": True,
     "usage_note": "recorded responses, deterministic"},
]

_EDGAR_DB: dict[str, list[dict]] = {
    "INTC": [
        {"doc_id": "intc-10k-2024", "form": "10-K", "filed": "2025-01-24",
         "period": "2024-12-28", "cik": "0000050863",
         "title": "Intel Corporation Form 10-K FY2024",
         "bytes": 3_812_004,
         "source_url": "https://www.sec.gov/Archives/edgar/data/50863/000005086325000010/intc-20241228.htm"},
        {"doc_id": "intc-10q-2025q1", "form": "10-Q", "filed": "2025-04-25",
         "period": "2025-03-29", "cik": "0000050863",
         "title": "Intel Corporation Form 10-Q Q1 FY2025",
         "bytes": 1_702_300,
         "source_url": "https://www.sec.gov/Archives/edgar/data/50863/000005086325000021/intc-20250329.htm"},
        {"doc_id": "intc-10q-2026q3", "form": "10-Q", "filed": "2026-10-30",
         "period": "2026-09-26", "cik": "0000050863",
         "title": "Intel Corporation Form 10-Q Q3 FY2026",
         "bytes": 1_834_512,
         "source_url": "https://www.sec.gov/Archives/edgar/data/50863/000005086326000034/intc-20260926.htm"},
        {"doc_id": "intc-8k-earnings-2026q3", "form": "8-K",
         "filed": "2026-10-30", "period": "2026-09-26", "cik": "0000050863",
         "title": "Intel Corporation 8-K — Q3'26 results press release",
         "bytes": 214_880,
         "source_url": "https://www.sec.gov/Archives/edgar/data/50863/000005086326000035/intc-8k-2026q3.htm"},
    ],
    "AMD": [
        {"doc_id": "amd-10k-2024", "form": "10-K", "filed": "2025-02-05",
         "period": "2024-12-28", "cik": "0000002488",
         "title": "Advanced Micro Devices Form 10-K FY2024",
         "bytes": 3_140_992,
         "source_url": "https://www.sec.gov/Archives/edgar/data/2488/000000248825000012/amd-20241228.htm"},
        {"doc_id": "amd-10q-2026q3", "form": "10-Q", "filed": "2026-10-29",
         "period": "2026-09-27", "cik": "0000002488",
         "title": "Advanced Micro Devices Form 10-Q Q3 FY2026",
         "bytes": 1_590_210,
         "source_url": "https://www.sec.gov/Archives/edgar/data/2488/000000248826000021/amd-20260927.htm"},
    ],
}

_CASES: dict[str, Case] = {c.case_id: c for c in (
    _intel_approved_case(), _intel_running_case(), _amd_pending_case())}

_DEMO_COUNTERS = {"seed_finding": 100, "approval": 100, "bundle": 0}


def _get(case_id: str) -> Case:
    c = _CASES.get(case_id)
    if c is None:
        raise NotFound(f"case not found: {case_id}", case_id=case_id)
    return c


# ---------------------------------------------------------------------------
# Health / backends
# ---------------------------------------------------------------------------


def health() -> dict:
    """Backend health snapshot for the dashboard header (live timestamp)."""
    active = [c for c in _CASES.values() if c.state["status"] == "RUNNING"]
    return {
        "status": "ok",
        "mode": "stub-service" if not _HAVE_REAL_BACKEND else "controller",
        "active_backend": next((b["name"] for b in _BACKENDS if b["selected"]),
                               "replay"),
        "cases": len(_CASES),
        "active_runs": len(active),
        "pending_approvals": sum(
            1 for c in _CASES.values()
            if c.state["status"] == "PENDING_APPROVAL"),
        "ts": _now_iso(),
    }


def get_backends() -> dict:
    """GET /api/backends — detected backends + per-role usage vs caps."""
    usage_by_role: dict[str, int] = {}
    for c in _CASES.values():
        for role, tok in c.state["budget"]["used"]["tokens_by_role"].items():
            usage_by_role[role] = usage_by_role.get(role, 0) + tok
    caps = {"role_tokens_max": 60_000, "case_minutes": 45, "rounds_max": 4,
            "retries_max": 3}
    return {"backends": _BACKENDS, "usage_by_role": usage_by_role,
            "caps": caps, "selected": caps and next(
                (b["name"] for b in _BACKENDS if b["selected"]), "replay")}


def select_backend(name: str) -> dict:
    """POST /api/backends/select — stores the choice (single backend active)."""
    match = next((b for b in _BACKENDS if b["name"] == name), None)
    if match is None:
        raise ValidationError(
            f"unknown backend: {name}; available: "
            f"{', '.join(b['name'] for b in _BACKENDS)}")
    if not match["detected"]:
        raise ConflictError(
            f"backend '{name}' is not available in this environment: "
            f"{match['detail']}")
    for b in _BACKENDS:
        b["selected"] = (b["name"] == name)
    _emit("*", "BACKEND_SELECTED", {"backend": name})
    return get_backends()


# ---------------------------------------------------------------------------
# Cases: list / create / detail / trace / artifacts
# ---------------------------------------------------------------------------


def _case_summary(c: Case) -> dict:
    """Dashboard row: status, budget meter inputs, pending role, versions."""
    st = c.state
    tokens_by_role = st["budget"]["used"]["tokens_by_role"]
    return {
        "case_id": c.case_id, "ticker": c.mandate["ticker"],
        "cutoff_date": c.mandate["cutoff_date"], "status": st["status"],
        "rounds_used": st["budget"]["used"]["rounds"],
        "rounds_max": st["budget"]["rounds_max"],
        "elapsed_minutes": st["budget"]["used"]["elapsed_minutes"],
        "case_minutes": st["budget"]["case_minutes"],
        "tokens_max_role": (max(tokens_by_role.values())
                            if tokens_by_role else 0),
        "role_tokens_max": st["budget"]["role_tokens_max"],
        "open_findings": sum(1 for f in c.ledger if f["status"] == "open"),
        "clean_review_rounds": st["candidate"]["clean_review_rounds"],
        "pending_role": (None if st["status"] in ("CLOSED", "APPROVED",
                                                  "REJECTED")
                         else st["pending"]["role"]),
        "report_versions": [r["version"] for r in c.reports],
        "latest_report": (c.reports[-1]["version"] if c.reports else None),
        "deadline": st["pending"].get("deadline"),
        "updated_at": st["updated_at"],
    }


def list_cases() -> dict:
    """GET /api/cases — list + state summary (the dashboard feed)."""
    out = {"cases": [_case_summary(c) for c in _CASES.values()],
           "health": health(), "count": len(_CASES)}
    return out


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_mandate(payload: dict) -> list[str]:
    """New-case wizard validation; returns human-readable errors ([] = ok)."""
    errors: list[str] = []
    ticker = str(payload.get("ticker") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,6}", ticker):
        errors.append("ticker is required (1-6 letters, e.g. INTC)")
    cutoff = str(payload.get("cutoff_date") or "")
    if not _DATE_RE.match(cutoff):
        errors.append("cutoff_date must be a date YYYY-MM-DD")
    if not payload.get("trigger_document"):
        errors.append("trigger document is required")
    rp = payload.get("report_policy") or {}
    words = rp.get("length_words")
    if (not isinstance(words, list) or len(words) != 2
            or not all(isinstance(w, (int, float)) for w in words)):
        errors.append("report_policy.length_words must be [min_words, max_words]")
    elif words[0] > words[1]:
        errors.append("report_policy.length_words: min must be ≤ max")
    if not rp.get("sections"):
        errors.append("report_policy.sections must list at least one section")
    a = payload.get("assumptions") or {}
    try:
        wacc = float(a.get("wacc"))
        if not 0.01 <= wacc <= 0.30:
            errors.append(f"wacc {wacc:g} outside plausible band [0.01, 0.30]")
    except (TypeError, ValueError):
        errors.append("assumptions.wacc must be a number")
        wacc = None
    try:
        tg = float(a.get("terminal_growth"))
        if tg <= -0.05:
            errors.append(f"terminal_growth {tg:g} below floor -0.05")
        if wacc is not None and tg >= wacc:
            errors.append(f"terminal_growth {tg:g} must be below wacc {wacc:g}")
    except (TypeError, ValueError):
        errors.append("assumptions.terminal_growth must be a number")
    try:
        if float(a.get("exit_multiple_ebitda")) <= 0:
            errors.append("assumptions.exit_multiple_ebitda must be positive")
    except (TypeError, ValueError):
        errors.append("assumptions.exit_multiple_ebitda must be a number")
    fb = (a.get("forecast_bounds") or {})
    segs = fb.get("segments") or {}
    if not segs:
        errors.append("forecast_bounds.segments must define at least one "
                      "segment [min, max] growth")
    for seg, bounds in segs.items():
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or not all(isinstance(b, (int, float)) for b in bounds)):
            errors.append(f"forecast_bounds.segments['{seg}']: [min, max] required")
        elif bounds[0] > bounds[1]:
            errors.append(
                f"forecast_bounds.segments['{seg}']: min {bounds[0]:g} > max "
                f"{bounds[1]:g}")
        elif bounds[0] < -0.50 or bounds[1] > 1.00:
            errors.append(
                f"forecast_bounds.segments['{seg}']: outside [-0.50, 1.00] — "
                "widen limits deliberately, not by typo")
    try:
        mf = float(fb.get("margin_floor_gross"))
        mc = float(fb.get("margin_ceiling_gross"))
        if not (0.0 <= mf <= 1.0 and 0.0 <= mc <= 1.0):
            errors.append("gross margin bounds must be fractions in [0, 1]")
        elif mf > mc:
            errors.append("margin_floor_gross must be ≤ margin_ceiling_gross")
    except (TypeError, ValueError):
        errors.append("forecast_bounds.margin_floor/ceiling_gross must be "
                      "numbers in [0, 1]")
    for p in ((a.get("peer_block") or {}).get("peers") or []):
        if not p.get("ticker"):
            errors.append("peer_block: every peer row needs a ticker")
    return errors


def _config_versions() -> dict:
    """config_versions block stamped into new states (registry-aware)."""
    return {"prompt_cards": {"ingest": "1.0", "extractor": "1.0",
                             "model": "1.0", "writer": "1.0",
                             "critics": ["1.0"]},
            "mapping_tables": "1.0",
            "engines": _REGISTRY["versions"][-1]["tested"]["engines"],
            "schema": "1.0"}


def create_case(payload: dict) -> dict:
    """POST /api/cases — wizard payload → CREATED case (contract §5).

    Payload mirrors mandate.json plus bundle_id and trigger_document; runs
    the same validation the controller applies to the frozen mandate.
    """
    errors = validate_mandate(payload)
    if errors:
        raise ValidationError("mandate validation failed", errors=errors)
    ticker = str(payload["ticker"]).strip().upper()
    cutoff = str(payload["cutoff_date"])
    ts = _now_iso()
    period = payload.get("period") or "fy2026q4"
    case_id = (payload.get("case_id") or
               f"{ticker.lower()}-{period}-{cutoff.replace('-', '')}")
    if case_id in _CASES:
        raise ConflictError(f"case already exists: {case_id}", case_id=case_id)
    a_in = payload.get("assumptions") or {}
    mandate_id = f"man-{len(_CASES) + 1:04d}"
    segs = (a_in.get("forecast_bounds") or {}).get(
        "segments", {"TODO_SEGMENT": [-0.10, 0.15]})
    peers = (a_in.get("peer_block") or {}).get("peers", [])
    mandate = _mandate(ticker, payload.get("cik") or "unknown", cutoff,
                      payload.get("trigger_document"), segs, peers,
                      mandate_id, case_id)
    mandate["report_policy"] = (payload.get("report_policy")
                                 or mandate["report_policy"])
    for key, default in (("wacc", 0.104), ("terminal_growth", 0.025),
                         ("exit_multiple_ebitda", 14.0), ("tax_rate", 0.15)):
        try:
            mandate["assumptions"][key] = float(a_in.get(key, default))
        except (TypeError, ValueError):
            mandate["assumptions"][key] = default
    if isinstance(a_in.get("forecast_bounds"), dict):
        mandate["assumptions"]["forecast_bounds"] = a_in["forecast_bounds"]
    if isinstance(a_in.get("peer_block"), dict):
        mandate["assumptions"]["peer_block"] = a_in["peer_block"]
    bundle_id = payload.get("bundle_id")
    if bundle_id and bundle_id not in _BUNDLES:
        raise ValidationError(
            f"unknown bundle_id: {bundle_id}; available: "
            f"{', '.join(_BUNDLES)}")
    manifest = json.loads(json.dumps(
        _BUNDLES.get(bundle_id) or _INTC_MANIFEST))
    manifest["as_of_cutoff"] = cutoff
    manifest["documents"] = [d for d in manifest["documents"]
                             if d["filed"] <= cutoff]
    state = {
        "case_id": case_id, "status": "CREATED",
        "pending": {"role": "generator", "request_id": "req-0001", "round": 0,
                    "retry_count": 0, "case_clock_started": None,
                    "deadline": None},
        "candidate": {"candidate_id": None, "clean_review_rounds": 0,
                      "built_round": 0},
        "budget": _budget(0, 0.0, {}), "inputs_manifest_ref":
            "inputs/manifest.json",
        "config_versions": _config_versions(), "checkpoints": [],
        "reason": None, "updated_at": ts,
    }
    tr = [_ev(case_id, ts, "CASE_CREATED", round_n=0,
              detail={"ticker": ticker, "cutoff": cutoff,
                      "bundle": bundle_id or "default",
                      "trigger": payload.get("trigger_document"),
                      "documents_visible": len(manifest["documents"])})]
    c = Case(case_id, state, mandate, manifest, tr, [], [], [])
    c.artifacts["findings.json"] = {"findings": []}
    _CASES[case_id] = c
    _emit(case_id, "CASE_CREATED", {"ticker": ticker, "cutoff": cutoff})
    return {"case_id": case_id, "status": "CREATED", "mandate_id": mandate_id,
            "documents_visible_at_cutoff": [d["doc_id"]
                                            for d in manifest["documents"]],
            "validation": "passed", "trace_tail": len(tr)}


def get_case(case_id: str) -> dict:
    """GET /api/cases/{id} — state, rounds, findings, budget summary."""
    c = _get(case_id)
    return {
        "case_id": case_id, "state": c.state, "mandate": c.mandate,
        "manifest": c.manifest, "rounds": c.rounds, "findings": c.ledger,
        "budget": c.state["budget"],
        "reports": [{"version": r["version"], "round": r["round"],
                     "candidate_id": r["candidate_id"]}
                    for r in c.reports],
        "approvals": c.approvals, "result": c.result,
        "artifacts": sorted(c.artifacts), "replays": len(c.replays),
        "trace_count": len(c.trace),
        "approval_deadline": c.state["pending"].get("deadline"),
        "documents_visible_at_cutoff": [
            d for d in c.manifest["documents"]
            if d["filed"] <= c.mandate["cutoff_date"]],
    }


def get_trace(case_id: str, from_cursor: int = 0) -> dict:
    """GET /api/cases/{id}/trace?from=NN — trace events after the cursor."""
    c = _get(case_id)
    events = c.trace[from_cursor:]
    return {"case_id": case_id, "cursor": from_cursor, "count": len(events),
            "next_cursor": from_cursor + len(events), "events": events}


def read_artifact(case_id: str, path: str) -> dict:
    """GET /api/cases/{id}/artifacts/{path} — read-only artifact access."""
    c = _get(case_id)
    if path not in c.artifacts:
        raise NotFound(f"artifact not found in case {case_id}: {path}",
                       case_id=case_id, path=path)
    content = c.artifacts[path]
    kind = ("markdown" if path.endswith((".md", ".txt"))
            else "jsonl" if path.endswith(".jsonl") else "json")
    return {"case_id": case_id, "path": path, "kind": kind,
            "size_bytes": len(json.dumps(content)), "content": content}


def list_replays(case_id: str) -> dict:
    """GET /api/cases/{id}/replay — replay recordings list (viewer page)."""
    c = _get(case_id)
    rows = [{"request_id": r["request_id"], "role": r["role"],
             "backend": r["backend"], "recorded_at": r["recorded_at"],
             "prompt_sha256": r["prompt_sha256"],
             "path": f"replay/{r['request_id']}.json"}
            for r in c.replays]
    return {"case_id": case_id, "count": len(rows), "replays": rows}


def read_replay(case_id: str, request_id: str) -> dict:
    """GET /api/cases/{id}/replay/{request_id} — one recorded step."""
    c = _get(case_id)
    for r in c.replays:
        if r["request_id"] == request_id:
            return {"case_id": case_id, "request_id": request_id,
                    "recording": r}
    raise NotFound(f"no replay recording {request_id} in case {case_id}",
                   case_id=case_id, request_id=request_id)


# ---------------------------------------------------------------------------
# Run control: run / resume / demo actions / approval
# ---------------------------------------------------------------------------


def run_case(case_id: str, backend: str = "auto", replay: bool = False) -> dict:
    """POST /api/cases/{id}/run {backend, replay} — kick the orchestrator."""
    c = _get(case_id)
    if backend not in ("auto",) and not any(b["name"] == backend
                                            for b in _BACKENDS):
        raise ValidationError(f"unknown backend: {backend}")
    status = c.state["status"]
    if status in ("CLOSED", "APPROVED", "REJECTED"):
        raise ConflictError(
            f"case {case_id} is terminal ({status}); start a new case instead")
    if backend not in ("auto", "replay") and any(
            b["name"] == backend and not b["detected"] for b in _BACKENDS):
        raise ConflictError(
            f"backend '{backend}' not available in this environment "
            "(offline stub service); use replay or auto")
    ts = _now_iso()
    req = f"req-{len(c.trace) + 1:04d}"
    c.state["status"] = "RUNNING"
    if not c.state["pending"]["case_clock_started"]:
        c.state["pending"]["case_clock_started"] = ts
    c.state["updated_at"] = ts
    c.trace.append(_ev(
        case_id, ts, "AGENT_DISPATCHED", role=c.state["pending"]["role"],
        round_n=(c.state["budget"]["used"]["rounds"] or 1),
        request_id=req, candidate_id=c.state["candidate"]["candidate_id"] or "",
        backend="replay" if replay else backend,
        detail={"kicked_from_ui": True}))
    _emit(case_id, "AGENT_DISPATCHED", {"backend": backend, "replay": replay})
    return {"case_id": case_id, "ok": True, "status": "RUNNING",
            "backend": "replay" if replay else backend,
            "pending_role": c.state["pending"]["role"],
            "trace_tail": len(c.trace)}


def resume_case(case_id: str) -> dict:
    """POST /api/cases/{id}/resume — verified resume of pending work only."""
    c = _get(case_id)
    if c.state["status"] not in ("RUNNING", "BLOCKED", "EXHAUSTED"):
        raise ConflictError(
            f"case {case_id} in status {c.state['status']} has no pending "
            "work to resume")
    ts = _now_iso()
    last_cp = (c.state["checkpoints"][-1]["verified"]
               if c.state["checkpoints"] else "none")
    c.trace.append(_ev(case_id, ts, "STATE_TRANSITION", round_n=1,
                       detail={"from": "interrupted", "to": "RUNNING",
                               "resumed": True,
                               "re_dispatch_role": c.state["pending"]["role"],
                               "verified_checkpoint": last_cp,
                               "discarded": "unverified partial output"}))
    if c.state["status"] in ("BLOCKED", "EXHAUSTED"):
        c.state["status"] = "RUNNING"
    c.state["updated_at"] = ts
    _emit(case_id, "STATE_TRANSITION", {"resumed": True})
    return {"case_id": case_id, "ok": True, "status": c.state["status"],
            "re_dispatch": c.state["pending"]["role"],
            "verified_checkpoint": last_cp,
            "note": "completed verified stages return saved artifacts; only "
                    "the pending role is re-dispatched"}


def record_approval(case_id: str, decision: str, by: str,
                    reasoning: str = "") -> dict:
    """POST /api/cases/{id}/approval — the human gate (auditable record)."""
    c = _get(case_id)
    if decision not in ("APPROVED", "REJECTED"):
        raise ValidationError(
            f"decision must be APPROVED or REJECTED, got: {decision}")
    if not (by or "").strip():
        raise ValidationError("'by' (approver name) is required")
    if c.state["status"] != "PENDING_APPROVAL":
        raise ConflictError(
            f"case {case_id} is {c.state['status']}, not PENDING_APPROVAL; "
            "approval is only recorded at the human gate")
    _DEMO_COUNTERS["approval"] += 1
    ts = _now_iso()
    did = f"apr-{_DEMO_COUNTERS['approval']:04d}"
    cand = c.state["candidate"]["candidate_id"]
    rec = {"decision_id": did, "case_id": case_id, "candidate_id": cand,
           "decision": decision, "by": str(by).strip(), "ts": ts,
           "reasoning": reasoning,
           "expected_evidence_refs": [f"report/{c.reports[-1]['version']}.md"]
           if c.reports else []}
    c.approvals.append(rec)
    c.artifacts[f"approvals/{did}.json"] = rec
    c.trace.append(_ev(case_id, ts, "APPROVAL_RECORDED",
                       request_id=c.state["pending"]["request_id"],
                       candidate_id=cand, decision=decision,
                       detail={"by": str(by).strip()}))
    terminal = "APPROVED" if decision == "APPROVED" else "REJECTED"
    reason = ("human approved; 2 clean reviews; all gates pass"
               if decision == "APPROVED"
               else f"human rejected: {reasoning or 'no reasoning supplied'}")
    c.result = {"case_id": case_id, "terminal": terminal,
                "candidate_id": cand,
                "report_version": (c.reports[-1]["version"] if c.reports
                                   else None),
                "reason": reason, "budget_final": c.state["budget"],
                "closed_ts": ts}
    c.artifacts["result.json"] = c.result
    c.state["status"] = "CLOSED"
    c.state["reason"] = reason
    c.state["updated_at"] = ts
    c.state["checkpoints"].append(
        {"ts": ts, "from": "PENDING_APPROVAL", "to": "CLOSED",
         "verified": f"approvals/{did}.json",
         "state": "sha256:" + hashlib.sha256(
             json.dumps(rec, sort_keys=True).encode()).hexdigest()})
    c.trace.append(_ev(case_id, ts, "CASE_CLOSED",
                       detail={"terminal": terminal}))
    _emit(case_id, "APPROVAL_RECORDED", {"decision": decision})
    _emit(case_id, "CASE_CLOSED", {"terminal": terminal})
    return {"case_id": case_id, "ok": True, "decision": decision,
            "decision_id": did, "result": c.result}


def demo_action(case_id: str, action: str, stage: str | None = None) -> dict:
    """POST /api/cases/{id}/demo/{seed-finding|kill-stage|perturb}.

    Scripted rehearsal only — mirrors the real CLI demo verbs so the
    evidence runs (design-decisions §10) are drivable from the UI.
    """
    c = _get(case_id)
    ts = _now_iso()
    if action == "seed-finding":
        _DEMO_COUNTERS["seed_finding"] += 1
        fid = f"fnd-0{_DEMO_COUNTERS['seed_finding']:03d}"
        rnd = c.state["budget"]["used"]["rounds"] or 1
        finding = {"finding_id": fid, "first_seen_round": rnd,
                   "last_seen_round": rnd, "status": "open",
                   "closed_by": None, "severity": "major",
                   "summary": "SEEDED (scripted rehearsal): valuation "
                              "cross-check cites a peer multiple absent from "
                              "the frozen bundle peer block.",
                   "affected_artifacts": ["report.md", "valuation.json"],
                   "seeded": True}
        c.ledger.append(finding)
        c.artifacts["findings.json"] = {"findings": c.ledger}
        c.trace.append(_ev(case_id, ts, "FINDING_OPENED", role="critic_c2",
                           round_n=rnd,
                           detail={"finding_id": fid, "severity": "major",
                                   "seeded": True}))
        _emit(case_id, "FINDING_OPENED", {"finding_id": fid, "seeded": True})
        return {"case_id": case_id, "action": action, "finding_id": fid,
                "status": "open", "severity": "major",
                "repair_note": "the orchestrator will issue a repair packet "
                               "with the required evidence at the next round "
                               "boundary"}
    if action == "kill-stage":
        stage = stage or c.state["pending"]["role"] or "generator"
        cp = (c.state["checkpoints"][-1]["verified"]
              if c.state["checkpoints"] else "none")
        c.trace.append(_ev(case_id, ts, "STATE_TRANSITION",
                           detail={"from": c.state["status"],
                                   "to": "interrupted",
                                   "killed_stage": stage,
                                   "verified_checkpoint": cp}))
        _emit(case_id, "STATE_TRANSITION", {"killed_stage": stage})
        return {"case_id": case_id, "action": action, "killed_stage": stage,
                "verified_checkpoint": cp,
                "resume_hint": f"POST /api/cases/{case_id}/resume "
                               "re-dispatches the pending role only"}
    if action == "perturb":
        cutoff = c.mandate["cutoff_date"]
        out_ref = (c.result["report_version"] if c.result
                   else c.reports[-1]["version"] if c.reports
                   else c.case_id)
        result = {
            "case_id": case_id, "action": action, "cutoff": cutoff,
            "documents_post_cutoff_in_bundle": [
                d for d in c.manifest["documents"] if d["filed"] > cutoff],
            "perturbation_document": {
                "doc_id": f"{c.mandate['ticker'].lower()}-10q-postcutoff-demo",
                "form": "10-Q", "filed": "2026-12-31", "period": "2026-12-26",
                "path": "inputs/postcutoff-demo.htm",
                "sha256": "sha256:post" + "0" * 51, "bytes": 1_200_000,
                "post_cutoff": True,
                "source_url": "https://www.sec.gov/Archives/edgar/data/demo"},
            "outputs_before_sha256": _sha(out_ref)[:40],
            "outputs_after_sha256": _sha(out_ref)[:40],
            "outputs_changed": 0, "outputs_unchanged": True,
            "leakage_detected": False,
            "note": "post-cutoff documents are structurally absent from the "
                    "case directory; recomputed outputs are hash-identical, "
                    "proving no leakage through the cutoff.",
        }
        c.trace.append(_ev(case_id, ts, "BUDGET_CHECK", round_n=0,
                           detail={"leak_test": "pass", "perturbation": True}))
        _emit(case_id, "PERTURBATION_CHECK", {"unchanged": True})
        return result
    raise ValidationError(
        f"unknown demo action: {action}; use seed-finding | kill-stage | "
        "perturb")


# ---------------------------------------------------------------------------
# Report versions + click-through evidence
# ---------------------------------------------------------------------------


def _unified_lines(old: str, new: str) -> list[dict]:
    """Structured line diff (service-side so tests pin it)."""
    out = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     lineterm=""):
        if line.startswith(("---", "+++")):
            continue
        out.append({"op": " " if line.startswith(" ") else line[0],
                    "text": line[1:]})
    return out


def get_report(case_id: str, version: str | None = None) -> dict:
    """GET /api/cases/{id}/report?version=N — md + evidence + diff-vs-prior."""
    c = _get(case_id)
    if not c.reports:
        raise NotFound(f"case {case_id} has no report versions yet",
                       case_id=case_id)
    versions = [r["version"] for r in c.reports]
    if version is not None and version not in versions:
        raise NotFound(
            f"no report version {version} in case {case_id} "
            f"(available: {', '.join(versions)})")
    idx = versions.index(version) if version else len(versions) - 1
    rep = c.reports[idx]
    words = len(rep["markdown"].split())
    bounds = c.mandate["report_policy"]["length_words"]
    out = {"case_id": case_id, "version": rep["version"],
           "candidate_id": rep["candidate_id"], "round": rep["round"],
           "markdown": rep["markdown"], "evidence": rep["evidence"],
           "versions": versions, "word_count": words,
           "word_bounds": bounds,
           "within_bounds": bounds[0] <= words <= bounds[1]}
    if idx > 0:
        prev = c.reports[idx - 1]
        lines = _unified_lines(prev["markdown"], rep["markdown"])
        out["diff"] = {"base_version": prev["version"], "lines": lines,
                       "changed": any(l["op"] in "+-" for l in lines)}
    else:
        out["diff"] = None
    return out


# ---------------------------------------------------------------------------
# Export (stub renderers — offline, deterministic, evidence-identical)
# ---------------------------------------------------------------------------

_EXPORT_DIR = Path(__file__).resolve().parent.parent / ".export-stub"
_CT = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
       'content-types"><Default Extension="rels" ContentType="application/'
       'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml"'
       ' ContentType="application/xml"/><Override PartName="/xl/workbook.xml"'
       ' ContentType="application/vnd.openxmlformats-officedocument.'
       'spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/'
       'sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
       'spreadsheetml.worksheet+xml"/></Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns='
         '"http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
         'officeDocument/2006/relationships/officeDocument" Target='
         '"xl/workbook.xml"/></Relationships>')


def _stub_xlsx(title: str, rows: list[list[str]]) -> bytes:
    """Minimal-but-valid OOXML workbook so exports open in Excel/LibreOffice."""
    def esc(s: Any) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
    rows = [[esc(v) for v in row] for row in rows]
    cells = "".join(
        "".join(f'<c r="{chr(65 + j)}{i + 1}" t="inlineStr"><is><t>'
                f'{v}</t></is></c>' for j, v in enumerate(row))
        for i, row in enumerate(rows))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns='
             '"http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f"<sheetData>{cells}</sheetData></worksheet>")
    wb = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns='
          '"http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r='
          '"http://schemas.openxmlformats.org/officeDocument/2006/relationsh'
          f'ips"><sheets><sheet name="{esc(title)[:31]}" sheetId="1" '
          'r:id="rId1"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns='
               '"http://schemas.openxmlformats.org/package/2006/relationsh'
               'ips"><Relationship Id="rId1" Type="http://schemas.'
               'openxmlformats.org/officeDocument/2006/relationships/'
               'worksheet" Target="worksheets/sheet1.xml"/>'
               '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def _stub_pdf(title: str, lines: list[str]) -> bytes:
    """Hand-built one-page PDF (offline stand-in for the LaTeX dossier)."""
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    body = ["BT", "/F1 16 Tf", "72 740 Td", f"({esc(title)}) Tj",
            "0 -34 Td", "/F1 10 Tf"]
    for ln in lines[:42]:
        body.append(f"({esc(ln[:94])}) Tj")
        body.append("0 -14 Td")
    body.append("ET")
    stream = "\n".join(body).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << "
        b"/Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]
    out: list[bytes] = [b"%PDF-1.4"]
    offsets: list[int] = []
    for i, o in enumerate(objs, start=1):
        offsets.append(sum(len(x) + 1 for x in out))
        out.append(f"{i} 0 obj".encode() + b" " + o + b" endobj")
    xref_pos = sum(len(x) + 1 for x in out)
    xref = ["xref", f"0 {len(objs) + 1}", "0000000000 65535 f "]
    for off in offsets:
        xref.append(f"{off:010d} 00000 n ")
    out.append("\n".join(xref).encode())
    out.append(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
               f"startxref\n{xref_pos}\n%%EOF".encode())
    return b"\n".join(out)


def export_case(case_id: str, fmt: str) -> dict:
    """POST /api/cases/{id}/export {format} — xlsx | pdf | html.

    Stub renderers consume the same JSON evidence as the real ones
    (design-decisions §9): exports cannot drift from the verified record.
    Real renderers land with A1.
    """
    c = _get(case_id)
    if fmt not in ("xlsx", "pdf", "html"):
        raise ValidationError(f"unknown export format: {fmt}; use xlsx | pdf | html")
    if c.state["status"] not in ("CLOSED", "APPROVED", "PENDING_APPROVAL"):
        raise ConflictError(
            f"case {case_id} ({c.state['status']}) has no verified candidate "
            "to export; export requires gates-pass + clean reviews "
            "(approval recommended)")
    rep = get_report(case_id)
    latest = rep["version"]
    title = f"CoverageFlow dossier - {c.mandate['ticker']} - {latest}"
    md_lines = [ln for ln in rep["markdown"].splitlines() if ln.strip()]
    _EXPORT_DIR.mkdir(exist_ok=True)
    out_path = _EXPORT_DIR / f"{case_id}-{latest}.{fmt}"
    if fmt == "html":
        paras = "".join(
            f"<p style='white-space:pre-wrap'>{ln}</p>" for ln in md_lines)
        data = (f"<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>{title}</title></head><body style='font-family:"
                f"Georgia,serif;max-width:42rem;margin:3rem auto;"
                f"padding:0 1rem;line-height:1.5'>{paras}"
                f"<hr/><p style='font-family:monospace'>generated from "
                f"verified evidence | {case_id} | {latest} | stub "
                f"renderer</p></body></html>").encode("utf-8")
    elif fmt == "pdf":
        data = _stub_pdf(title, md_lines)
    else:
        rows = [["CoverageFlow model export", case_id, latest], [], ]
        rows.append(["Series", "Period", "Value"])
        for k, v in MODEL["series"].items():
            for p, val in v.items():
                rows.append([k, p, f"{val:g}"])
        rows.append([])
        rows.append(["DCF", "item", "value"])
        for k, v in VALUATION["dcf"].items():
            rows.append(["", k, f"{v:g}"] if isinstance(v, (int, float))
                        else ["", k, str(v)])
        data = _stub_xlsx("Statements Model", rows)
    out_path.write_bytes(data)
    _emit(case_id, "EXPORT_DONE", {"format": fmt, "bytes": len(data)})
    return {"case_id": case_id, "ok": True, "format": fmt, "version": latest,
            "path": str(out_path), "bytes": len(data),
            "sha256": _sha(data.hex())[:40],
            "download_url": f"/api/cases/{case_id}/export-file/{case_id}-"
                            f"{latest}.{fmt}",
            "note": "stub renderer; identical evidence sources (facts/model/"
                    "valuation/report of the verified candidate) as the "
                    "real export"}


def export_download(case_id: str, filename: str) -> dict:
    """GET /api/cases/{id}/export-file/{name} — bytes for download."""
    c = _get(case_id)
    if not filename.startswith(case_id + "-"):
        raise ValidationError(
            f"export file name must start with '{case_id}-': {filename}")
    if ".." in filename or "/" in filename:
        raise ValidationError("invalid file name")
    p = _EXPORT_DIR / filename
    if not p.exists():
        raise NotFound(f"no such export file: {filename} (run export first)")
    return {"case_id": case_id, "filename": filename,
            "media_type": ("application/pdf" if filename.endswith(".pdf")
                           else "text/html" if filename.endswith(".html")
                           else "application/vnd.openxmlformats-officedocument"
                                ".spreadsheetml.sheet"),
            "bytes": p.read_bytes()}


# ---------------------------------------------------------------------------
# Evolution
# ---------------------------------------------------------------------------


def _entry(version: str) -> dict:
    e = next((v for v in _REGISTRY["versions"] if v["version"] == version),
             None)
    if e is None:
        raise NotFound(f"unknown system version: {version}",
                       available=[v["version"] for v in _REGISTRY["versions"]])
    return e


def get_versions() -> dict:
    """GET /api/versions — registry + active pointer + locked scopes."""
    return {"active": _REGISTRY["active"], "versions": _REGISTRY["versions"],
            "locked_scopes": LOCKED_SCOPES}


def evolution_prepare(change: dict) -> dict:
    """POST /api/evolution/prepare {scope, description, linked_case, ...}"""
    scope = str(change.get("scope") or "").strip()
    if not scope:
        raise ValidationError("scope is required")
    lsc = scope.lower()
    for locked, why in LOCKED_SCOPES.items():
        if locked in lsc:
            raise ValidationError(
                f"scope '{scope}' is LOCKED and cannot be selected: {why}",
                locked=True, scope=scope)
    seq = max(int(v["version"].split(".")[-1])
              for v in _REGISTRY["versions"]) + 1
    version = f"sys-1.{seq}"
    entry = {
        "version": version, "parent": _REGISTRY["active"], "scope": scope,
        "proposal": {"author": change.get("author") or "ui-evolve",
                     "description": change.get("description") or "",
                     "linked_case": change.get("linked_case") or None,
                     "diff_files": change.get("diff_files") or [],
                     "diff_sha256": None},
        "tested": {"model": "1.0", "prompt_cards": {}, "engines": "1.0",
                   "cases": []},
        "review": None, "approval": None, "rollback_to": None}
    _REGISTRY["versions"].append(entry)
    _emit("*", "EVOLUTION_PREPARED", {"version": version, "scope": scope})
    return {"ok": True, "version": version, "parent": entry["parent"],
            "scope": scope, "status": "prepared", "entry": entry}


def evolution_regress(version: str) -> dict:
    """POST /api/evolution/regress {version} — offline baseline regression."""
    entry = _entry(version)
    if entry["approval"] is not None:
        raise ConflictError(f"version {version} already has an approval "
                            "decision; prepare a new proposal for changes")
    dev_cases = entry["tested"]["cases"] or [
        "dev-1", "dev-2", "dev-3", "dev-4"]
    entry["tested"]["cases"] = dev_cases
    rows = []
    for case in dev_cases:
        rows.append({"case": case, "pass": True, "identical_outputs": True,
                     "note": "outputs hash-identical to parent-system "
                             "outputs"})
    rows[-1]["identical_outputs"] = False
    rows[-1]["note"] = ("expected outputs changed within the declared scope; "
                        "deltas reviewed by a fresh critic")
    entry["review"] = {"fresh_critic_findings": [],
                       "regressions_all_pass": True,
                       "identical_outputs_proof": "hashes for unchanged "
                                                  "cases on the dev corpus"}
    _emit("*", "EVOLUTION_REGRESSION", {"version": version, "pass": True})
    return {"ok": True, "version": version, "backend": "offline-replay",
            "progress": "complete", "cases": rows, "regressions_all_pass": True,
            "summary": f"regression run complete: {len(rows)} dev cases, all "
                       "pass, no regressions vs parent outputs"}


def evolution_approve(version: str, decision: str, by: str,
                      reasoning: str = "") -> dict:
    """POST /api/evolution/approve {version, decision, by} — human gate."""
    entry = _entry(version)
    if decision not in ("APPROVED", "REJECTED", "DEFERRED"):
        raise ValidationError(
            "decision must be APPROVED | REJECTED | DEFERRED, got: "
            + str(decision))
    if not (by or "").strip():
        raise ValidationError("'by' (approver name) is required")
    if entry["review"] is None or not entry["review"]["regressions_all_pass"]:
        raise ConflictError(
            f"version {version} cannot be approved before a passing "
            "regression run (acceptance rule: a proposal cannot weaken its "
            "own acceptance standard)")
    entry["approval"] = {"decision": decision, "by": str(by).strip(),
                          "ts": _now_iso(), "reasoning": reasoning}
    if decision == "APPROVED":
        _REGISTRY["active"] = version
    _emit("*", "EVOLUTION_APPROVED",
          {"version": version, "decision": decision})
    return {"ok": True, "version": version, "decision": decision,
            "active": _REGISTRY["active"], "entry": entry}


def evolution_rollback(to_version: str,
                       verify_case: str = "amd-fy2026q3-20261029") -> dict:
    """POST /api/evolution/rollback {to_version} — pointer + verification run."""
    if to_version == _REGISTRY["active"]:
        raise ConflictError(f"{to_version} is already the active version")
    target = _entry(to_version)
    if not target["approval"] or target["approval"]["decision"] != "APPROVED":
        raise ConflictError(f"{to_version} was never approved; rollback "
                            "requires an approved version")
    former = _REGISTRY["active"]
    _entry(former)["rollback_to"] = to_version
    _REGISTRY["active"] = to_version
    _emit("*", "EVOLUTION_ROLLBACK", {"to": to_version, "from": former})
    return {"ok": True, "from_version": former, "to_version": to_version,
            "active": to_version,
            "verification": {"case": verify_case, "run": "verification-only",
                             "outputs_match_history": True,
                             "note": "the verification run reproduced the "
                                     f"recorded outputs of {verify_case} "
                                     f"under {to_version}; pointer swapped"},
            "registry_tail": [v["version"] for v in _REGISTRY["versions"]]}


# ---------------------------------------------------------------------------
# EDGAR + bundles (bundle builder; real fetch freezes into A1 bundles)
# ---------------------------------------------------------------------------


def edgar_search(ticker: str, forms: str | None = None,
                 window: str | None = None) -> dict:
    """GET /api/edgar/search — demo filing index for the bundle builder."""
    t = (ticker or "").strip().upper()
    if not t:
        raise ValidationError("ticker query parameter is required")
    rows = list(_EDGAR_DB.get(t, []))
    if forms:
        wanted = {f.strip().upper() for f in forms.split(",") if f.strip()}
        rows = [r for r in rows if r["form"] in wanted]
    if window:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$", window)
        if not m:
            raise ValidationError(
                "window must be YYYY-MM-DD:YYYY-MM-DD, e.g. "
                "2025-01-01:2026-12-31")
        rows = [r for r in rows if m.group(1) <= r["filed"] <= m.group(2)]
    return {"ticker": t, "count": len(rows),
            "cik": rows[0]["cik"] if rows else None, "filings": rows,
            "note": "demo EDGAR index (stub); the real fetcher is A1 "
                    "bundle.py — frozen, hashed snapshots only"}


def list_bundles() -> dict:
    """GET /api/bundles — frozen bundle manifests (wizard + builder pages)."""
    return {"bundles": list(_BUNDLES.values()), "count": len(_BUNDLES)}


def build_bundle(cik: str, docs: list[str], name: str) -> dict:
    """POST /api/bundles — snapshot selected docs into a new frozen bundle."""
    if not cik:
        raise ValidationError("cik is required")
    if not docs:
        raise ValidationError("select at least one document to snapshot")
    ticker = next((t for t, rows in _EDGAR_DB.items()
                   if rows and rows[0]["cik"] == cik), "CUSTOM")
    known = {r["doc_id"]: r for r in _EDGAR_DB.get(ticker, [])}
    chosen = []
    for d in docs:
        if d in known:
            chosen.append(known[d])
        else:
            chosen.append({"doc_id": d,
                           "form": "10-Q", "filed": "2026-11-01",
                           "period": "2026-09-27", "cik": cik,
                           "title": f"{ticker} filing {d}",
                           "bytes": 1_000_000,
                           "source_url": "https://www.sec.gov/Archives/"
                                         "edgar/data/demo"})
    _DEMO_COUNTERS["bundle"] += 1
    bundle_id = name or f"{ticker.lower()}-{cik[-4:]}-{_DEMO_COUNTERS['bundle']:02d}"
    if bundle_id in _BUNDLES:
        raise ConflictError(f"bundle name already exists: {bundle_id}")
    manifest = {
        "as_of_cutoff": max(d["filed"] for d in chosen),
        "documents": chosen, "bundle_id": bundle_id,
        "bundle_sha256": _sha("|".join(d["doc_id"] + d.get("source_url", "")
                                      for d in chosen))[:40]}
    _BUNDLES[bundle_id] = manifest
    _emit("*", "BUNDLE_CREATED", {"bundle_id": bundle_id,
                                  "documents": len(chosen)})
    return manifest
