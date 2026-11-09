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

from dataclasses import dataclass, field
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


class ReplayScriptError(RuntimeError):
    """Raised when a script references an impossible state (guards typos)."""


# ---------------------------------------------------------------------------
# default per-role outputs (deterministic, gate-satisfying)
# ---------------------------------------------------------------------------

def _default_ingest(payload: dict) -> dict:
    manifest = payload.get("input_manifest") or {}
    docs = manifest.get("documents") or [{}]
    doc = docs[0]
    doc_id = doc.get("doc_id", "intc-10q-2026q3")
    slices = [
        {
            "slice_id": f"{doc_id}/IS",
            "kind": "income_statement",
            "path": f"inputs/slices/{doc_id}/IS.txt",
            "sha256": _stable_sha(doc_id + "/IS"),
            "anchors": {"first_line": "Consolidated Statements of Operations"},
        }
    ]
    return {
        "role": "ingest",
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
    doc_id = _doc_id_from_payload(payload)
    return {
        "role": "extractor",
        "facts": [
            {"field": "income_statement.revenue", "period": "2026-Q3", "value": 13500.0,
             "unit": "USDmm", "confidence": 0.98,
             "citation": {"doc_id": doc_id, "slice_id": f"{doc_id}/IS",
                          "anchor": "Consolidated Statements of Operations",
                          "locator": "line 14"}},
            {"field": "income_statement.gross_margin", "period": "2026-Q3",
             "value": 0.42, "unit": "ratio", "confidence": 0.95,
             "citation": {"doc_id": doc_id, "slice_id": f"{doc_id}/IS",
                          "anchor": "Consolidated Statements of Operations",
                          "locator": "line 19"}},
        ],
        "missing": [],
        "notes": [],
        "conformance": "schema 1.0",
        "prompt_version": "1.0",
    }


def _default_forecast(payload: dict) -> dict:
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
    "Revenue rose to 13,500 [EV:income_statement.revenue] with gross margin "
    "of 0.42 [EV:income_statement.gross_margin].\n"
    "The DCF yields 28.0 [EV:dcf.per_share] per share; comps imply 26.0 "
    "[EV:comps.per_share].\n"
)

ALT_REPORT_MD = (
    "## Summary\n"
    "Revenue improved to 13,500 [EV:income_statement.revenue] with gross margin "
    "of 0.42 [EV:income_statement.gross_margin] and operating discipline.\n"
    "The DCF yields 28.0 [EV:dcf.per_share] per share; comps imply 26.0 "
    "[EV:comps.per_share].\n"
    "Operating expense discipline supports the margin glide in the out years.\n"
)


def _default_writer(payload: dict) -> dict:
    return {
        "role": "writer",
        "report_md": DEFAULT_REPORT_MD,
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
        carried = payload.get("carried_findings", [])
        artifacts = payload.get("candidate_artifacts", {}) or {}
        resolutions = []
        for f in carried:
            resolutions.append({
                "finding_id": f["finding_id"],
                "resolved": True,
                "verified_evidence": _resolve_evidence(f, artifacts),
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
                 tokens_in: int = 140, tokens_out: int = 260):
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
            return AgentResult(ok=True, parsed=parsed, output_text=str(parsed),
                               tokens_in=self.tokens_in, tokens_out=self.tokens_out)
        if callable(entry):
            parsed = entry(call.payload)
            parsed = self._echo_identity(parsed, call)
            return AgentResult(ok=True, parsed=parsed, output_text=str(parsed),
                               tokens_in=self.tokens_in, tokens_out=self.tokens_out)
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
        return AgentResult(ok=True, parsed=parsed, output_text=str(parsed),
                           tokens_in=self.tokens_in, tokens_out=self.tokens_out)

    @staticmethod
    def _echo_identity(parsed: dict, call: RoleCall) -> dict:
        if isinstance(parsed, dict) and "request_id" not in parsed:
            parsed = dict(parsed)
        if isinstance(parsed, dict):
            out = dict(parsed)
            if "request_id" not in out or out.get("request_id") is None:
                out["request_id"] = call.request_id
            if call.candidate_id is not None:
                out.setdefault("candidate_id", call.candidate_id)
            return out
        return parsed

    # -------------------------------------------------------------- helpers
    def calls_of(self, role: str) -> list[RoleCall]:
        return [c for c in self.calls if c.role == role]

    def request_ids_of(self, role: str) -> list[str]:
        return [c.request_id for c in self.calls_of(role)]
