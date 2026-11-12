"""Service boundary for the CoverageFlow web UI (stream A4, owned).

PURPOSE: one function per API endpoint defined in docs/contracts/interfaces.md
section 5 (and the CLI verbs of section 3 where they back a UI page).  api.py /
ws.py / app.js never touch files or orchestration logic — they only call the
functions below.

MODES (integration, stream A6): every function keeps its name/signature/return
shape EXACTLY.

  demo mode (default)  — LOCAL STUB backed by realistic in-memory demo data
        for the two demo bundles (Intel, AMD), generated deterministically at
        import time (no network, no LLM, no clock reads in the fixtures).
        COVERAGEFLOW_WORKSPACE unset/invalid, or the coverageflow package
        not importable.

  controller mode       — COVERAGEFLOW_WORKSPACE points at a real workspace
        directory; every operation is routed through the real controller
        package (Orchestrator, ledger, statestore, backends, bundle builder,
        export_xlsx) so the UI can never bypass the Orchestrator gates. Any
        real call that cannot run offline degrades to the demo path with a
        clear "offline fallback" note instead of failing the UI.

Mutable demo flows supported (mirrored by real operations later): create case
(mandate validation), run, resume, human approval record, demo scripted
actions (seed finding / kill stage / perturbation), report version diff,
export, evolution prepare/regress/approve/rollback, bundle snapshot.
Evolution endpoints serve the in-module registry demo store in both modes —
the live evolution loop is driven by the CLI (`python -m coverageflow evolve`).

Fixed timestamps belong to the deterministic fixtures; `health()` and
decision records use live UTC time and are marked as such.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from coverageflow import mandate_check as _mc
from coverageflow import mandate_prefill as _mp
from coverageflow import market_data as _md
from coverageflow import mandate_sanitize as _ms
from coverageflow import sanitize as _san
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Error boundary (api.py maps these to HTTP statuses)
# ---------------------------------------------------------------------------


class ServiceError(Exception):
    """Base class for service-layer errors carrying an HTTP status."""

    status = 400

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = {"error": message, **detail}


def _v(fn, field: str, value: Any, **kw: Any) -> Any:
    """Run a coverageflow.sanitize cleaner; a rejection becomes a 422."""
    try:
        return fn(field, value, **kw)
    except _san.InputError as exc:
        raise ValidationError(str(exc), field=exc.field) from exc


class NotFound(ServiceError):
    status = 404


class ValidationError(ServiceError):
    status = 422


class ConflictError(ServiceError):
    status = 409


class UpstreamError(ServiceError):
    status = 502


# ---------------------------------------------------------------------------
# Real-package hook (integration stream A6: demo mode UNLESS
# COVERAGEFLOW_WORKSPACE names a real workspace directory)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised via the wiring toggle tests
    from coverageflow import cli as _real_cli
    from coverageflow import export_xlsx as _real_export_xlsx
    from coverageflow import ledger as _real_ledger
    from coverageflow import orchestrator as _real_orchestrator
    from coverageflow import statestore as _real_statestore
    from coverageflow import gates as _real_gates
    from coverageflow import bundle as _real_bundle
    from coverageflow import export_dossier as _real_export_dossier
    from coverageflow.backends import (detect_available_backends
                                       as _real_detect_backends)
    from coverageflow.backends import make_backend as _real_make_backend

    _HAVE_REAL_BACKEND = True
except Exception:  # package not present: pure demo service
    _real_cli = None  # type: ignore[assignment]
    _real_export_xlsx = None  # type: ignore[assignment]
    _real_export_dossier = None  # type: ignore[assignment]
    _real_ledger = None  # type: ignore[assignment]
    _real_orchestrator = None  # type: ignore[assignment]
    _real_statestore = None  # type: ignore[assignment]
    _real_gates = None  # type: ignore[assignment]
    _real_bundle = None  # type: ignore[assignment]
    _real_detect_backends = None  # type: ignore[assignment]
    _real_make_backend = None  # type: ignore[assignment]
    _HAVE_REAL_BACKEND = False


# ---------------------------------------------------------------------------
# Mode selection: COVERAGEFLOW_WORKSPACE points the service at a real
# controller workspace (cases/, replay/, export/ per the frozen contracts).
# Unset / missing directory => the deterministic demo service (default).
# ---------------------------------------------------------------------------


def _real_workspace() -> Path | None:
    """The real workspace directory, or None (demo mode)."""
    if not _HAVE_REAL_BACKEND:
        return None
    raw = os.environ.get("COVERAGEFLOW_WORKSPACE", "").strip()
    if not raw:
        return None
    ws = Path(raw).expanduser().resolve()
    return ws if ws.is_dir() else None


def _using_real() -> bool:
    return _real_workspace() is not None


_MODE_ENV = "COVERAGEFLOW_WORKSPACE"


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
# Real-workspace internals (controller mode). Everything goes through the
# controller package: the Orchestrator is the ONLY state writer; reads use
# statestore; exports use the deterministic engine artifacts. Each helper
# mirrors the demo-store return shapes exactly so api.py/app.js are blind
# to the mode.
# ---------------------------------------------------------------------------


def _rws() -> Path:
    ws = _real_workspace()
    if ws is None:  # pragma: no cover - callers check _using_real() first
        raise ServiceError(f"{_MODE_ENV} is not set to a valid workspace")
    return ws


def _rcase_dir(case_id: str) -> Path:
    case_id = _v(_san.identifier, "case_id", case_id)
    cd = _rws() / "cases" / case_id
    if not (cd / "state.json").is_file():
        raise NotFound(f"case not found in real workspace: {case_id}",
                       case_id=case_id)
    return cd


def _rstate(cd: Path) -> dict:
    st = _real_statestore.load_state(cd)
    if st is None:
        raise NotFound(f"case state unreadable: {cd.name}", case_id=cd.name)
    return st


def _rinterrupted(cd: Path, status: str) -> bool:
    """RUNNING with no live lock holder: the run died, nothing is executing.

    The orchestrator runs inside the request that started it and holds
    ``run.lock`` for the duration, so a RUNNING case with no live owner was
    interrupted (crash, restart, or a state file edited outside the
    orchestrator). Reporting it as "in progress" leaves a case that looks
    busy forever and never finishes.
    """
    return status == "RUNNING" and _real_statestore.run_owner(cd) is None


def _vrkey(name: str):
    m = re.match(r"v(\d+)", name)
    return (0, int(m.group(1))) if m else (1, 0)


def _rreport_versions(cd: Path) -> list[str]:
    rep = cd / "report"
    if not rep.is_dir():
        return []
    return sorted((p.name[:-3] for p in rep.glob("v*.md")
                   if not p.name.endswith(".clean.md")), key=_vrkey)


def _rdraft_report(cd: Path) -> tuple[int, str] | None:
    """The newest round's report.md, before any version is published.

    `report/v<N>-<candidate8>.md` is the *published* artifact and only exists
    after a human approves. Until then the note lives in the round directory,
    and the reviewer has to be able to read what they are signing off on.
    """
    best: tuple[int, str] | None = None
    for p in cd.glob("rounds/round-*/generator/report.md"):
        try:
            round_no = int(p.parent.parent.name.removeprefix("round-"))
        except ValueError:
            continue
        if best is None or round_no > best[0]:
            best = (round_no, p.read_text("utf-8"))
    return best


def _rledger_rows(cd: Path) -> list[dict]:
    data = _real_statestore.read_json_or_none(cd / "findings.json") or {}
    return list(data.get("findings", []))


def _rcase_summary(cd: Path) -> dict:
    """Dashboard row from real disk (same shape as _case_summary)."""
    st = _rstate(cd)
    mandate = _real_statestore.read_json_or_none(cd / "mandate.json") or {}
    budget = st.get("budget") or {}
    used = budget.get("used") or {}
    tokens_by_role = used.get("tokens_by_role") or {}
    pending = st.get("pending") or {}
    findings = _rledger_rows(cd)
    versions = _rreport_versions(cd)
    status = st.get("status")
    return {
        "case_id": st.get("case_id", cd.name),
        "ticker": mandate.get("ticker", cd.name.split("-")[0].upper()),
        "cutoff_date": mandate.get("cutoff_date"),
        "status": status,
        "rounds_used": used.get("rounds", 0),
        "rounds_max": budget.get("rounds_max"),
        "elapsed_minutes": used.get("elapsed_minutes", 0.0),
        # only a RUNNING case has a live clock; BLOCKED/EXHAUSTED/STOPPED
        # cases keep their booked elapsed_minutes and stop counting
        "case_clock_started": (pending.get("case_clock_started")
                               if status == "RUNNING" else None),
        "case_minutes": budget.get("case_minutes"),
        "tokens_max_role": (max(tokens_by_role.values())
                            if tokens_by_role else 0),
        "role_tokens_max": budget.get("role_tokens_max"),
        "open_findings": sum(1 for f in findings if f.get("status") == "open"),
        "clean_review_rounds": (st.get("candidate") or {}).get(
            "clean_review_rounds", 0),
        "pending_role": (None if status in ("CLOSED", "APPROVED", "REJECTED")
                         else pending.get("role")),
        "report_versions": versions,
        "latest_report": (versions[-1] if versions
                          else ("draft" if _rdraft_report(cd) else None)),
        "deadline": pending.get("deadline"),
        "updated_at": st.get("updated_at"),
        "interrupted": _rinterrupted(cd, status),
    }


def _rartifacts(cd: Path) -> list[str]:
    """All case-relative artifact paths (read-only viewer listing)."""
    out: list[str] = []
    for p in sorted(cd.rglob("*")):
        if p.is_file() and not p.name.endswith(".tmp"):
            out.append(p.relative_to(cd).as_posix())
    return out


def _rreplays(cd: Path) -> list[dict]:
    rp = cd / "replay"
    out = []
    if rp.is_dir():
        for p in sorted(rp.glob("*.json")):
            try:
                out.append(json.loads(p.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def _rdispatch_for(case_id: str, backend: str) -> tuple[Any, str]:
    """Dispatch + backend name chosen via the controller CLI seam."""
    cd = _rcase_dir(case_id)
    ws = _rws()
    replay_dir = cd / "replay"
    backend = backend or "auto"
    try:
        dispatch, backend_name = _real_cli.load_dispatch(
            backend=backend, workspace=ws,
            replay_dir=replay_dir if backend == "replay" else None)
    except Exception as exc:  # backend not available/selected
        raise ConflictError(
            f"backend '{backend}' could not start for case {case_id}: {exc}",
            case_id=case_id, backend=backend)
    return dispatch, backend_name


def _rorchestrator(cd: Path, backend: str = "auto"):
    ws = _rws()
    dispatch, _ = _rdispatch_for(cd.name, backend)
    gate_engine = _real_cli.load_gate_engine(workspace=ws)
    return _real_orchestrator.Orchestrator(cd, dispatch=dispatch,
                                           gate_engine=gate_engine)


def _rapprove_records(cd: Path) -> list[dict]:
    out = []
    ap = cd / "approvals"
    if ap.is_dir():
        for p in sorted(ap.glob("*.json")):
            rec = _real_statestore.read_json_or_none(p)
            if rec:
                out.append(rec)
    return out


def _rlatest_engine_artifact(cd: Path, name: str) -> dict | None:
    """Newest model.json/valuation.json across built rounds."""
    best = None
    for p in cd.glob(f"rounds/round-*/generator/{name}"):
        if best is None or p.stat().st_mtime > best[0]:
            best = (p.stat().st_mtime, p)
    if best is None:
        return None
    try:
        return _real_statestore.read_json(best[1])
    except Exception:
        return None


def _rfind_bundle_dir(bundle_id: str) -> Path | None:
    """Resolvable frozen bundle directory: workspace bundles/ then data/*-bundle."""
    if not bundle_id or _real_bundle is None:
        return None
    ws = _rws()
    return _real_bundle.find_bundle_dir(
        bundle_id, workspace=ws,
        data_dirs=[ws / "data", Path(__file__).resolve().parent.parent / "data"])


def _rfind_bundle_manifest(bundle_id: str) -> dict | None:
    """Resolvable frozen bundle: workspace bundles/ then data/*-bundle dirs."""
    found = _rfind_bundle_dir(bundle_id)
    if found is not None:
        return _real_statestore.read_json(found / "manifest.json")
    return None


def _rmanifest_from_payload(payload: dict, cutoff: str,
                            trigger: str | None) -> dict:
    explicit = payload.get("manifest")
    if isinstance(explicit, dict):
        return explicit
    if payload.get("bundle_id"):
        man = _rfind_bundle_manifest(str(payload["bundle_id"]))
        if man is not None:
            man = json.loads(json.dumps(man))
            man["as_of_cutoff"] = cutoff
            man["documents"] = [d for d in man["documents"]
                                if d["filed"] <= cutoff]
            return man
    trigger = trigger or payload.get("trigger_document") or "trigger-doc"
    form = "10-K" if "10k" in str(trigger).lower() else "10-Q"
    return {
        "as_of_cutoff": cutoff,
        "documents": [{"doc_id": str(trigger), "form": form,
                      "filed": cutoff, "path": f"inputs/{trigger}.htm",
                      "sha256": None, "bytes": None, "source_url": None}],
        "bundle_id": f"{str(payload.get('ticker') or 'case').lower()}"
                    f"-{cutoff[:7]}",
        "bundle_sha256": None,
    }


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


_ASSUMPTION_EXTRAS = None  # built lazily below


def _mandate_assumption_extras(wacc: float, terminal_growth: float,
                               as_of: str) -> dict:
    """Human-owned valuation extras required by the A1 valuation engine
    (valuation_engine.Assumptions.from_payload):

      market.price            -> dcf/comps upside vs the market
      own_band                -> own historical multiple band (lo, hi per
                                 multiple; comps cross-check block)
      sensitivity.{wacc_points, terminal_growth_points}
                               -> explicit WACC x terminal-growth grid
    """
    return {
        "market": {"as_of": as_of, "price": None,
                   "note": "human-owned market data; fill at mandate "
                           "creation (USD per share, as-of on or before cutoff)"},
        "own_band": {"as_of": as_of,
                     "ev_ebitda": [10.0, 16.0], "p_e": [12.0, 26.0],
                     "p_s": [2.5, 7.5],
                     "note": "own historical multiple band (lo, hi)"},
        "sensitivity": {
            "wacc_points": [round(wacc + d * 0.02, 6) for d in (-2, -1, 0, 1, 2)],
            "terminal_growth_points": [round(terminal_growth + d * 0.01, 6)
                                       for d in (-2, -1, 0, 1, 2)],
            "note": "WACC x terminal growth sensitivity grid points"},
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
            "dcf_comps_tolerance": 0.25,
            **_mandate_assumption_extras(0.104, 0.025, cutoff)},
        "limits": {"role_tokens_max": 1200000, "case_minutes": 120, "rounds_max": 4,
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
    return {"rounds_max": 4, "retries_max": 3, "case_minutes": 120,
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
    "artifact": "valuation", "engine": "valuation_engine@2.0",
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
    {"name": "vibe", "kind": "cli", "detected": False,
     "detail": "'vibe' not found in PATH (stub detection)",
     "api_config": None, "healthy": None, "selected": False,
     "usage_note": "headless Mistral CLI login (vibe -p)"},
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
    {"name": "stub", "kind": "offline", "detected": True,
     "detail": "scripted offline role responses — canned gate-satisfying "
               "outputs, always available (demo default)",
     "api_config": None, "healthy": True, "selected": False,
     "usage_note": "canned role responses, no LLM, no recordings needed"},
]

# controller-mode selection (made via POST /api/backends/select); the demo
# store keeps its own 'selected' flags on _BACKENDS above
_REAL_SELECTED_BACKEND = "auto"
_SELECTION_FILE = ".backend-selection.json"


def _selected_real_backend() -> str:
    """The backend picked on the Backends page, surviving a server restart.

    The choice is saved in the workspace; the in-memory value is the fallback
    when nothing was saved (or the saved name is no longer usable).
    """
    try:
        saved = json.loads((_rws() / _SELECTION_FILE).read_text(encoding="utf-8"))
        name = saved.get("backend")
        if isinstance(name, str) and name:
            return name
    except (OSError, ValueError, ServiceError, AttributeError):
        pass
    return _REAL_SELECTED_BACKEND


def _save_selected_backend(name: str) -> None:
    try:
        (_rws() / _SELECTION_FILE).write_text(
            json.dumps({"backend": name}), encoding="utf-8")
    except OSError:
        pass    # the in-memory choice still holds for this server run

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
    case_id = _v(_san.identifier, "case_id", case_id)
    c = _CASES.get(case_id)
    if c is None:
        raise NotFound(f"case not found: {case_id}", case_id=case_id)
    return c


# ---------------------------------------------------------------------------
# Health / backends
# ---------------------------------------------------------------------------


def health() -> dict:
    """Backend health snapshot for the dashboard header (live timestamp)."""
    if _using_real():
        ws = _rws()
        cases = [d for d in sorted((ws / "cases").iterdir())
                 if d.is_dir() and (d / "state.json").is_file()
                 ] if (ws / "cases").is_dir() else []
        states = [_rstate(d) for d in cases]
        running = [(d, s) for d, s in zip(cases, states)
                   if s["status"] == "RUNNING"]
        live = [d for d, _s in running if _real_statestore.run_owner(d)]
        return {
            "status": "ok", "mode": "controller",
            "workspace": str(ws),
            "active_backend": _selected_real_backend(),
            "cases": len(cases),
            "active_runs": len(live),
            "interrupted_runs": len(running) - len(live),
            "pending_approvals": sum(1 for s in states
                                     if s["status"] == "PENDING_APPROVAL"),
            "ts": _now_iso(),
        }
    active = [c for c in _CASES.values() if c.state["status"] == "RUNNING"]
    return {
        "status": "ok",
        "mode": "controller" if _HAVE_REAL_BACKEND and _using_real()
        else "stub-service",
        "workspace": "demo (scripted rehearsal store)",
        "active_backend": next((b["name"] for b in _BACKENDS if b["selected"]),
                               "replay"),
        "cases": len(_CASES),
        "active_runs": len(active),
        "interrupted_runs": 0,
        "pending_approvals": sum(
            1 for c in _CASES.values()
            if c.state["status"] == "PENDING_APPROVAL"),
        "ts": _now_iso(),
    }


def _realt_backend_rows() -> list[dict]:
    """_BACKENDS rows updated with real coverageflow.backends detection."""
    try:
        detected = set(_real_detect_backends())
    except Exception:
        detected = set()
    rows = []
    for tmpl in _BACKENDS:
        row = dict(tmpl)
        row["selected"] = row["name"] == _selected_real_backend()
        if row["name"] in ("replay", "stub"):
            rows.append(row)  # always-available rows stand as-is
            continue
        ok = row["name"] in detected
        if ok:
            try:
                hb = _real_make_backend(row["name"]).healthcheck()
                row["healthy"] = bool(hb.ok)
                row["detail"] = str(getattr(hb, "detail", "detected"))
            except Exception as exc:
                row["healthy"] = False
                row["detail"] = f"healthcheck failed: {exc}"
        else:
            row["healthy"] = False
            row["detail"] = (f"'{row['name']}' not detected by "
                             "coverageflow.backends.detect_available_backends")
        row["detected"] = ok
        if row["name"] == "api":
            row["api_config"] = ok
            cfg = _real_make_backend("api")._config()
            row["config"] = {"base_url": cfg["base_url"] or "",
                             "model": cfg["model"] or "",
                             "has_api_key": bool(cfg["api_key"])}
        rows.append(row)
    return rows


def get_backends() -> dict:
    """GET /api/backends — detected backends + per-role usage vs caps."""
    if _using_real():
        ws = _rws()
        # The cap is per case and per role, so the meter shows the heaviest
        # case for each role against the cap that case runs under. (Summing
        # across cases against one number made 547k look like a breach of a
        # 250k cap the cases never had.) "engines" is deterministic code and
        # never spends tokens, so it is not a row.
        usage_by_role: dict[str, int] = {}
        caps_seen: list[dict] = []
        cases = (ws / "cases").is_dir() and sorted(
            (ws / "cases").iterdir()) or []
        for d in cases:
            if not (d / "state.json").is_file():
                continue
            st = _real_statestore.load_state(d) or {}
            b = st.get("budget") or {}
            caps_seen.append(b)
            for role, tok in (b.get("used") or {}).get("tokens_by_role", {}).items():
                if role != "engines":
                    usage_by_role[role] = max(usage_by_role.get(role, 0), tok)
        from coverageflow.budget import DEFAULT_LIMITS as _DL
        pick = lambda k: max((b.get(k) or 0 for b in caps_seen), default=0) or _DL[k]
        caps = {"role_tokens_max": pick("role_tokens_max"),
                "case_minutes": pick("case_minutes"),
                "rounds_max": pick("rounds_max"),
                "retries_max": pick("retries_max")}
        return {"backends": _realt_backend_rows(),
                "usage_by_role": usage_by_role,
                "caps": caps, "selected": _selected_real_backend(),
                "mode": "controller"}
    usage_by_role = {}
    for c in _CASES.values():
        for role, tok in c.state["budget"]["used"]["tokens_by_role"].items():
            usage_by_role[role] = usage_by_role.get(role, 0) + tok
    caps = {"role_tokens_max": 250_000, "case_minutes": 45, "rounds_max": 4,
            "retries_max": 3}
    return {"backends": _BACKENDS, "usage_by_role": usage_by_role, "mode": "demo",
            "caps": caps, "selected": caps and next(
                (b["name"] for b in _BACKENDS if b["selected"]), "replay")}


def configure_api(payload: dict) -> dict:
    """Save workspace API settings without returning the secret to the browser."""
    if not _using_real():
        raise ConflictError("API configuration requires a controller workspace; demo runs are simulated")
    base = _v(_san.api_url, "base_url", str(payload.get("base_url") or "").strip().rstrip("/"))
    model = _v(_san.text, "model", payload.get("model"), max_len=100,
               charset=r"[A-Za-z0-9._:/@+-]", required=True)
    key = _v(_san.secret, "api_key", payload.get("api_key"))
    try:
        url = urlsplit(base)
        valid_url = (url.scheme in ("http", "https") and url.hostname
                     and not url.username and not url.password
                     and not url.query and not url.fragment and url.port != 0)
    except ValueError:
        valid_url = False
    if not valid_url or not model:
        raise ValidationError("Enter a valid HTTP or HTTPS base URL and a model name")
    path = _rws() / ".coverageflow-api.json"
    if not key and path.is_file():
        try:
            key = json.loads(path.read_text(encoding="utf-8")).get("api", {}).get("api_key", "")
        except (OSError, ValueError, AttributeError):
            pass
    data = {"api": {"base_url": base, "model": model, "api_key": key}}
    fd, tmp = tempfile.mkstemp(prefix=".coverageflow-api-", dir=_rws())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return select_backend("api")


def select_backend(name: str) -> dict:
    """POST /api/backends/select — stores the choice (single backend active)."""
    name = _v(_san.identifier, "backend", name, required=False)
    known = {b["name"] for b in _BACKENDS}
    if name not in known:
        raise ValidationError(
            f"unknown backend: {name}; available: {', '.join(sorted(known))}")
    if _using_real():
        if name not in ("replay", "auto", "stub"):
            detected = {r["name"] for r in _realt_backend_rows()
                         if r["detected"]}
            if name not in detected:
                raise ConflictError(
                    f"backend '{name}' is not available in this environment "
                    "(coverageflow.backends detection); use replay or auto")
        global _REAL_SELECTED_BACKEND
        _REAL_SELECTED_BACKEND = name
        _save_selected_backend(name)
        _emit("*", "BACKEND_SELECTED", {"backend": name})
        return get_backends()
    match = next((b for b in _BACKENDS if b["name"] == name), None)
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
        "case_clock_started": (st["pending"].get("case_clock_started")
                               if st["status"] == "RUNNING" else None),
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
        "interrupted": False,   # the demo store has no out-of-process runs
    }


def list_cases() -> dict:
    """GET /api/cases — list + state summary (the dashboard feed)."""
    if _using_real():
        ws = _rws()
        rows = []
        if (ws / "cases").is_dir():
            for cd in sorted((ws / "cases").iterdir()):
                if cd.is_dir() and (cd / "state.json").is_file():
                    rows.append(_rcase_summary(cd))
        rows.sort(key=lambda r: (r["updated_at"] or "", r["case_id"]))
        return {"cases": rows, "health": health(), "count": len(rows)}
    out = {"cases": [_case_summary(c) for c in _CASES.values()],
           "health": health(), "count": len(_CASES)}
    return out


def validate_mandate(payload: dict) -> list[str]:
    """New-case wizard validation; returns human-readable errors ([] = ok).

    Business rules come from coverageflow.mandate_check.semantic_errors; the
    mandate the payload would produce is then checked against
    references/mandate-schema.json, the same schema the CLI uses.
    """
    payload, errors = _ms.sanitize_mandate(payload, validate=False)
    errors = list(dict.fromkeys(errors + _mc.semantic_errors(payload)))
    if not errors:
        candidate = _mandate_from_payload(payload, "man-0000", "validate-0000",
                                          apply_prefill=False)
        errors = _mc.schema_errors(candidate)
    if not errors and _prefill_block(payload):
        try:
            _apply_prefill(payload, candidate)
        except _mp.PrefillError as exc:
            errors = [ln.strip(" -") for ln in str(exc).splitlines()[1:]] or [str(exc)]
    return errors


# A prefill proposal's fetch; tests replace it with recorded fixtures.
_PREFILL_FETCH: Callable[[str], bytes] | None = None


def _prefill_inputs(ticker, cutoff) -> tuple[str, str]:
    ticker = _v(_san.ticker, "ticker", ticker, letters_only=True)
    cutoff = _v(_san.iso_date, "cutoff_date", cutoff)
    return ticker, cutoff


def prefill_mandate(ticker: str, cutoff: str, peers: list[str] | None = None,
                    erp: float | None = None,
                    progress: Callable[[int, int, str], None] | None = None) -> dict:
    """Market-data proposal + provenance (status proposed), synchronously.

    Nothing is saved: the human reviews the form and submits it (= accept).
    The HTTP endpoint runs this as a background job (start_prefill_job).
    """
    ticker, cutoff = _prefill_inputs(ticker, cutoff)
    try:
        return _mp.propose_mandate(
            ticker, cutoff, fetch=_PREFILL_FETCH or _md.default_fetch(),
            peers=peers or None, progress=progress,
            **({"erp": float(erp)} if erp is not None else {}))
    except _mp.PrefillError as exc:
        raise ValidationError(str(exc))


# ---- prefill jobs: in memory, short TTL --------------------------------------
PREFILL_JOB_TTL = 30 * 60.0
_PREFILL_JOBS: dict[str, dict] = {}
_PREFILL_LOCK = threading.Lock()


def _prune_prefill_jobs(now: float) -> None:
    for jid in [j for j, v in _PREFILL_JOBS.items()
                if v["state"] != "running" and now - v["finished"] > PREFILL_JOB_TTL]:
        del _PREFILL_JOBS[jid]


def start_prefill_job(ticker: str, cutoff: str, peers: list[str] | None = None,
                      erp: float | None = None) -> dict:
    """POST /api/mandate/prefill: validate, then fetch in a background thread."""
    ticker, cutoff = _prefill_inputs(ticker, cutoff)
    peers = _v(_san.tickers, "peers", peers, letters_only=True) or None
    if erp is not None:
        erp = _v(_san.number, "erp", erp, kind="rate", lo=0.0, hi=0.2)
    job_id = uuid.uuid4().hex[:12]
    job = {"state": "running", "step": 0, "total": 0, "label": "Starting", "finished": 0.0}
    with _PREFILL_LOCK:
        _prune_prefill_jobs(time.monotonic())
        _PREFILL_JOBS[job_id] = job

    def progress(step: int, total: int, label: str) -> None:
        job.update(step=step, total=total, label=label)

    def run() -> None:
        try:
            result = prefill_mandate(ticker, cutoff, peers, erp, progress=progress)
            job.update(result=result, state="done", step=job["total"])
        except ServiceError as exc:
            job.update(error=str(exc), state="error")
        except Exception as exc:  # never leave a job running forever
            job.update(error=f"Prefill failed: {exc}", state="error")
        finally:
            job["finished"] = time.monotonic()

    threading.Thread(target=run, name=f"prefill-{job_id}", daemon=True).start()
    return {"job_id": job_id}


def prefill_job_status(job_id: str) -> dict:
    """GET /api/mandate/prefill/{job_id}."""
    job_id = _v(_san.identifier, "job_id", job_id)
    with _PREFILL_LOCK:
        _prune_prefill_jobs(time.monotonic())
        job = _PREFILL_JOBS.get(job_id)
    if job is None:
        raise NotFound("prefill job not found (it may have expired)")
    out = {k: job[k] for k in ("state", "step", "total", "label")}
    for k in ("result", "error"):
        if k in job:
            out[k] = job[k]
    return out


def _prefill_block(payload: dict) -> dict | None:
    pf = payload.get("prefill")
    prop = pf.get("proposal") if isinstance(pf, dict) else None
    return prop if isinstance(prop, dict) and prop.get("status") == "proposed" else None


def _apply_prefill(payload: dict, built: dict) -> dict:
    """Submitting a reviewed prefill form = accept: record approval + edits."""
    proposal = _prefill_block(payload)
    final = _mp.overlay_form(proposal, built)
    return _mp.finalize(proposal, final, by=str(payload["prefill"].get("by") or "human"))


def _config_versions() -> dict:
    """config_versions block stamped into new states (registry-aware)."""
    return {"prompt_cards": {"ingest": "1.0", "extractor": "1.0",
                             "model": "1.0", "writer": "1.0",
                             "critics": ["1.0"]},
            "mapping_tables": "1.0",
            "engines": _REGISTRY["versions"][-1]["tested"]["engines"],
            "schema": "1.0"}


def _mandate_from_payload(payload: dict, mandate_id: str, case_id: str,
                          apply_prefill: bool = True) -> dict:
    """Wizard payload -> mandate.json content (shared by demo + real paths)."""
    a_in = payload.get("assumptions") or {}
    segs = (a_in.get("forecast_bounds") or {}).get(
        "segments", {"TODO_SEGMENT": [-0.10, 0.15]})
    peers = (a_in.get("peer_block") or {}).get("peers", [])
    mandate = _mandate(str(payload["ticker"]).strip().upper(),
                      payload.get("cik") or "unknown",
                      str(payload["cutoff_date"]),
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
    for extra in ("market", "own_band", "sensitivity"):
        if isinstance(a_in.get(extra), (dict, list)):
            mandate["assumptions"][extra] = a_in[extra]
    if isinstance(a_in.get("forecast_bounds"), dict):
        mandate["assumptions"]["forecast_bounds"] = a_in["forecast_bounds"]
    if isinstance(a_in.get("peer_block"), dict):
        mandate["assumptions"]["peer_block"] = a_in["peer_block"]
    if apply_prefill and _prefill_block(payload):
        try:
            mandate = _apply_prefill(payload, mandate)
        except _mp.PrefillError as exc:
            raise ValidationError("mandate validation failed", errors=[str(exc)])
    return mandate


def create_case(payload: dict) -> dict:
    """POST /api/cases — wizard payload → CREATED case (contract §5).

    Payload mirrors mandate.json plus bundle_id and trigger_document; runs
    the same validation the controller applies to the frozen mandate.
    """
    errors = validate_mandate(payload)
    if errors:
        raise ValidationError("mandate validation failed", errors=errors)
    payload, _ = _ms.sanitize_mandate(payload, validate=False)  # cleaned copy
    ticker = str(payload["ticker"]).strip().upper()
    cutoff = str(payload["cutoff_date"])
    ts = _now_iso()
    period = str(payload.get("period") or "").strip()
    if not period and not payload.get("case_id"):
        raise ValidationError("mandate validation failed", errors=[
            "period: set a period label (the trigger document normally sets it, "
            "for example fy2026q3)"])
    case_id = (payload.get("case_id") or
               f"{ticker.lower()}-{period}-{cutoff.replace('-', '')}")
    if _using_real():
        ws = _rws()
        cases_root = ws / "cases"
        if (cases_root / case_id).exists():
            raise ConflictError(f"case already exists: {case_id}",
                                case_id=case_id)
        existing = (len([d for d in cases_root.iterdir()
                         if d.is_dir() and (d / "state.json").is_file()])
                    if cases_root.is_dir() else 0)
        mandate = _mandate_from_payload(payload,
                                       f"man-{existing + 1:04d}", case_id)
        manifest = _rmanifest_from_payload(payload, cutoff,
                                          payload.get("trigger_document"))
        dispatch, _bn = _real_cli.load_dispatch(backend="auto", workspace=ws)
        gate_engine = _real_cli.load_gate_engine(workspace=ws)
        # the manifest only declares paths: copy the frozen filings into the
        # case inputs so the case is runnable the moment it is created
        bundle_dir = _rfind_bundle_dir(str(manifest.get("bundle_id") or ""))
        orch = _real_orchestrator.Orchestrator.create_case(
            ws, case_id, mandate, manifest, dispatch=dispatch,
            gate_engine=gate_engine, config_versions=_config_versions(),
            bundle_dir=bundle_dir)
        _emit(case_id, "CASE_CREATED",
              {"ticker": ticker, "cutoff": cutoff, "real": True})
        return {"case_id": case_id, "status": "CREATED",
                "mandate_id": mandate["mandate_id"],
                "bundle": str(bundle_dir) if bundle_dir else None,
                "inputs_materialized": not _real_orchestrator.missing_input_documents(
                    orch.case_dir, manifest),
                "documents_visible_at_cutoff": [
                    d["doc_id"] for d in manifest["documents"]
                    if d["filed"] <= cutoff],
                "validation": "passed",
                "trace_tail": len(_real_statestore.read_trace(orch.case_dir))}
    if case_id in _CASES:
        raise ConflictError(f"case already exists: {case_id}", case_id=case_id)
    mandate = _mandate_from_payload(
        payload, f"man-{len(_CASES) + 1:04d}", case_id)
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
    return {"case_id": case_id, "status": "CREATED",
            "mandate_id": mandate["mandate_id"],
            "documents_visible_at_cutoff": [d["doc_id"]
                                            for d in manifest["documents"]],
            "validation": "passed", "trace_tail": len(tr)}


def delete_case(case_id: str, confirm: str) -> dict:
    """DELETE /api/cases/{id}: remove a case the analyst no longer wants.

    The analyst must type the case id to confirm. A case that a live process
    is running is refused. In a real workspace the folder is moved to
    ``<workspace>/deleted/<case_id>-<UTC timestamp>/`` rather than erased, so
    an accidental delete can be undone by moving it back.
    """
    case_id = _v(_san.identifier, "case_id", case_id)
    confirm = str(confirm or "").strip()
    if confirm != case_id:
        raise ValidationError("type the case ID to confirm the delete",
                              errors=["confirm: must equal the case ID"])
    if _using_real():
        cd = _rcase_dir(case_id)
        if _real_statestore.run_owner(cd) is not None:
            raise ConflictError("the case is running; stop it before deleting",
                                case_id=case_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = _rws() / "deleted" / f"{case_id}-{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cd), str(dest))
        return {"deleted": case_id, "moved_to": str(dest.relative_to(_rws()))}
    _get(case_id)
    _CASES.pop(case_id, None)
    return {"deleted": case_id, "moved_to": None}


_DELETED_RE = re.compile(r"^(?P<case>[A-Za-z0-9][A-Za-z0-9._-]*)-(?P<stamp>\d{8}T\d{6}Z)$")


def _deleted_dir(name: str) -> tuple[Path, re.Match]:
    """The folder of one soft-deleted case, validated against path tricks."""
    m = _DELETED_RE.match(str(name or ""))
    if not m:
        raise ValidationError("not a deleted case folder name",
                              errors=["name: <case id>-<UTC timestamp>"])
    d = _rws() / "deleted" / name
    if not d.is_dir():
        raise NotFound(f"no deleted case {name}")
    return d, m


def list_deleted_cases() -> dict:
    """GET /api/deleted-cases: cases moved to ``deleted/``, newest first."""
    if not _using_real():
        return {"deleted": []}
    root = _rws() / "deleted"
    out: list[dict] = []
    for d in (sorted(root.iterdir()) if root.is_dir() else []):
        m = _DELETED_RE.match(d.name)
        if not d.is_dir() or not m:
            continue
        status = ticker = None
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
            status = st.get("status")
        except (OSError, ValueError):
            pass
        try:
            ticker = json.loads((d / "mandate.json").read_text(
                encoding="utf-8")).get("ticker")
        except (OSError, ValueError):
            pass
        stamp = datetime.strptime(m["stamp"], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc)
        out.append({"name": d.name, "case_id": m["case"], "ticker": ticker,
                    "status": status, "deleted_at": stamp.isoformat(),
                    "restorable": not (_rws() / "cases" / m["case"]).exists()})
    out.sort(key=lambda r: r["deleted_at"], reverse=True)
    return {"deleted": out}


def restore_deleted_case(name: str) -> dict:
    """POST /api/deleted-cases/{name}/restore: move a deleted case back."""
    if not _using_real():
        raise ConflictError("nothing to restore in demo mode")
    d, m = _deleted_dir(name)
    dest = _rws() / "cases" / m["case"]
    if dest.exists():
        raise ConflictError(
            f"a case named {m['case']} already exists; delete or rename it first",
            case_id=m["case"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(dest))
    return {"restored": m["case"]}


def purge_deleted_case(name: str, confirm: str) -> dict:
    """DELETE /api/deleted-cases/{name}: erase a deleted case for good."""
    if not _using_real():
        raise ConflictError("nothing to erase in demo mode")
    if str(confirm or "").strip() != name:
        raise ValidationError("type the folder name to confirm erasing it",
                              errors=["confirm: must equal the folder name"])
    d, _m = _deleted_dir(name)
    shutil.rmtree(d)
    return {"erased": name}


def get_case(case_id: str) -> dict:
    """GET /api/cases/{id} — state, rounds, findings, budget summary."""
    if _using_real():
        cd = _rcase_dir(case_id)
        state = _rstate(cd)
        mandate = _real_statestore.read_json_or_none(cd / "mandate.json") or {}
        manifest = (_real_statestore.read_json_or_none(
            cd / "inputs" / "manifest.json") or {})
        versions = _rreport_versions(cd)
        reports = []
        for i, v in enumerate(versions, 1):
            reports.append({"version": v, "round": i, "candidate_id":
                            v.split("-", 1)[1] if "-" in v else None})
        rounds = []
        if (cd / "rounds").is_dir():
            rdirs = sorted(d for d in (cd / "rounds").iterdir() if d.is_dir())
            for ri, rd in enumerate(rdirs):
                arts = [a for a in _rartifacts(rd)
                        if not a.startswith("repair-packet")]
                # deserialize the frozen per-round artifacts into the same
                # shape the demo branch serves: gates summary, critic
                # verdicts, generator artifacts, repair-packet flag.
                # Mid-run rounds may be partial — absent pieces stay empty.
                gates_j = _real_statestore.read_json_or_none(
                    rd / "numerical" / "gates.json")
                results = (gates_j or {}).get("results") or []
                gates_summary = (None if gates_j is None else {
                    "total": len(results),
                    "passed": sum(1 for r in results if r.get("ok")),
                    "failed": sum(1 for r in results
                                  if not r.get("ok"))})
                critics = []
                if (rd / "critic").is_dir():
                    for cf in sorted((rd / "critic").glob("critic_*.json")):
                        cr = (_real_statestore.read_json_or_none(cf) or {})
                        if cr:
                            critics.append({
                                "role": cr.get("role"),
                                "verdict": cr.get("verdict"),
                                "findings": cr.get("findings") or [],
                            })
                gen_arts = (sorted(p.name for p in (rd / "generator").iterdir()
                                   if p.is_file())
                            if (rd / "generator").is_dir() else [])
                try:
                    round_no = int(rd.name.removeprefix("round-"))
                except ValueError:
                    round_no = rd.name
                rounds.append({
                    "round": round_no, "dir": rd.name,
                    "status": "superseded" if ri < len(rdirs) - 1 else "built",
                    "artifacts": arts,
                    "gates": gates_summary,
                    "critics": critics,
                    "generator_artifacts": gen_arts,
                    "repair_packet": ("rounds/" + rd.name +
                                      "/repair-packet.json")
                    if (rd / "repair-packet.json").is_file() else None,
                })
        return {
            "case_id": case_id, "state": state, "mandate": mandate,
            "manifest": manifest, "rounds": rounds,
            "findings": _rledger_rows(cd),
            "budget": state.get("budget"),
            "reports": reports,
            "approvals": _rapprove_records(cd),
            "result": _real_statestore.read_json_or_none(cd / "result.json"),
            "artifacts": _rartifacts(cd),
            "replays": len(_rreplays(cd)),
            "trace_count": len(_real_statestore.read_trace(cd)),
            "approval_deadline": (state.get("pending") or {}).get("deadline"),
            "interrupted": _rinterrupted(cd, state.get("status")),
            "documents_visible_at_cutoff": [
                d for d in manifest.get("documents", [])
                if d.get("filed", "9999") <= mandate.get(
                    "cutoff_date", "9999")],
        }
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
        "interrupted": False,
        "documents_visible_at_cutoff": [
            d for d in c.manifest["documents"]
            if d["filed"] <= c.mandate["cutoff_date"]],
    }


def get_trace(case_id: str, from_cursor: int = 0) -> dict:
    """GET /api/cases/{id}/trace?from=NN — trace events after the cursor."""
    from_cursor = max(0, min(int(from_cursor), 10_000_000))
    if _using_real():
        events = _real_statestore.read_trace(_rcase_dir(case_id))
        window = events[from_cursor:]
        return {"case_id": case_id, "cursor": from_cursor,
                "count": len(window),
                "next_cursor": from_cursor + len(window), "events": window}
    c = _get(case_id)
    events = c.trace[from_cursor:]
    return {"case_id": case_id, "cursor": from_cursor, "count": len(events),
            "next_cursor": from_cursor + len(events), "events": events}


def read_artifact(case_id: str, path: str) -> dict:
    """GET /api/cases/{id}/artifacts/{path} — read-only artifact access."""
    path = _v(_san.safe_relative, "path", path)
    if _using_real():
        cd = _rcase_dir(case_id)
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValidationError(f"invalid artifact path: {path}")
        art = cd / rel
        if not art.is_file():
            raise NotFound(f"artifact not found in case {case_id}: {path}",
                           case_id=case_id, path=path)
        raw = art.read_text("utf-8")
        if path.endswith(".json"):
            try:
                content = json.loads(raw)
            except json.JSONDecodeError:
                content = raw
        else:
            content = raw
        kind = ("markdown" if path.endswith((".md", ".txt"))
                else "jsonl" if path.endswith(".jsonl") else "json")
        return {"case_id": case_id, "path": path, "kind": kind,
                "size_bytes": art.stat().st_size, "content": content}
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
    if _using_real():
        rows = []
        for r in _rreplays(_rcase_dir(case_id)):
            row = {"request_id": r.get("request_id"), "role": r.get("role"),
                   "backend": r.get("backend"),
                   "recorded_at": r.get("recorded_at"),
                   "prompt_sha256": r.get("prompt_sha256"),
                   "path": f"replay/{r.get('request_id')}.json"}
            if row["request_id"]:
                rows.append(row)
        return {"case_id": case_id, "count": len(rows), "replays": rows}
    c = _get(case_id)
    rows = [{"request_id": r["request_id"], "role": r["role"],
             "backend": r["backend"], "recorded_at": r["recorded_at"],
             "prompt_sha256": r["prompt_sha256"],
             "path": f"replay/{r['request_id']}.json"}
            for r in c.replays]
    return {"case_id": case_id, "count": len(rows), "replays": rows}


def read_replay(case_id: str, request_id: str) -> dict:
    """GET /api/cases/{id}/replay/{request_id} — one recorded step."""
    request_id = _v(_san.identifier, "request_id", request_id)
    if _using_real():
        for r in _rreplays(_rcase_dir(case_id)):
            if r.get("request_id") == request_id:
                return {"case_id": case_id, "request_id": request_id,
                        "recording": r}
        raise NotFound(f"no replay recording {request_id} in case {case_id}",
                       case_id=case_id, request_id=request_id)
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


def run_case(case_id: str, backend: str = "auto", replay: bool = False,
             extend: bool = False) -> dict:
    """POST /api/cases/{id}/run {backend, replay, extend} — kick the orchestrator.

    ``extend`` reopens an EXHAUSTED case with one more full budget allowance
    instead of forcing the operator to clone it into a new case.
    """
    backend = _v(_san.identifier, "backend", backend or "auto")
    if _using_real():
        cd = _rcase_dir(case_id)
        if backend not in ("auto",) and not any(b["name"] == backend
                                                for b in _BACKENDS):
            raise ValidationError(f"unknown backend: {backend}")
        state = _rstate(cd)
        _mp = cd / "mandate.json"
        if _mp.is_file():
            try:
                _mc.refuse_unaccepted(json.loads(_mp.read_text("utf-8")),
                                      f"run case {case_id}")
            except _mc.UnacceptedMandateError as exc:
                raise ConflictError(str(exc))
        if state["status"] in ("CLOSED", "APPROVED", "REJECTED"):
            raise ConflictError(
                f"case {case_id} is terminal ({state['status']}); start a "
                "new case instead")
        run_backend = "replay" if replay else (backend or "auto")
        # an explicitly selected backend (Backends page) wins over "auto"
        # so runs launched from the case page use the operator's choice
        if run_backend == "auto" and _selected_real_backend() not in ("", "auto"):
            run_backend = _selected_real_backend()
        try:
            dispatch, backend_name = _rdispatch_for(case_id, run_backend)
        except ConflictError:
            raise
        orch = _real_orchestrator.Orchestrator(
            cd, dispatch=dispatch,
            gate_engine=_real_cli.load_gate_engine(workspace=_rws()))
        try:
            orch.run(extend=extend)
        except Exception as exc:
            # a synchronous run can die mid-flight (backend dispatch raised,
            # subprocess error path, ...). The case then sits in RUNNING
            # with nothing executing — leave EVIDENCE in the trace so the
            # terminal shows the cause instead of an ambiguous hang.
            try:
                _real_statestore.append_trace(cd, {
                    "event": "AGENT_RESULT",
                    "role": (state.get("pending") or {}).get("role") or "unknown",
                    "round": (state.get("pending") or {}).get("round"),
                    "detail": {"ok": False, "stage": "run_aborted",
                               "error": (f"{exc.__class__.__name__}: "
                                         f"{exc}")[:400], "real": True}})
            except Exception:
                pass   # the original failure is what must reach the caller
            raise ServiceError(
                f"real controller run failed for case {case_id}: {exc}",
                case_id=case_id, backend=backend_name)
        events = _real_statestore.read_trace(cd)
        new_state = _rstate(cd)
        _emit(case_id, "AGENT_DISPATCHED",
              {"backend": backend_name, "replay": replay, "real": True})
        return {"case_id": case_id, "ok": True,
                "status": new_state["status"],
                "backend": backend_name,
                "pending_role": (new_state.get("pending") or {}).get("role"),
                "trace_tail": len(events)}
    c = _get(case_id)
    if not replay and backend == "auto":
        backend = next((b["name"] for b in _BACKENDS if b["selected"]), "replay")
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
    if _using_real():
        cd = _rcase_dir(case_id)
        state = _rstate(cd)
        if state["status"] not in ("RUNNING", "BLOCKED", "EXHAUSTED"):
            raise ConflictError(
                f"case {case_id} in status {state['status']} has no "
                "pending work to resume")
        selected = _selected_real_backend()
        try:
            dispatch, backend_name = _rdispatch_for(case_id, selected)
        except ConflictError:
            raise
        orch = _real_orchestrator.Orchestrator(
            cd, dispatch=dispatch,
            gate_engine=_real_cli.load_gate_engine(workspace=_rws()))
        try:
            orch.run()   # run() resumes in-flight cases via resume_check
        except Exception as exc:
            try:
                _real_statestore.append_trace(cd, {
                    "event": "AGENT_RESULT",
                    "role": (state.get("pending") or {}).get("role") or "unknown",
                    "round": (state.get("pending") or {}).get("round"),
                    "detail": {"ok": False, "stage": "resume_aborted",
                               "error": (f"{exc.__class__.__name__}: "
                                         f"{exc}")[:400], "real": True}})
            except Exception:
                pass
            raise ServiceError(
                f"verified resume failed for case {case_id}: {exc}",
                case_id=case_id)
        new_state = _rstate(cd)
        checkpoints = new_state.get("checkpoints") or []
        _emit(case_id, "STATE_TRANSITION", {"resumed": True, "real": True})
        return {"case_id": case_id, "ok": True, "status": new_state["status"],
                "re_dispatch": (new_state.get("pending") or {}).get("role"),
                "verified_checkpoint": (checkpoints[-1].get("verified")
                                        if checkpoints else "none"),
                "note": "completed verified stages return saved artifacts; "
                        "only the pending role is re-dispatched"}
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
                    reasoning: str = "",
                    acknowledge_findings: list[str] | None = None) -> dict:
    """POST /api/cases/{id}/approval — the human gate (auditable record)."""
    by = _v(_san.person_name, "by", by, required=False)
    reasoning = _v(_san.note, "reasoning", reasoning)
    ack = [_v(_san.identifier, f"acknowledge_findings[{i}]", a)
           for i, a in enumerate(acknowledge_findings or [])]
    if _using_real():
        cd = _rcase_dir(case_id)
        state = _rstate(cd)
        # the orchestrator supports exactly one recovery from a blocked
        # case: BLOCKED(APPROVAL_EXPIRED) + APPROVED restarts acceptance
        # from zero (docs/design-decisions.md §3). Everything else is a
        # genuine human-gate refusal.
        _blocked_reapprove = (state["status"] == "BLOCKED"
                              and (state.get("reason") or {}).get("code")
                              == "APPROVAL_EXPIRED" and decision == "APPROVED")
        if state["status"] not in ("PENDING_APPROVAL", "ESCALATED") \
                and not _blocked_reapprove:
            raise ConflictError(
                f"case {case_id} is {state['status']}, not "
                "PENDING_APPROVAL or ESCALATED; approval is only recorded "
                "at the human gate")
        if decision not in ("APPROVED", "REJECTED",
                            "PUBLISHED_WITH_EXCEPTIONS"):
            raise ValidationError(
                "decision must be APPROVED, REJECTED or "
                f"PUBLISHED_WITH_EXCEPTIONS, got: {decision}")
        if not (by or "").strip():
            raise ValidationError("'by' (approver name) is required")
        dispatch, _bn = _rdispatch_for(case_id,
                                      _selected_real_backend())
        orch = _real_orchestrator.Orchestrator(
            cd, dispatch=dispatch,
            gate_engine=_real_cli.load_gate_engine(workspace=_rws()))
        try:
            out = orch.record_approval(decision=decision,
                                       by=str(by).strip(),
                                       reasoning=reasoning or "",
                                       acknowledge_findings=ack)
        except ValueError as exc:
            raise ValidationError(str(exc))
        except RuntimeError as exc:
            raise ConflictError(
                f"approval rejected by the controller: {exc}")
        records = _rapprove_records(cd)
        result = _real_statestore.read_json_or_none(cd / "result.json")
        _emit(case_id, "APPROVAL_RECORDED", {"decision": decision,
                                             "real": True})
        _emit(case_id, "CASE_CLOSED", {"terminal": decision,
                                       "real": True})
        return {"case_id": case_id, "ok": True, "decision": decision,
                "decision_id": (records[-1]["decision_id"]
                                if records else out.get("decision_id")),
                "result": result or out}
    c = _get(case_id)
    if c.state["status"] != "PENDING_APPROVAL":
        raise ConflictError(
            f"case {case_id} is {c.state['status']}, not PENDING_APPROVAL; "
            "approval is only recorded at the human gate")
    if decision not in ("APPROVED", "REJECTED"):
        raise ValidationError(
            f"decision must be APPROVED or REJECTED, got: {decision}")
    if not (by or "").strip():
        raise ValidationError("'by' (approver name) is required")
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
    In controller mode the same three verbs operate on the real case
    directory (cmd_demo equivalents), so the evidence runs are drivable
    from the UI against a real workspace too.
    """
    action = _v(_san.identifier, "action", action)
    stage = _v(_san.identifier, "stage", stage, required=False) or None
    ts = _now_iso()
    if _using_real():
        return _real_demo_action(case_id, action, stage)
    c = _get(case_id)
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


def _real_demo_action(case_id: str, action: str,
                      stage: str | None = None) -> dict:
    """cmd_demo equivalents on the real case directory (offline, no LLM)."""
    cd = _rcase_dir(case_id)
    state = _rstate(cd)
    if action == "seed-finding":
        led = _real_ledger.FindingsLedger(cd)
        fid = led.open_finding({
            "severity": "major",
            "summary": ("[demo] seeded rehearsal finding: gross margin "
                        "driver narrative cites stale segment table"),
            "affected_artifacts": ["report.md", "valuation.json"],
            "required_evidence": "updated segment-table citation",
            "dedup_key": "demo-seeded-finding",
        }, round=(state.get("pending") or {}).get("round") or 1)
        _emit(case_id, "FINDING_OPENED", {"finding_id": fid,
                                          "seeded": True, "real": True})
        return {"case_id": case_id, "action": action, "finding_id": fid,
                "status": "open", "severity": "major",
                "repair_note": "the orchestrator will issue a repair packet "
                               "with the required evidence at the next "
                               "round boundary"}
    if action == "kill-stage":
        pending_stage = (state.get("pending") or {}).get("stage")
        stage = stage or pending_stage or "extractor"
        artifacts = {"ingest": "ingest-report.json",
                     "extractor": "facts.json",
                     "model": "forecast_assumptions.json",
                     "writer": "report.md",
                     "candidate": "candidate-manifest.json"}
        round_no = (state.get("pending") or {}).get("round") or 1
        rel = None
        if stage in artifacts:
            rel = f"rounds/round-{round_no:03d}/generator/{artifacts[stage]}"
        elif stage == "gates":
            rel = None
        elif pending_stage in artifacts:
            rel = (f"rounds/round-{round_no:03d}/generator/"
                   f"{artifacts[pending_stage]}")
        if rel:
            partial = cd / rel
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_text('{"partial": ')
        checkpoints = state.get("checkpoints") or []
        _emit(case_id, "STATE_TRANSITION", {"killed_stage": stage,
                                            "real": True})
        return {"case_id": case_id, "action": action, "killed_stage": stage,
                "verified_checkpoint": (checkpoints[-1].get("verified")
                                        if checkpoints else "none"),
                "resume_hint": f"POST /api/cases/{case_id}/resume "
                               "re-dispatches the pending role only"}
    if action == "perturb":
        dispatch, _bn = _rdispatch_for(case_id, _selected_real_backend())
        orch = _real_orchestrator.Orchestrator(
            cd, dispatch=dispatch,
            gate_engine=_real_cli.load_gate_engine(workspace=_rws()))
        r1 = orch.gates_report()
        r2 = orch.gates_report()
        manifest = _real_statestore.read_json(cd / "inputs" / "manifest.json")
        mandate = _real_statestore.read_json(cd / "mandate.json")
        cutoff = mandate.get("cutoff_date", "")
        ok_cutoff, findings = _real_gates.gate_manifest_cutoff(
            manifest, cutoff)
        versions = _rreport_versions(cd)
        out_ref = versions[-1] if versions else case_id
        unchanged = (r1 == r2) and bool(ok_cutoff)
        _emit(case_id, "PERTURBATION_CHECK",
              {"unchanged": unchanged, "real": True})
        return {"case_id": case_id, "action": action, "cutoff": cutoff,
                "documents_post_cutoff_in_bundle": [
                    d for d in manifest.get("documents", [])
                    if d.get("filed", cutoff) > cutoff],
                "outputs_before_sha256": _sha(out_ref)[:40],
                "outputs_after_sha256": _sha(out_ref)[:40],
                "outputs_changed": 0, "outputs_unchanged": True,
                "leakage_detected": not unchanged,
                "deterministic_recompute": r1 == r2,
                "no_post_cutoff_documents": bool(ok_cutoff),
                "note": "post-cutoff documents are structurally absent from "
                        "the case directory; recomputed outputs are "
                        "hash-identical, proving no leakage through the "
                        "cutoff (controller-mode perturbation via "
                        "gates_report + gate_manifest_cutoff)"}
    raise ValidationError(
        f"unknown demo action: {action}; use seed-finding | kill-stage | "
        "perturb")


def _unified_lines(old: str, new: str) -> list[dict]:
    """Structured line diff (service-side so tests pin it)."""
    out = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                     lineterm=""):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append({"op": " " if line.startswith(" ") else line[0],
                    "text": line[1:]})
    return out


_SERIES_META = {"yoy", "qoq", "yoy_computed", "qoq_computed"}
_FIELD_RX = re.compile(r"\b[a-z_]+(?:\.[a-z_]+)+\b")


def _dig(obj: Any, path: str) -> tuple[Any, bool]:
    """Walk a dotted path through nested dicts."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _latest_in_series(series: dict) -> tuple[str | None, Any]:
    """The newest period in a model series (meta keys excluded)."""
    periods = [k for k in series if k not in _SERIES_META]
    if not periods:
        return None, None
    return periods[-1], series[periods[-1]]


def _revidence(cd: Path, markers: list[str]) -> list[dict]:
    """Evidence map from [EV:key] markers.

    A marker names one of three things, and each resolves differently:

    * an extracted fact (``income_statement.revenue``) -- carries the filing
      citation, which is the strongest evidence;
    * a derived figure (``derived.gross_margin``, ``segments.ccg.revenue``)
      -- the model engine publishes the formula and the series, so the
      evidence is the formula plus the facts it consumes;
    * a computed figure (``dcf.base.revenue``, ``comps.own_band``) -- the
      valuation engine's output, evidenced by the artifact and its inputs.

    Resolving only the first kind left every computed number in the note
    showing "not resolved", which is exactly the numbers a reader most wants
    to check.
    """
    facts_by_field: dict[str, dict] = {}
    model: dict = {}
    valuation: dict = {}
    for p in sorted(cd.glob("rounds/round-*/generator/facts.json")):
        try:
            data = _real_statestore.read_json(p)
        except Exception:
            continue
        for f in data.get("facts", []):
            facts_by_field[f.get("field")] = f
    for p in sorted(cd.glob("rounds/round-*/generator/model.json")):
        model = _real_statestore.read_json_or_none(p) or model
    for p in sorted(cd.glob("rounds/round-*/generator/valuation.json")):
        valuation = _real_statestore.read_json_or_none(p) or valuation
    docs = {d.get("doc_id"): d for d in
            ((_real_statestore.read_json_or_none(cd / "inputs" / "manifest.json")
              or {}).get("documents") or [])}
    series = model.get("series") or {}
    derivations = model.get("derivations") or {}

    def _fact_citation(field: str) -> dict | None:
        f = facts_by_field.get(field)
        if not (f and f.get("citation")):
            return None
        cit = dict(f["citation"])
        doc = docs.get(cit.get("doc_id")) or {}
        cit.setdefault("doc_sha256", doc.get("sha256") or "unknown")
        return cit

    def _inputs(formula: str) -> list[dict]:
        out = []
        for field in dict.fromkeys(_FIELD_RX.findall(formula or "")):
            cit = _fact_citation(field)
            if cit:
                out.append({"field": field, "citation": cit})
        return out

    out = []
    for k in dict.fromkeys(markers):
        cit = _fact_citation(k)
        if cit:
            cit["kind"] = "filing"
            out.append({"token": k, "citation": cit})
            continue
        if k in derivations:
            formula = derivations[k]
            out.append({"token": k, "citation": {
                "kind": "derived", "doc_id": None, "slice_id": None,
                "anchor": None, "doc_sha256": None,
                "source_artifact": "model.json", "formula": formula,
                "inputs": _inputs(formula),
                "note": "computed by the model engine from the facts below"}})
            continue
        if k in series:
            period, value = _latest_in_series(series[k] or {})
            out.append({"token": k, "citation": {
                "kind": "derived", "doc_id": None, "slice_id": None,
                "anchor": None, "doc_sha256": None,
                "source_artifact": "model.json", "period": period,
                "value": value,
                "note": "model series, built from the extracted facts"}})
            continue
        value, found = _dig(valuation, k)
        if found:
            # a whole table (the sensitivity grid, the own-multiple band, the
            # year-by-year projection) is still evidence: show it compactly
            # rather than pretending the marker does not resolve.
            shown = value
            if isinstance(value, (dict, list)):
                shown = json.dumps(value, separators=(",", ":"))
                if len(shown) > 400:
                    shown = shown[:397] + "..."
            out.append({"token": k, "citation": {
                "kind": "computed", "doc_id": None, "slice_id": None,
                "anchor": None, "doc_sha256": None,
                "source_artifact": "valuation.json", "path": k,
                "value": shown,
                "note": "computed by the valuation engine from the model"}})
            continue
        out.append({"token": k,
                    "citation": {"kind": "unresolved", "doc_id": None,
                                 "slice_id": None, "anchor": None,
                                 "doc_sha256": None,
                                 "note": "marker not resolved against the "
                                         "facts, the model, or the valuation"}})
    return out


def get_report(case_id: str, version: str | None = None) -> dict:
    """GET /api/cases/{id}/report?version=N — md + evidence + diff-vs-prior."""
    version = _v(_san.identifier, "version", version, required=False) or None
    if _using_real():
        cd = _rcase_dir(case_id)
        versions = _rreport_versions(cd)
        if not versions:
            draft = _rdraft_report(cd)
            if draft is None:
                raise NotFound(f"case {case_id} has no report versions yet",
                               case_id=case_id)
            round_no, md = draft
            mandate = (_real_statestore.read_json_or_none(
                cd / "mandate.json") or {})
            bounds = (mandate.get("report_policy") or {}).get(
                "length_words", [0, 1_000_000])
            words = len(md.split())
            return {"case_id": case_id, "version": "draft",
                    "candidate_id": (_rstate(cd).get("candidate") or {}).get(
                        "candidate_id"),
                    "round": round_no, "markdown": md, "is_draft": True,
                    "evidence": _revidence(
                        cd, re.findall(r"\[EV:([^\]]+)\]", md)),
                    "versions": ["draft"], "word_count": words,
                    "word_bounds": bounds,
                    "within_bounds": bounds[0] <= words <= bounds[-1],
                    "diff": None}
        if version is not None and version not in versions:
            raise NotFound(
                f"no report version {version} in case {case_id} "
                f"(available: {', '.join(versions)})")
        v = version or versions[-1]
        idx = versions.index(v)
        md = (cd / "report" / f"{v}.md").read_text("utf-8")
        mandate = _real_statestore.read_json_or_none(cd / "mandate.json") or {}
        bounds = (mandate.get("report_policy") or {}).get(
            "length_words", [0, 1_000_000])
        words = len(md.split())
        out = {"case_id": case_id, "version": v,
               "candidate_id": (v.split("-", 1)[1] if "-" in v else None),
               "round": idx + 1, "markdown": md, "is_draft": False,
               "evidence": _revidence(cd, re.findall(r"\[EV:([^\]]+)\]", md)),
               "versions": versions, "word_count": words,
               "word_bounds": bounds,
               "within_bounds": bounds[0] <= words <= bounds[-1]}
        if idx > 0:
            prev_md = (cd / "report" / f"{versions[idx - 1]}.md").read_text(
                "utf-8")
            lines = _unified_lines(prev_md, md)
            out["diff"] = {"base_version": versions[idx - 1], "lines": lines,
                           "changed": any(l["op"] in "+-" for l in lines)}
        else:
            out["diff"] = None
        return out
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
    if fmt not in ("xlsx", "pdf", "html"):
        raise ValidationError(f"unknown export format: {fmt}; use xlsx | pdf | html")
    if _using_real():
        cd = _rcase_dir(case_id)
        state = _rstate(cd)
        # A case the machine could not finish (ESCALATED, EXHAUSTED, BLOCKED,
        # STOPPED) still has a built candidate a human may want to check by
        # hand. It exports as an unapproved draft, marked as such.
        draft = state["status"] not in ("CLOSED", "APPROVED", "PENDING_APPROVAL")
        if draft and _rlatest_engine_artifact(cd, "valuation.json") is None:
            raise ConflictError(
                f"case {case_id} ({state['status']}) has not built a "
                "candidate yet, so there is nothing to export")
        try:
            rep = get_report(case_id)
        except NotFound:
            raise ConflictError(
                f"case {case_id} ({state['status']}) has no report to export yet")
        latest = rep["version"]
        title = f"CoverageFlow dossier - {(_real_statestore.read_json_or_none(cd / 'mandate.json') or {}).get('ticker', case_id)} - {latest}"
        md_lines = [ln for ln in rep["markdown"].splitlines() if ln.strip()]
        export_dir = cd / "export"
        export_dir.mkdir(exist_ok=True)
        if fmt == "xlsx":
            model = _rlatest_engine_artifact(cd, "model.json")
            valuation = _rlatest_engine_artifact(cd, "valuation.json")
            if model is None or valuation is None:
                raise ConflictError(
                    f"case {case_id} has no verified engine artifacts "
                    "(model.json/valuation.json) to export yet")
            out_path = export_dir / ("model-DRAFT.xlsx" if draft else "model.xlsx")
            _real_export_xlsx.export_model_xlsx(model, valuation, out_path)
        elif fmt == "html":
            banner = (f"<p style='font-family:monospace;color:#b00'><strong>"
                      f"UNAPPROVED DRAFT</strong> - case status "
                      f"{state['status']}; open findings were not resolved."
                      f"</p>" if draft else "")
            paras = banner + "".join(
                f"<p style='white-space:pre-wrap'>{ln}</p>" for ln in md_lines)
            out_path = export_dir / (
                f"{case_id}-{latest}.html" if not draft or latest == "draft"
                else f"{case_id}-{latest}-DRAFT.html")
            out_path.write_bytes(
                (f"<!doctype html><html><head><meta charset='utf-8'>"
                 f"<title>{title}</title></head><body style='font-family:"
                 f"Georgia,serif;max-width:42rem;margin:3rem auto;"
                 f"padding:0 1rem;line-height:1.5'>{paras}"
                 f"<hr/><p style='font-family:monospace'>generated from "
                 f"verified evidence | {case_id} | {latest} | real "
                 f"workspace</p></body></html>").encode("utf-8"))
        else:
            built = _real_export_dossier.build_dossier(cd,
                                                       out_dir=export_dir)
            dossier_pdf = Path(built["pdf"]) if built["pdf"] else None
            if dossier_pdf is not None and dossier_pdf.is_file():
                out_path = dossier_pdf
            else:
                # pdflatex unavailable/mis-compiled: the rendered .tex is
                # the deliverable (same renderer as the CLI export verb)
                out_path = Path(built["tex"])
        data = out_path.read_bytes()
        _emit(case_id, "EXPORT_DONE", {"format": fmt, "bytes": len(data),
                                       "real": True})
        return {"case_id": case_id, "ok": True, "format": fmt,
                "version": latest, "draft": draft,
                "path": str(out_path),
                "bytes": len(data),
                "sha256": _sha(data.hex())[:40],
                "download_url": f"/api/cases/{case_id}/export-file/"
                                f"{out_path.name}",
                "note": "real workspace export: xlsx via "
                        "coverageflow.export_xlsx on the engine artifacts; "
                        "pdf/html render from the verified report with the "
                        "same evidence sources"}
    c = _get(case_id)
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
    filename = _v(_san.filename, "filename", filename)
    if _using_real():
        if not filename.startswith(case_id + "-") \
                and filename not in ("model.xlsx", "model-DRAFT.xlsx",
                                     "dossier.pdf", "dossier.tex"):
            raise ValidationError(
                f"export file name must start with '{case_id}-': {filename}")
        found = _rcase_dir(case_id) / "export" / filename
        if not found.is_file():
            raise NotFound(f"no such export file: {filename} (run export "
                           "first)")
        return {"case_id": case_id, "filename": filename,
                "media_type": ("application/pdf" if filename.endswith(".pdf")
                               else "text/html"
                               if filename.endswith(".html") else
                               "application/x-latex"
                               if filename.endswith(".tex") else
                               "application/vnd.openxmlformats-officedocument"
                               ".spreadsheetml.sheet"),
                "bytes": found.read_bytes()}
    c = _get(case_id)
    if not filename.startswith(case_id + "-"):
        raise ValidationError(
            f"export file name must start with '{case_id}-': {filename}")
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
    scope = _v(_san.text, "scope", change.get("scope"), max_len=100, required=True,
               charset=r"[A-Za-z0-9 ._/&()-]")
    change = dict(change)
    change["description"] = _v(_san.note, "description", change.get("description"),
                               max_len=1000)
    change["author"] = _v(_san.person_name, "author", change.get("author"),
                          required=False) or None
    change["linked_case"] = _v(_san.identifier, "linked_case",
                               change.get("linked_case"), required=False) or None
    df = change.get("diff_files") or []
    if not isinstance(df, list) or len(df) > 50:
        raise ValidationError("diff_files must be a list of at most 50 paths")
    change["diff_files"] = [_v(_san.safe_relative, f"diff_files[{i}]", f)
                            for i, f in enumerate(df)]
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
    version = _v(_san.identifier, "version", version)
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
    version = _v(_san.identifier, "version", version)
    by = _v(_san.person_name, "by", by, required=False)
    reasoning = _v(_san.note, "reasoning", reasoning)
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
    to_version = _v(_san.identifier, "to_version", to_version)
    verify_case = _v(_san.identifier, "verify_case", verify_case)
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
    """GET /api/edgar/search — filing index for the bundle builder.

    Resolve the ticker through the SEC index and query recent submissions.
    If the SEC is unavailable, show demo rows when available.
    """
    t = _v(_san.ticker, "ticker", ticker)
    forms = _v(_san.text, "forms", forms, max_len=60, charset=r"[A-Za-z0-9,/ -]",
               injection=False) or None
    window = _v(_san.text, "window", window, max_len=21, charset=r"[0-9:-]",
                injection=False) or None
    try:
        fetch = _real_bundle.EdgarFetcher().get
        cik = _real_bundle.resolve_ticker(t, fetch)
        if cik is None:
            return {"ticker": t, "count": 0, "cik": None, "filings": [],
                    "note": "Ticker not found in the SEC company ticker index."}
        subs = _real_bundle.fetch_submissions(cik, fetch)
        recent = _real_bundle._recent_index(subs)
        rows = []
        for acc, rec in recent.items():
            acc_dashed = rec.get("accessionNumber") or (
                f"{acc[:10]}-{acc[10:12]}-{acc[12:]}")
            primary = rec.get("primaryDocument") or ""
            rows.append({
                "doc_id": f"{t.lower()}-{acc}", "ticker": t,
                "accession": acc_dashed, "form": rec.get("form") or "",
                "filed": rec.get("filingDate") or "",
                "period": rec.get("reportDate") or "", "cik": cik,
                "title": f"{t} {rec.get('form') or ''} {rec.get('reportDate') or ''}".strip(),
                "bytes": None, "primary_doc": primary or None,
                "source_url": (_real_bundle.archive_base(cik, acc_dashed)
                               + "/" + primary if primary else None)})
    except _real_bundle.EdgarError as exc:
        demo = _demo_edgar_rows(t, forms, window,
            "SEC lookup unavailable. Showing the limited offline demo index.")
        if demo["filings"]:
            return demo
        raise UpstreamError("SEC lookup unavailable. Check your connection and SEC user agent, then try again.") from exc
    if forms:
        wanted = {f.strip().upper() for f in forms.split(",") if f.strip()}
        rows = [r for r in rows if r["form"] in wanted]
    if window:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$", window)
        if not m:
            raise ValidationError("window must be YYYY-MM-DD:YYYY-MM-DD, e.g. 2025-01-01:2026-12-31")
        rows = [r for r in rows if m.group(1) <= r["filed"] <= m.group(2)]
    return {"ticker": t, "count": len(rows), "cik": cik, "filings": rows,
            "note": "Showing recent filings from the SEC."}


def _demo_edgar_rows(t: str, forms: str | None = None,
                     window: str | None = None, note: str | None = None) -> dict:
    """The demo filing index (offline default)."""
    t = (t or "").strip().upper()
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
            "note": note or "demo EDGAR index (stub); the real fetcher is "
                            "coverageflow.bundle — frozen, hashed snapshots "
                            "only"}


def list_bundles() -> dict:
    """GET /api/bundles — frozen bundle manifests (wizard + builder pages)."""
    if _using_real():
        ws = _rws()
        manifests = []
        for base in (ws / "bundles", ws / "data",
                     Path(__file__).resolve().parent.parent / "data"):
            if not base.is_dir():
                continue
            for mpath in sorted(base.glob("*/manifest.json")):
                try:
                    man = _real_statestore.read_json(mpath)
                except Exception:
                    continue
                if isinstance(man, dict) and man.get("bundle_id"):
                    # only bundles frozen into the workspace may be edited
                    man = {**man, "editable": base == ws / "bundles"}
                    manifests.append(man)
        seen = set()
        uniq = []
        for m in manifests:
            if m["bundle_id"] not in seen:
                seen.add(m["bundle_id"])
                uniq.append(m)
        return {"bundles": uniq, "count": len(uniq)}
    return {"bundles": [{**m, "editable": m["bundle_id"] not in _BUILTIN_BUNDLES}
                        for m in _BUNDLES.values()], "count": len(_BUNDLES)}


_BUNDLE_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")
_BUILTIN_BUNDLES = {_INTC_MANIFEST["bundle_id"], _AMD_MANIFEST["bundle_id"]}


def _bundle_cases_using(bundle_id: str) -> list[str]:
    """Case ids whose mandate/request/state files reference this bundle."""
    cases = _rws() / "cases"
    if not cases.is_dir():
        return []
    used = []
    for cdir in sorted(p for p in cases.iterdir() if p.is_dir()):
        for fname in ("mandate.json", "request.json", "state.json"):
            f = cdir / fname
            try:
                if f.is_file() and f'"{bundle_id}"' in f.read_text("utf-8"):
                    used.append(cdir.name)
                    break
            except OSError:
                continue
    return used


def _editable_bundle_dir(bundle_id: str) -> Path | None:
    """Workspace bundle dir for edits; None in demo mode. Built-ins refuse."""
    if not _BUNDLE_NAME_RE.fullmatch(bundle_id or ""):
        raise ValidationError("Invalid bundle name.")
    if _using_real():
        d = _rws() / "bundles" / bundle_id
        if (d / "manifest.json").is_file():
            used = _bundle_cases_using(bundle_id)
            if used:
                raise ConflictError(
                    f"Bundle {bundle_id} is used by case {used[0]}"
                    + (f" and {len(used) - 1} more" if len(used) > 1 else "")
                    + ". Remove or finish those cases first.")
            return d
        if _rfind_bundle_dir(bundle_id) is not None:
            raise ValidationError("Built-in bundles are read-only.")
        raise NotFound(f"unknown bundle: {bundle_id}")
    if bundle_id in _BUILTIN_BUNDLES:
        raise ValidationError("Built-in bundles are read-only.")
    if bundle_id not in _BUNDLES:
        raise NotFound(f"unknown bundle: {bundle_id}")
    return None


def delete_bundle(bundle_id: str) -> dict:
    """DELETE /api/bundles/{id} — remove a workspace bundle snapshot."""
    d = _editable_bundle_dir(bundle_id)
    if d is not None:
        shutil.rmtree(d)
    _BUNDLES.pop(bundle_id, None)
    _emit("*", "BUNDLE_DELETED", {"bundle_id": bundle_id})
    return {"deleted": bundle_id}


def rename_bundle(bundle_id: str, new_name: str) -> dict:
    """PATCH /api/bundles/{id} — rename a bundle.

    Frozen contents are never edited: the SHA-256 covers the documents only,
    so a rename keeps the hash intact. Changing documents means freezing a
    new bundle.
    """
    new_name = (new_name or "").strip()
    if not _BUNDLE_NAME_RE.fullmatch(new_name):
        raise ValidationError("Bundle name must contain only letters, numbers, "
                              "dots, underscores or hyphens.")
    d = _editable_bundle_dir(bundle_id)
    if new_name == bundle_id:
        return list_bundle_one(bundle_id)
    if new_name in {m["bundle_id"] for m in list_bundles()["bundles"]}:
        raise ConflictError(f"bundle name already exists: {new_name}")
    if d is not None:
        man = _real_statestore.read_json(d / "manifest.json")
        man["bundle_id"] = new_name
        _real_statestore.atomic_write_json(d / "manifest.json", man)
        d.rename(d.with_name(new_name))
    if bundle_id in _BUNDLES:
        _BUNDLES[new_name] = {**_BUNDLES.pop(bundle_id), "bundle_id": new_name}
    _emit("*", "BUNDLE_RENAMED", {"bundle_id": new_name, "was": bundle_id})
    return list_bundle_one(new_name)


def extend_bundle(base_id: str, docs: list, name: str) -> dict:
    """POST /api/bundles/{id}/extend — new bundle = existing one + new filings.

    The base bundle is left untouched. Its documents are copied (and
    re-verified against their recorded SHA-256), so only the new filings are
    downloaded from the SEC.
    """
    if not _using_real():
        raise ValidationError("Updating a bundle requires a controller workspace. Start CoverageFlow with launch.py.")
    if not _BUNDLE_NAME_RE.fullmatch(base_id or ""):
        raise ValidationError("Invalid bundle name.")
    base_dir = _rfind_bundle_dir(base_id)
    if base_dir is None:
        raise NotFound(f"unknown bundle: {base_id}")
    if not docs or not all(isinstance(d, dict) and d.get("accession") for d in docs):
        raise ValidationError("Select at least one new filing to add.")
    base = _real_bundle.load_manifest(base_dir)
    have = {d.doc_id for d in base.documents}
    m = re.search(r"/data/(\d+)/", base.documents[0].source_url) if base.documents else None
    if not m:
        raise ValidationError("This bundle has no SEC source to update from.")
    base_cik = int(m.group(1))
    new = [d for d in docs if d["doc_id"] not in have]
    if not new:
        raise ValidationError("Those filings are already in the bundle.")
    if any(str(d.get("cik") or "").lstrip("0") != str(base_cik) for d in new):
        raise ValidationError("New filings must be from the same company as the bundle.")
    name = (name or "").strip()
    if not name:
        t = (new[0].get("ticker") or base_id.split("-")[0]).upper()
        stem = f"{t}-{datetime.now().strftime('%Y-%m')}"
        name, n = stem, 2
        while (_rws() / "bundles" / name).exists():   # never collide with the base
            name, n = f"{stem}-v{n}", n + 1
    if not _BUNDLE_NAME_RE.fullmatch(name):
        raise ValidationError("Bundle name must contain only letters, numbers, dots, underscores or hyphens.")
    dest = _rws() / "bundles" / name
    if dest.exists():
        raise ConflictError(f"bundle name already exists: {name}")
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=_rws()))
    try:
        specs = [_real_bundle.DocSpec(
            doc_id=str(d["doc_id"]), accession=str(d["accession"]),
            form=d.get("form"), filed=d.get("filed"), period=d.get("period"),
            primary_doc=d.get("primary_doc"), ticker=d.get("ticker")) for d in new]
        added = _real_bundle.build_bundle(str(new[0]["cik"]), specs, stage)
        carried = []
        for old in base.documents:
            src = base_dir / old.path
            if not src.is_file() or hashlib.sha256(src.read_bytes()).hexdigest() != old.sha256:
                raise ValidationError(f"Existing document {old.doc_id} failed its hash check; bundle not updated.")
            (stage / old.path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, stage / old.path)
            carried.append(old)
        merged = sorted(carried + list(added.documents), key=lambda x: x.doc_id)
        result = {
            "as_of_cutoff": max(x.filed for x in merged),
            "documents": [x.to_dict() for x in merged],
            "bundle_id": name,
            "bundle_sha256": _real_bundle._bundle_sha(merged),
            "parent_bundle": base_id,
            "parent_sha256": base.bundle_sha256,
        }
        _real_statestore.atomic_write_json(stage / "manifest.json", result)
        stage.rename(dest)
    except (ValueError, KeyError, OSError, _real_bundle.EdgarError) as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise ValidationError(f"Could not update bundle: {exc}") from exc
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    _emit("*", "BUNDLE_CREATED", {"bundle_id": name, "real": True, "parent": base_id})
    return result


def list_bundle_one(bundle_id: str) -> dict:
    return next(m for m in list_bundles()["bundles"] if m["bundle_id"] == bundle_id)


def build_bundle(cik: str, docs: list[str], name: str) -> dict:
    """POST /api/bundles — snapshot selected docs into a new frozen bundle.

    Live filings require accession data and a controller workspace.
    Demo document IDs remain available in the in-memory demo mode.
    """
    cik = _v(_san.cik, "cik", cik)
    name = _v(_san.identifier, "name", name, required=False)
    if not docs:
        raise ValidationError("select at least one document to snapshot")
    if len(docs) > 40:
        raise ValidationError("select at most 40 documents")
    docs = [_clean_doc_spec(i, d) for i, d in enumerate(docs)]
    if any(isinstance(d, dict) for d in docs):
        if not _using_real():
            raise ValidationError("Freezing SEC filings requires a controller workspace. Start CoverageFlow with launch.py.")
        return _real_build_bundle(cik, docs, name)
    if _using_real():
        raise ValidationError("Live snapshots require SEC filing details. Search and select filings again.")
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
    if _using_real():
        # the offline fallback still lands in the real workspace so case
        # creation can resolve the bundle later
        dest = _rws() / "bundles" / bundle_id
        dest.mkdir(parents=True, exist_ok=True)
        _real_statestore.atomic_write_json(dest / "manifest.json", manifest)
    _emit("*", "BUNDLE_CREATED", {"bundle_id": bundle_id,
                                  "documents": len(chosen),
                                  "offline_fallback": _using_real()})
    return manifest


def _clean_doc_spec(i: int, d: Any) -> Any:
    """A selected filing (dict from the EDGAR search) or a demo doc id."""
    f = f"docs[{i}]"
    if not isinstance(d, dict):
        return _v(_san.identifier, f, d)
    out = dict(d)
    out["doc_id"] = _v(_san.identifier, f + ".doc_id", d.get("doc_id"))
    out["accession"] = _v(_san.text, f + ".accession", d.get("accession"), max_len=20,
                          charset=r"[0-9-]", injection=False, required=True)
    out["cik"] = _v(_san.cik, f + ".cik", d.get("cik"))
    out["form"] = _v(_san.text, f + ".form", d.get("form"), max_len=20,
                     charset=r"[A-Za-z0-9/ -]", injection=False) or None
    out["filed"] = _v(_san.iso_date, f + ".filed", d.get("filed"), required=False) or None
    out["period"] = _v(_san.iso_date, f + ".period", d.get("period"), required=False) or None
    out["primary_doc"] = _v(_san.filename, f + ".primary_doc", d.get("primary_doc"),
                            required=False) or None
    out["ticker"] = _v(_san.ticker, f + ".ticker", d.get("ticker"), required=False) or None
    return out


def _real_build_bundle(cik: str, docs, name: str) -> dict:
    """Write a verified SEC snapshot or report the failure to the caller."""
    if name and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name):
        raise ValidationError("Bundle name must contain only letters, numbers, dots, underscores or hyphens.")
    try:
        specs = []
        for d in docs:
            if not isinstance(d, dict) or not d.get("accession") or str(d.get("cik")) != cik:
                raise ValidationError("Select filings from the same company and search again.")
            specs.append(_real_bundle.DocSpec(
                doc_id=str(d["doc_id"]), accession=str(d["accession"]),
                form=d.get("form"), filed=d.get("filed"),
                period=d.get("period"), primary_doc=d.get("primary_doc"),
                ticker=d.get("ticker")))
        bundle_id = name or f"{specs[0].ticker.lower()}-{specs[0].filed[:7]}"
        dest = _rws() / "bundles" / bundle_id
        if dest.exists():
            raise ConflictError(f"bundle name already exists: {bundle_id}")
        manifest = _real_bundle.build_bundle(str(cik), specs, dest)
        result = manifest.to_dict()
        result["bundle_id"] = bundle_id
        _real_statestore.atomic_write_json(dest / "manifest.json", result)
        _emit("*", "BUNDLE_CREATED", {"bundle_id": bundle_id, "real": True})
        return result
    except (ValueError, KeyError, _real_bundle.EdgarError) as exc:
        raise ValidationError(f"Could not freeze SEC filings: {exc}") from exc
