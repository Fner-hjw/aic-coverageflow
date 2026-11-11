"""Role-call dispatch: RoleCall envelope, prompt assembly, backend invocation,
strict output parsing, budget enforcement, and trace-record production.

Owner: stream A3. `RoleCall` is the single source of truth for the role-call
envelope (docs/contracts/interfaces.md section 1). Dispatch NEVER writes trace
or state itself - it returns a trace-record dict; only the orchestrator (A2)
appends it.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _prompt_assembly as pa
from .backends.base import AgentResult, HealthReport  # re-exported for protocol users

__all__ = ["RoleCall", "AgentResult", "HealthReport", "dispatch_role"]


@dataclass(frozen=True)
class RoleCall:
    """One bound role request dispatched to a backend (interfaces s1)."""

    role: str                    # ingest|extractor|model|writer|critic_c1..c4|generator_lead|critic_lead
    request_id: str
    candidate_id: str | None
    role_card: dict              # {"path": "prompts/ingest.md", "version": "1.0", "sha256": "..."}
    payload: dict                # bound request data injected into the assembled prompt
    output_schema: str           # json-schema ref; submission validated against it
    budget: dict                 # {"tokens_max": 60000, "timeout_seconds": 240}
    write_scope: list[str]       # exactly one artifact path for worker roles
    workdir: str | None = None   # case directory the role's file references resolve against


# ---------------------------------------------------------------------------
# Output-schema registry: minimal frozen-contract key checks.
#
# The full artifacts are defined in docs/contracts/state-and-artifacts.md
# sections 5-6; this registry pins the wire format each role must emit so an
# invalid submission can be *repairably* rejected at dispatch instead of
# failing later at the gate. Nested-item checks stay light on purpose -
# the orchestrator's artifact validation remains the final authority.
# ---------------------------------------------------------------------------

_SCHEMA_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    # wire-format key -> required top-level keys per frozen contract
    "ingest-report.json": (
        "role", "request_id", "candidate_id", "doc_id", "classification",
        "slices", "superseded", "conformance", "prompt_version",
    ),
    "facts.json": (
        "role", "request_id", "candidate_id", "facts", "missing", "notes",
        "conformance", "prompt_version",
    ),
    "forecast_assumptions.json": (
        "role", "horizon_years", "segments", "margin_glide",
        "terminal_basis", "conformance", "prompt_version",
    ),
    "model.json": ("artifact", "engine", "candidate_id", "cutoff", "series"),
    "valuation.json": ("artifact", "engine", "candidate_id", "cutoff", "dcf", "comps"),
    "report.md": (  # writer wire envelope: report.md itself is markdown
        "role", "request_id", "candidate_id", "report_md", "citations",
        "conformance", "prompt_version",
    ),
    "critic-report.json": (
        "role", "request_id", "candidate_id", "round", "verdict",
        "findings", "coverage", "conformance", "prompt_version",
    ),
    "merged-findings.json": (
        "round", "candidate_id", "findings", "clean", "coverage_ok",
        "reviewer_count", "fresh_context_proofs",
    ),
    "candidate-manifest.json": (
        "request_id", "candidate_id", "round", "reports", "evidence_index",
        "scope_statement",
    ),
}

# Which artifact each role emits. The orchestrator used to pass the literal
# string "references/schemas/1.0" for every role, which is not a registry key,
# so the per-artifact required-key check below never ran in the real pipeline.
ROLE_OUTPUT_SCHEMA: dict[str, str] = {
    "ingest": "ingest-report.json",
    "extractor": "facts.json",
    "model": "forecast_assumptions.json",
    "writer": "report.md",
    "generator_lead": "candidate-manifest.json",
    "critic_lead": "merged-findings.json",
    **{f"critic_c{i}": "critic-report.json" for i in range(1, 5)},
}


def output_schema_for(role: str) -> str:
    """The artifact name a role must emit; raises for an unknown role."""
    try:
        return ROLE_OUTPUT_SCHEMA[role]
    except KeyError:
        raise LookupError(
            f"no output schema registered for role {role!r}; known roles: "
            f"{sorted(ROLE_OUTPUT_SCHEMA)}"
        ) from None


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n```", re.DOTALL)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def build_bound_request(call: RoleCall) -> dict:
    """The bound request payload injected into the assembled prompt.

    Everything the role is allowed to see of its own dispatch context -
    integrity fields plus the orchestrator-bound payload - in canonical
    (sorted-key) JSON on assembly.
    """
    return {
        "request_id": call.request_id,
        "role": call.role,
        "candidate_id": call.candidate_id,
        "output_schema": call.output_schema,
        "write_scope": list(call.write_scope),
        "budget": dict(call.budget),
        "payload": call.payload,
    }


def parse_output_strict(raw: str, output_schema: str) -> tuple[dict | None, str | None]:
    """Strictly parse a raw model output into (parsed, error).

    Tolerates JSON wrapped in markdown code fences; anything else that does
    not parse as a JSON object, or parses but misses required schema keys,
    yields a repairable error describing exactly what to fix.
    Unknown ``output_schema`` refs fall back to dict-only validation.
    """
    if not raw or not raw.strip():
        return None, "empty output: emit the strict JSON artifact only, no prose"

    candidate = raw.strip()
    parsed: Any = None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        m = _FENCE_RE.search(candidate)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError as exc:
                return None, (
                    "output contains a fenced JSON block that does not parse "
                    f"({exc}); repair: emit exactly one valid JSON object "
                    "matching the artifact schema, no comments, no trailing text"
                )
        # last resort: a fenced block without the closing fence or prose-wrapped
        else:
            first, last = candidate.find("{"), candidate.rfind("}")
            if first != -1 and last > first:
                try:
                    parsed = json.loads(candidate[first : last + 1])
                except json.JSONDecodeError:
                    parsed = None
            if parsed is None:
                return None, (
                    "repair: output is not valid JSON. Emit exactly one JSON "
                    "object matching the artifact schema for "
                    f"'{output_schema}'; no prose, no markdown fences"
                )
    if not isinstance(parsed, dict):
        return None, (
            "repair: output parsed as JSON but is not an object; emit a "
            f"single JSON object matching '{output_schema}'"
        )
    required = _SCHEMA_REQUIRED_KEYS.get(output_schema)
    if required is None:
        # Fail closed: an unregistered ref means the caller wired the wrong
        # artifact name, and "dict-only" validation would accept anything.
        return None, (
            f"unknown output schema ref {output_schema!r}; registered refs: "
            f"{sorted(_SCHEMA_REQUIRED_KEYS)}"
        )
    missing = [k for k in required if k not in parsed]
    if missing:
        return None, (
            f"repair: output is schema-invalid - '{output_schema}' requires "
            f"top-level keys {list(required)}; missing: {missing}. Re-emit "
            "the complete JSON artifact per the role card CONTRACT"
        )
    return parsed, None


def check_cost_cap(call: RoleCall, result: AgentResult) -> str | None:
    """Return an error message if the call blew its per-call USD cap.

    ``call.budget["cost_usd_max"]`` of None/absent means unlimited; results
    that report no cost (cost_usd None) are never failed by this check.
    """
    cap = call.budget.get("cost_usd_max")
    cost = getattr(result, "cost_usd", None)
    if cap is None or cost is None:
        return None
    if cost > float(cap):
        return (
            f"cost cap exceeded: ${cost:.4f} > budget cost_usd_max=${float(cap):.4f}; "
            "repair: bound the output strictly to the artifact schema "
            "(no commentary, no restating inputs)"
        )
    return None


def check_token_cap(call: RoleCall, result: AgentResult) -> str | None:
    """Return an error message if the call blew its per-call token cap."""
    cap = call.budget.get("tokens_max")
    if cap is None:
        return None
    tokens_in = result.tokens_in if result.tokens_in is not None else 0
    tokens_out = result.tokens_out if result.tokens_out is not None else 0
    if tokens_in + tokens_out > int(cap):
        return (
            f"token cap exceeded: {tokens_in} in + {tokens_out} out = "
            f"{tokens_in + tokens_out} > budget tokens_max={cap}; "
            "repair: bound the output strictly to the artifact schema "
            "(no commentary, no restating inputs)"
        )
    return None


# Payload keys whose `path`/`sha256` pairs describe files the harness will
# *produce*, not inputs the role must read. Ingest is handed the slicer's
# anchor candidates before the slices exist, so checking them would block
# every case at its first stage.
_NON_REFERENCE_KEYS = frozenset({"anchor_candidates"})


def _iter_file_refs(node: Any):
    """Yield dicts in *node* that look like case-relative file references."""
    if isinstance(node, dict):
        path = node.get("path")
        if isinstance(path, str) and path:
            parts = Path(path).parts
            if not os.path.isabs(path) and ".." not in parts:
                yield node
        for key, value in node.items():
            if key in _NON_REFERENCE_KEYS:
                continue
            yield from _iter_file_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_file_refs(value)


def missing_payload_inputs(payload: Any, workdir: Path | str) -> list[str]:
    """Case-relative file references in *payload* that are absent or changed.

    A role is handed paths, not file contents. If those paths do not resolve
    under the case directory the role can only burn its whole timeout
    discovering that, so the check runs before the backend is invoked.
    """
    root = Path(workdir)
    problems: list[str] = []
    for ref in _iter_file_refs(payload):
        rel = ref["path"]
        target = root / rel
        if not target.is_file():
            problems.append(f"{rel} (absent)")
            continue
        want = ref.get("sha256")
        if isinstance(want, str) and want:
            got = hashlib.sha256(target.read_bytes()).hexdigest()
            if got != want:
                problems.append(
                    f"{rel} (sha256 {got[:12]} != declared {want[:12]})")
    return problems


def _role_card_identity(call: RoleCall, card_path: Path) -> tuple[str | None, str | None]:
    """Return (card_version, mismatch_error) for the card actually on disk.

    The trace must record the version the card really carries, not whatever
    the caller claimed; a caller that declares a *different* version or sha
    is a hard error (a swapped card must never be dispatched silently).
    """
    card = pa.parse_role_card(card_path)
    version = card["version"]
    declared_version = call.role_card.get("version")
    if declared_version is not None and str(declared_version) != version:
        return version, (
            f"role card version mismatch for {call.role}: caller declared "
            f"{declared_version!r}, {card_path.name} is {version!r}")
    declared_sha = call.role_card.get("sha256")
    if isinstance(declared_sha, str) and declared_sha:
        actual = hashlib.sha256(card["body"].encode("utf-8")).hexdigest()
        if actual != declared_sha:
            return version, (
                f"role card sha256 mismatch for {call.role}: caller declared "
                f"{declared_sha[:12]}, {card_path.name} hashes to {actual[:12]}")
    return version, None


def _trace_record(call: RoleCall, result: AgentResult, prompt_sha: str | None,
                  card_version: str | None) -> dict:
    """The AGENT_RESULT trace record for one dispatch (orchestrator appends it)."""
    output_sha = (
        pa.assembled_prompt_sha256(result.output_text)
        if result.output_text else None
    )
    return {
        "ts": _utc_now_iso(),
        "event": "AGENT_RESULT",
        "case_id": None,          # orchestrator fills from its own scope
        "request_id": call.request_id,
        "candidate_id": call.candidate_id,
        "role": call.role,
        "round": call.payload.get("round") if isinstance(call.payload, dict) else None,
        "artifact": call.write_scope[0] if call.write_scope else None,
        # hash of the raw model output, not of the artifact the orchestrator
        # writes (that one is recorded by the checkpoint/candidate manifest)
        "artifact_sha256": output_sha,
        "prompt_versions": {"role_card": card_version},
        "backend": result.backend or "?",
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "tokens_cached": getattr(result, "tokens_cached", None),
        "cost_usd": getattr(result, "cost_usd", None),
        "model": getattr(result, "model", None),
        "detail": {
            "ok": result.ok,
            "error": result.error,
            "elapsed_seconds": result.elapsed_seconds,
            "assembled_prompt_sha256": prompt_sha,
            "output_sha256": output_sha,
            "role_card_path": str(call.role_card.get("path", "")),
        },
    }


def dispatch_role(
    call: RoleCall,
    backend: Any,
    *,
    root: Path | str | None = None,
) -> tuple[AgentResult, dict]:
    """Assemble, invoke, strictly parse, enforce budgets; return (result, trace).

    Dispatch NEVER writes trace or state itself: the returned trace-record
    dict is appended by the orchestrator (A2) to trace.jsonl. ReplayMissError
    from a replay backend propagates deliberately - a missing recording is a
    hard error, not a role failure to be retried.
    """
    root_path = Path(root) if root is not None else pa.default_root()
    backend_name = getattr(backend, "name", "?")

    # 1. The role's inputs must exist before any time is spent on the model.
    workdir = getattr(call, "workdir", None)
    if workdir:
        missing = missing_payload_inputs(call.payload, workdir)
        if missing:
            result = AgentResult(
                ok=False,
                error=(
                    "bound request references inputs that are absent or "
                    f"changed under {workdir}: {missing}. The role cannot read "
                    "them; repair the case inputs instead of retrying the role."
                ),
                backend=backend_name,
                blocking=True,  # no retry can fix a missing file
            )
            result.trace = _trace_record(call, result, None, None)
            return result, result.trace

    # 2. The card on disk must be the card the caller thinks it dispatched.
    card_path = root_path / call.role_card["path"]
    card_version, mismatch = _role_card_identity(call, card_path)
    if mismatch is not None:
        result = AgentResult(ok=False, error=mismatch, backend=backend_name)
        result.trace = _trace_record(call, result, None, card_version)
        return result, result.trace

    # 3. The output schema ref must name a registered artifact. This is a
    #    wiring bug, not a model failure: no retry can fix it.
    if call.output_schema not in _SCHEMA_REQUIRED_KEYS:
        result = AgentResult(
            ok=False,
            error=(
                f"unknown output schema ref {call.output_schema!r} for role "
                f"{call.role!r}; registered refs: {sorted(_SCHEMA_REQUIRED_KEYS)}. "
                "Repair the dispatch wiring instead of retrying the role."
            ),
            backend=backend_name,
            blocking=True,
        )
        result.trace = _trace_record(call, result, None, card_version)
        return result, result.trace

    bound_request = build_bound_request(call)
    prompt = pa.assemble_prompt(card_path, bound_request, root=root_path)
    prompt_sha = pa.assembled_prompt_sha256(prompt)

    result = backend.invoke(call, prompt)

    # Strict parse when the backend produced output
    if result.ok and result.output_text is not None:
        parsed, parse_error = parse_output_strict(result.output_text, call.output_schema)
        if parse_error is not None:
            result = result.failed(f"schema-invalid output for '{call.output_schema}': {parse_error}")
        else:
            result.parsed = parsed
    elif result.ok:
        result = result.failed("backend returned ok but no output text")

    # Per-call token cap (timeout is enforced by the backend via budget)
    if result.ok:
        cap_error = check_token_cap(call, result)
        if cap_error is not None:
            result = result.failed(f"budget violation: {cap_error}")
    if result.ok:
        cost_error = check_cost_cap(call, result)
        if cost_error is not None:
            result = result.failed(f"budget violation: {cost_error}")

    result.trace = _trace_record(call, result, prompt_sha, card_version)
    return result, result.trace
