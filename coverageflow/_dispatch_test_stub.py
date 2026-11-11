"""Scripted offline test stub for the dispatch seam (owner: stream A2, tests).

The real dispatch layer (coverageflow/dispatch.py, stream A3) assembles
prompts, picks a backend and returns AgentResults per contracts
interfaces.md sections 1-2. This module provides a pre-baked, scripted
substitute so stream A2's orchestrator can be tested fully offline:

  - RoleCall / AgentResult mirror the contract dataclasses exactly.
  - StubDispatch is callable: ``dispatch(role_call) -> AgentResult`` —
    the same seam the real dispatch will implement.
  - Each role returns gate-satisfying default outputs; per-role scripts can
    override any invocation (invalid submissions, stale request echoes,
    critic findings, changed writer output, ...).

At integration the coordinator swaps StubDispatch for the real dispatch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# contract envelope dataclasses (interfaces.md sections 1-2, mirrored)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoleCall:
    role: str
    request_id: str
    candidate_id: str | None
    role_card: dict
    payload: dict
    output_schema: str
    budget: dict
    write_scope: list[str]
    workdir: str | None = None


@dataclass
class AgentResult:
    ok: bool
    output_text: str | None = None
    parsed: dict | None = None
    error: str | None = None
    backend: str = "stub"
    elapsed_seconds: float = 0.5
    tokens_in: int | None = None
    tokens_out: int | None = None
    raw_response_ref: str | None = None
    cost_usd: float | None = None


class ReplayScriptError(RuntimeError):
    """Raised when a script references an impossible state (guards typos)."""


# ---------------------------------------------------------------------------
# default per-role outputs (deterministic, gate-satisfying)
# ---------------------------------------------------------------------------

def _slice_text(doc_id: str) -> str:
    """Deterministic stand-in for a statement slice's plain text."""
    return (
        f"Consolidated Statements of Operations | {doc_id}\n"
        "Revenue | 13,500\n"
        "Cost of sales | 7,400\n"
    )


def _default_ingest(payload: dict) -> dict:
    manifest = payload.get("input_manifest") or {}
    docs = manifest.get("documents") or [{}]
    doc = docs[0]
    doc_id = doc.get("doc_id", "intc-10q-2026q3")
    text = _slice_text(doc_id)
    slices = [
        {
            "slice_id": f"{doc_id}/IS",
            "kind": "income_statement",
            "path": f"inputs/slices/{doc_id}/IS.txt",
            # hash of the bytes the stub actually writes, so the extractor's
            # payload verification can check them like a real slice
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "anchors": {"first_line": "Consolidated Statements of Operations"},
        }
    ]
    return {
        "role": "ingest",
        # contract-required identity fields: the real ingest stage stamps the
        # document it classified and the candidate it belongs to (null before
        # a candidate exists), and dispatch enforces their presence
        "doc_id": doc_id,
        "candidate_id": None,
        "classification": {
            "form": doc.get("form", "10-Q"),
            "affects": ["extraction", "model", "valuation", "report"],
            "reason": "new quarterly statements supersede prior-period estimates",
        },
        "slices": slices,
        "superseded": [],
        "conformance": "schema 1.0",
        "prompt_version": "1.0",
    }


def _default_facts(payload: dict) -> dict:
    """Scripted extractor payload: mini quarterly set + FY annuals.

    Values mirror the hand-computed mini-10q fixture in tests/ so the REAL
    model and valuation engines can run end-to-end in offline stub mode.
    """
    doc_id = _doc_id_from_payload(payload)

    def fact(field, period, value, idx):
        return {"field": field, "period": period, "value": value,
                "unit": "USDmm",
                "confidence": 0.98,
                "citation": {"doc_id": doc_id,
                             "slice_id": f"{doc_id}/IS",
                             "anchor": "Consolidated Statements of Operations",
                             "locator": f"line {idx}"}}

    IS_Q = {"revenue": (13500.0, 12500.0), "cost_of_revenue": (8325.0, 8125.0),
            "gross_profit": (5175.0, 4375.0),
            "research_and_development": (3150.0, 3050.0),
            "selling_general_and_administrative": (1200.0, 1150.0),
            "operating_expenses": (4350.0, 4200.0),
            "operating_income": (825.0, 175.0),
            "depreciation_and_amortization": (500.0, 450.0),
            "net_income": (700.0, 150.0), "diluted_eps": (0.16, 0.03),
            "diluted_shares_outstanding": (4375.0, 5000.0)}
    BS_Q = {"cash_and_equivalents": (25000.0, 21000.0),
            "short_term_debt": (2000.0, 2000.0),
            "long_term_debt": (50000.0, 48000.0),
            "total_assets": (145000.0, 140000.0),
            "total_stockholders_equity": (66000.0, 64000.0)}
    CF_Q = {"operating_cash_flow": (3200.0, 1800.0),
            "capital_expenditure": (2500.0, 1700.0)}
    FY = {"income_statement": {
              "revenue": 52000.0, "cost_of_revenue": 33800.0,
              "gross_profit": 18200.0, "operating_income": 2080.0,
              "depreciation_and_amortization": 1200.0, "net_income": 1040.0,
              "diluted_eps": 0.20, "diluted_shares_outstanding": 5200.0},
          "cash_flow": {"operating_cash_flow": 4160.0,
                        "capital_expenditure": 3120.0},
          "balance_sheet": {"total_assets": 140000.0,
                            "total_stockholders_equity": 64000.0}}
    SEGS = {"ccg": (24000.0, 1500.0), "dcai": (12000.0, 600.0),
            "nex": (6000.0, 150.0), "foundry": (6000.0, -200.0),
            "altera": (6000.0, 150.0), "mobileye": (4000.0, 100.0),
            "all_other": (-6000.0, -220.0)}

    facts, i = [], 0
    for group, table in (("income_statement", IS_Q), ("balance_sheet", BS_Q),
                         ("cash_flow", CF_Q)):
        for fld, vals in table.items():
            for p, v in zip(("2026-Q3", "2025-Q3"), vals):
                facts.append(fact(f"{group}.{fld}", p, v, i)); i += 1
    SEGS_Q = {"Client Computing Group": ((6000.0, 700.0), (5800.0, 500.0)),
              "Data Center and AI": ((4500.0, 300.0), (4000.0, 150.0)),
              "Network and Edge": ((2000.0, 150.0), (1700.0, 100.0)),
              "Intel Foundry": ((1000.0, -650.0), (1000.0, -700.0)),
              "Altera": ((800.0, 250.0), (750.0, 200.0)),
              "Mobileye": ((500.0, 100.0), (450.0, 50.0)),
              "All Other": ((-1300.0, -25.0), (-1200.0, -25.0))}
    for name, ((r26, o26), (r25, o25)) in SEGS_Q.items():
        facts.append(fact(f"segments.{name}.revenue", "2026-Q3", r26, i)); i += 1
        facts.append(fact(f"segments.{name}.revenue", "2025-Q3", r25, i)); i += 1
        facts.append(fact(f"segments.{name}.operating_income", "2026-Q3", o26, i)); i += 1
        facts.append(fact(f"segments.{name}.operating_income", "2025-Q3", o25, i)); i += 1
    for group, table in FY.items():
        for fld, v in table.items():
            facts.append(fact(f"{group}.{fld}", "FY2025", v, i)); i += 1
    for seg, (rev, oi) in SEGS.items():
        if seg == "all_other":
            facts.append(fact("segments.all_other_revenue", "FY2025", rev, i)); i += 1
            facts.append(fact("segments.all_other_operating_income", "FY2025", oi, i)); i += 1
        else:
            facts.append(fact(f"segments.{seg}.revenue", "FY2025", rev, i)); i += 1
            facts.append(fact(f"segments.{seg}.operating_income", "FY2025", oi, i)); i += 1

    return {"role": "extractor", "facts": facts, "missing": [], "notes": [],
            "conformance": "schema 1.0", "prompt_version": "1.0"}


def _default_forecast(payload: dict) -> dict:
    return {
        "role": "model",
        "horizon_years": 5,
        "segments": [
            {"segment": "CCG", "growth": [0.08, 0.08, 0.08, 0.08, 0.08],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
            {"segment": "DCAI", "growth": [0.16, 0.15, 0.14, 0.13, 0.12],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
            {"segment": "NEX", "growth": [0.05, 0.05, 0.05, 0.05, 0.05],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
            {"segment": "Foundry", "growth": [0.05, 0.05, 0.05, 0.05, 0.05],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
            {"segment": "Altera", "growth": [0.05, 0.05, 0.05, 0.05, 0.05],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
            {"segment": "Mobileye", "growth": [0.12, 0.11, 0.10, 0.09, 0.08],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
        ],
        "margin_glide": {"gross": [0.36, 0.37, 0.38, 0.39, 0.40]},
        "terminal_basis": "mandate",
        "conformance": "schema 1.0",
        "prompt_version": "1.0",
    }
    return {
        "role": "model",
        "horizon_years": 5,
        "segments": [
            {"segment": "CCG", "growth": [0.02, 0.02, 0.02, 0.02, 0.02],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
            {"segment": "DCAI", "growth": [0.18, 0.16, 0.14, 0.12, 0.10],
             "drivers": [{"text": "Q3 segment table", "citation": {}}]},
        ],
        "margin_glide": {"gross": [0.40, 0.41, 0.42, 0.42, 0.43]},
        "terminal_basis": "mandate",
        "conformance": "schema 1.0",
        "prompt_version": "1.0",
    }


DEFAULT_REPORT_MD = (
    "## Summary\n"
    "Revenue rose to 13,500 [EV:income_statement.revenue] with gross profit "
    "of 5,175 [EV:income_statement.gross_profit].\n"
    "The DCF yields 28.0 [EV:dcf.per_diluted_share] per share; comps imply 26.0 "
    "[EV:comps.comps_price].\n"
)

ALT_REPORT_MD = (
    "## Summary\n"
    "Revenue improved to 13,500 [EV:income_statement.revenue] with gross profit "
    "of 5,175 [EV:income_statement.gross_profit] and operating discipline.\n"
    "The DCF yields 28.0 [EV:dcf.per_diluted_share] per share; comps imply 26.0 "
    "[EV:comps.comps_price].\n"
    "Operating expense discipline supports the margin glide in the out years.\n"
)


def _default_writer(payload: dict) -> dict:
    return {
        "role": "writer",
        "report_md": DEFAULT_REPORT_MD,
        # every claim in the report must carry a citation; the stub cites the
        # facts it was handed so the envelope matches the contract
        "citations": [
            {"claim": "revenue growth", "field": "income_statement.revenue",
             "doc_id": _doc_id_from_payload(payload), "slice_id": "IS",
             "anchor": "Consolidated Statements of Operations"},
        ],
        "conformance": "schema 1.0",
        "prompt_version": "1.0",
    }


def _default_generator_lead(payload: dict) -> dict:
    return {
        "role": "generator_lead",
        "candidate_id": payload["candidate_id"],
        "round": payload.get("round"),
        "reports": list(payload.get("artifacts", [])),
        "evidence_index": dict(payload.get("evidence_index", {})),
        "scope_statement": payload.get(
            "scope_statement",
            "full rebuild; carried findings addressed; no stages hash-identical"),
    }


def _default_critic(critic_role: str) -> Callable[[dict], dict]:
    def build(payload: dict) -> dict:
        # finding_id here is the LEDGER id (fnd-NNNN) the carried row exposes
        carried = payload.get("carried_findings", [])
        artifacts = payload.get("candidate_artifacts", {}) or {}
        resolutions = []
        for f in carried:
            resolutions.append({
                "finding_id": f["finding_id"],
                "resolved": True,
                "verified_evidence": _resolve_evidence(f, artifacts),
                "check_performed": "stub: re-inspected the repaired artifact",
            })
        return {
            "role": critic_role,
            "round": payload.get("round"),
            "verdict": "clean",
            "findings": [],
            "verified_resolutions": resolutions,
            "coverage": {
                "rubric_version": "1.0",
                "areas_checked": ["citation_integrity", "recomputation",
                                  "cutoff_discipline", "risk_controls"],
            },
            "conformance": "schema 1.0",
            "prompt_version": "1.0",
        }
    return build


def _resolve_evidence(finding: dict, artifacts: dict) -> dict:
    """Pick the current candidate artifact backing a finding's repair."""
    affected = (finding.get("affected_artifacts") or ["facts.json"])[0]
    base = affected.split("/")[-1]
    for rel, sha in artifacts.items():
        if rel == affected or rel.endswith("/" + base):
            return {"artifact": rel, "sha256": sha}
    return {"artifact": affected, "sha256": "0" * 64}


def _default_critic_lead(payload: dict) -> dict:
    reports = payload.get("critic_reports", [])
    all_findings = []
    seen = set()
    conflict_free = True
    for rep in reports:
        for f in rep.get("findings", []):
            key = f.get("summary", "")
            item = dict(f)
            item["source_critic"] = rep.get("role")
            item["dedup_key"] = f"critic:{key}"
            if key in seen:
                item["conflict_group"] = key
                conflict_free = False
            seen.add(key)
            all_findings.append(item)
    return {
        "role": "critic_lead",
        "round": payload.get("round"),
        "candidate_id": payload.get("candidate_id"),
        "findings": all_findings,
        "clean": all(not rep.get("findings") for rep in reports),
        "coverage_ok": len(reports) == 4,
        "reviewer_count": len(reports),
        "conflicts_kept": not conflict_free,
        "fresh_context_proofs": [rep.get("request_id") for rep in reports],
    }


def _stable_sha(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _doc_id_from_payload(payload: dict) -> str:
    manifest = payload.get("input_manifest") or {}
    docs = manifest.get("documents") or [{}]
    return docs[0].get("doc_id", "intc-10q-2026q3")


DEFAULT_BUILDERS: dict[str, Callable[[dict], dict]] = {
    "ingest": _default_ingest,
    "extractor": _default_facts,
    "model": _default_forecast,
    "writer": _default_writer,
    "generator_lead": _default_generator_lead,
}


# ---------------------------------------------------------------------------
# the scripted stub dispatch
# ---------------------------------------------------------------------------

class StubDispatch:
    """Callable stub implementing the dispatch seam with scriptable outputs.

    scripts[role] may be:
      - None                         → role default output
      - dict                          → that dict is the parsed output
      - callable(payload) -> dict     → computed output
      - list of the above            → consumed per invocation, in order;
                                        after the list is exhausted the role
                                        falls back to the default output.

    Magic keys inside a dict script entry:
      "__invalid": true   → ok=False result (unparseable output)
      "__error": "msg"    → ok=False with that error
      "__stale": true     → output echoes a wrong (stale) request_id
    """

    name = "stub"

    def __init__(self, scripts: dict[str, Any] | None = None,
                 tokens_in: int = 140, tokens_out: int = 260,
                 cost_usd: float | None = None):
        self.cost_usd = cost_usd
        self.scripts = dict(scripts or {})
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.calls: list[RoleCall] = []
        self.results: list[AgentResult] = []
        self._counts: dict[str, int] = {}

    # ------------------------------------------------------------- protocol
    def healthcheck(self) -> dict:
        return {"backend": self.name, "ok": True,
                "dispatches": len(self.calls)}

    def estimate_cost(self, text: str):
        return None

    def __call__(self, call: RoleCall) -> AgentResult:
        self.calls.append(call)
        n = self._counts.get(call.role, 0) + 1
        self._counts[call.role] = n
        entry = self._script_entry(call.role, n)
        result = self._materialize(call, entry)
        self.results.append(result)
        return result

    # dispatch.py naming parity (A3's real dispatch will expose .invoke)
    invoke = __call__

    # ---------------------------------------------------------------- internals
    def _script_entry(self, role: str, n: int) -> Any:
        entry = self.scripts.get(role)
        if isinstance(entry, list):
            idx = n - 1
            if idx < len(entry):
                return entry[idx]
            return None  # exhausted → default
        return entry

    def _materialize(self, call: RoleCall, entry: Any) -> AgentResult:
        if entry is None:
            builder = DEFAULT_BUILDERS.get(call.role)
            if builder is None:
                if call.role.startswith("critic_c"):
                    builder = _default_critic(call.role)
                elif call.role == "critic_lead":
                    builder = _default_critic_lead
            if builder is None:
                return AgentResult(ok=False, error=f"stub has no output for role {call.role!r}")
            parsed = builder(call.payload)
            parsed = self._echo_identity(parsed, call)
            return self._ok(call, parsed)
        if callable(entry):
            parsed = entry(call.payload)
            parsed = self._echo_identity(parsed, call)
            return self._ok(call, parsed)
        # dict entry with magic keys
        if entry.get("__invalid"):
            return AgentResult(ok=False, error="stub: malformed output (not parseable JSON)",
                               tokens_in=self.tokens_in, tokens_out=0)
        if "__error" in entry:
            return AgentResult(ok=False, error=str(entry["__error"]),
                               tokens_in=self.tokens_in, tokens_out=0)
        parsed = {k: v for k, v in entry.items() if not k.startswith("__")}
        if entry.get("__stale"):
            parsed["request_id"] = "req-stale-9999"  # wrong id on purpose
        parsed = self._echo_identity(parsed, call)
        return self._ok(call, parsed)

    def _ok(self, call: RoleCall, parsed: dict) -> AgentResult:
        """Successful result, after materializing any files the role declares."""
        self._write_slices(call, parsed)
        return AgentResult(ok=True, parsed=parsed, output_text=str(parsed),
                           tokens_in=self.tokens_in, tokens_out=self.tokens_out,
                           cost_usd=self.cost_usd)

    @staticmethod
    def _write_slices(call: RoleCall, parsed: dict) -> None:
        """Write the slice files an ingest report declares.

        The real ingest stage produces these through the slicer; the stub
        stands in for the whole stage, so it must leave the same files on
        disk or the extractor's payload references would not resolve.
        """
        if call.role != "ingest":
            return
        workdir = getattr(call, "workdir", None)
        if not workdir:
            return
        for entry in parsed.get("slices") or []:
            rel = (entry or {}).get("path")
            if not rel:
                continue
            target = Path(workdir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            doc_id = str(entry.get("slice_id", "")).split("/")[0] or "doc"
            target.write_text(_slice_text(doc_id), encoding="utf-8")

    @staticmethod
    def _echo_identity(parsed: dict, call: RoleCall) -> dict:
        """Stamp the identity fields every artifact envelope must carry.

        ``candidate_id`` is always present, null before a candidate exists:
        the contract requires the key, and dispatch enforces it.
        """
        if not isinstance(parsed, dict):
            return parsed
        out = dict(parsed)
        if out.get("request_id") is None:
            out["request_id"] = call.request_id
        if out.get("candidate_id") is None:
            out["candidate_id"] = call.candidate_id
        return out

    # -------------------------------------------------------------- helpers
    def calls_of(self, role: str) -> list[RoleCall]:
        return [c for c in self.calls if c.role == role]

    def request_ids_of(self, role: str) -> list[str]:
        return [c.request_id for c in self.calls_of(role)]


# ---------------------------------------------------------------------------
# offline engine doubles (for CLI runs while stream A1's engines are not
# importable; same contract signatures as interfaces.md section 4)
# ---------------------------------------------------------------------------

def stub_build_model(facts, mapping_version: str):
    """Offline engine double. Post-integration: delegates to the real, also
    offline model engine so builds and recomputes agree exactly.

    Falls back to the legacy naive stub only if the engine package is
    somehow unimportable (kept for forensics; not used in-repo)."""
    try:
        from coverageflow.model_engine import build_model
        return build_model(facts, mapping_version).to_dict()
    except ImportError:
        series = {}
        for f in facts.get("facts", []):
            series.setdefault(f["field"], {})[f["period"]] = f["value"]
        return {"artifact": "model", "engine": "stub-model@1.0",
                "mapping_tables": mapping_version, "series": series}


def stub_value_company(model, forecast, assumptions):
    """Offline engine double. Post-integration: delegates to the real,
    deterministic valuation engine (same inputs -> same payload)."""
    try:
        from coverageflow.valuation_engine import value_company
        return value_company(model, forecast, assumptions).payload
    except ImportError:
        return {"artifact": "valuation", "engine": "stub-val@1.0",
                "dcf": {"equity_value": 120000.0, "per_diluted_share": 28.0},
                "comps": {"comps_price": 26.0}, "sensitivity": {},
                "cross_check_delta": 0.08}
