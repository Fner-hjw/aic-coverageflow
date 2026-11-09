"""Independent numerical/structural gates — NO LLM (owner: stream A2).

Gates are pure, deterministic checks over candidate artifacts. Same inputs
always yield the same outcomes (quality bar #2). The gate set:

  input_cutoff            every manifest doc filed <= mandate cutoff
  facts_schema            facts.json vs JSON schema (default
                          references/model-schema.json, owned by stream A1;
                          tests inject a minimal fixture schema)
  citation_presence       every fact carries a full citation; cited docs must
                          be in the input manifest
  forecast_bounds         per-segment growth bounds + margin floor/ceiling
                          vs mandate.assumptions.forecast_bounds
  candidate_manifest_integrity  listed artifacts exist and hashes match
  recompute               deterministic engines recompute model/valuation
                          (engine callables injected per contracts interfaces
                          section 4; never imported here)
  report_evidence_links   every numeric token in report.md carries an
                          [EV:<key>] evidence reference

Every gate returns (ok, findings) where findings are ledger-shaped dicts
(dedup_key, severity, summary, affected_artifacts, required_evidence).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import statestore

DEFAULT_SCHEMA_PATH = Path("references") / "model-schema.json"

GATE_ORDER = (
    "input_cutoff",
    "facts_schema",
    "citation_presence",
    "forecast_bounds",
    "candidate_manifest_integrity",
    "recompute",
    "report_evidence_links",
)

CITATION_KEYS = ("doc_id", "slice_id", "anchor", "locator")
REPORT_ARTIFACT = "report.md"


# ---------------------------------------------------------------------------
# finding construction helpers
# ---------------------------------------------------------------------------

def _finding(gate: str, dedup_key: str, summary: str,
             affected: list[str], required_evidence: str = "",
             severity: str = "major") -> dict:
    return {
        "gate": gate,
        "dedup_key": dedup_key,
        "severity": severity,
        "summary": summary,
        "affected_artifacts": affected,
        "required_evidence": required_evidence,
    }


@dataclass
class GateOutcome:
    gate: str
    ok: bool
    findings: list[dict] = field(default_factory=list)


@dataclass
class PipelineReport:
    results: list[GateOutcome]

    @property
    def passed(self) -> bool:
        return all(o.ok for o in self.results)

    @property
    def finding_dicts(self) -> list[dict]:
        return [f for o in self.results for f in o.findings]

    def ledger_findings(self) -> list[dict]:
        return [dict(f) for f in self.finding_dicts]


# ---------------------------------------------------------------------------
# minimal JSON-schema subset validator (stdlib only; draft-07 subset)
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "object": dict, "array": list, "string": str,
    "number": (int, float), "integer": int, "boolean": bool, "null": type(None),
}


def validate_against_schema(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """Validate *instance* against a JSON-schema subset.

    Supported keywords: type, enum, required, properties, items, minItems,
    minLength, minimum, maximum. Returns human-readable error strings.
    """
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        py = _TYPE_MAP.get(t)
        if py is None:
            errors.append(f"{path}: unknown schema type {t!r}")
        elif not isinstance(instance, py):
            errors.append(f"{path}: expected {t}, got {type(instance).__name__}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in instance:
                errors.extend(validate_against_schema(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list):
        if "items" in schema:
            for i, item in enumerate(instance):
                errors.extend(validate_against_schema(item, schema["items"], f"{path}[{i}]"))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items, got {len(instance)}")
    if isinstance(instance, str) and "minLength" in schema and len(instance) < schema["minLength"]:
        errors.append(f"{path}: shorter than minLength {schema['minLength']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} above maximum {schema['maximum']}")
    return errors


# ---------------------------------------------------------------------------
# gate 1: input-manifest cutoff discipline
# ---------------------------------------------------------------------------

def gate_manifest_cutoff(manifest: dict, cutoff_date: str) -> tuple[bool, list[dict]]:
    """Every document in the manifest must be filed on or before the cutoff."""
    findings: list[dict] = []
    cutoff = str(cutoff_date)
    for doc in manifest.get("documents", []):
        if str(doc.get("filed", "")) > cutoff:
            findings.append(_finding(
                "input_cutoff", f"post-cutoff:{doc.get('doc_id')}",
                f"document {doc.get('doc_id')} filed {doc.get('filed')} is after "
                f"the information cutoff {cutoff}",
                ["inputs/manifest.json"],
                required_evidence="an input manifest containing only documents filed "
                                  "on or before the cutoff",
            ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 2: facts.json schema
# ---------------------------------------------------------------------------

def gate_facts_schema(facts: Any, schema_path: Path | str) -> tuple[bool, list[dict]]:
    """Validate facts.json against the JSON schema at *schema_path* (fail-closed)."""
    schema_path = Path(schema_path)
    if not schema_path.exists():
        return False, [_finding(
            "facts_schema", "schema:missing",
            f"model schema file not found at {schema_path} — gate fails closed",
            ["facts.json"], required_evidence="schema file present at the configured path",
        )]
    schema = statestore.read_json(schema_path)
    errors = validate_against_schema(facts, schema, "$")
    findings = [
        _finding("facts_schema", f"schema:{e}", f"facts.json schema violation: {e}",
                 ["facts.json"], required_evidence="a facts.json instance that validates "
                 "against references/model-schema.json", severity="major")
        for e in errors
    ]
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 3: citation presence per fact
# ---------------------------------------------------------------------------

def gate_citation_presence(facts: dict, manifest: dict | None = None) -> tuple[bool, list[dict]]:
    """Every fact must carry a complete citation; cited docs must be in the manifest."""
    findings: list[dict] = []
    known_docs: set[str] | None = None
    if manifest is not None:
        known_docs = {d.get("doc_id") for d in manifest.get("documents", [])}
    for idx, fact in enumerate(facts.get("facts", [])):
        field_name = fact.get("field", f"<fact {idx}>")
        cit = fact.get("citation") or {}
        missing = [k for k in CITATION_KEYS if not (isinstance(cit, dict) and cit.get(k))]
        if missing:
            findings.append(_finding(
                "citation_presence", f"uncited:{field_name}",
                f"fact {field_name} lacks citation fields: {', '.join(missing)}",
                ["facts.json"],
                required_evidence=f"complete citation (doc_id, slice_id, anchor, "
                                  f"locator) for {field_name}",
            ))
            continue
        if known_docs is not None and cit["doc_id"] not in known_docs:
            findings.append(_finding(
                "citation_presence", f"unknown-doc:{field_name}",
                f"fact {field_name} cites {cit['doc_id']} which is not in the input manifest",
                ["facts.json"],
                required_evidence=f"citation to a document in inputs/manifest.json "
                                  f"(cutoff discipline)",
            ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 4: forecast bounds vs mandate
# ---------------------------------------------------------------------------

def gate_forecast_bounds(forecast: dict, bounds: dict) -> tuple[bool, list[dict]]:
    """Segment growth vectors and margin glide path must respect mandate bounds."""
    findings: list[dict] = []
    seg_bounds = (bounds or {}).get("segments", {}) or {}
    horizon = int(forecast.get("horizon_years", 5))
    for seg in forecast.get("segments", []):
        name = seg.get("segment", "<unnamed>")
        growth = seg.get("growth", [])
        if len(growth) != horizon:
            findings.append(_finding(
                "forecast_bounds", f"horizon:{name}",
                f"segment {name} growth has {len(growth)} values but the mandate horizon is "
                f"{horizon} years",
                ["forecast_assumptions.json"],
                required_evidence=f"exactly {horizon} growth values for {name}",
            ))
        lim = seg_bounds.get(name)
        if lim is None:
            findings.append(_finding(
                "forecast_bounds", f"unbounded-segment:{name}",
                f"segment {name} has no mandate growth bounds — out-of-bounds cannot be "
                f"proven",
                ["forecast_assumptions.json"],
                required_evidence=f"mandate forecast_bounds for segment {name}",
            ))
            continue
        lo, hi = float(lim[0]), float(lim[1])
        for i, g in enumerate(growth):
            if g < lo or g > hi:
                findings.append(_finding(
                    "forecast_bounds", f"growth:{name}:{i + 1}",
                    f"segment {name} growth year {i + 1} = {g} outside mandate bounds "
                    f"[{lo}, {hi}]",
                    ["forecast_assumptions.json"],
                    required_evidence=f"{name} growth year {i + 1} within [{lo}, {hi}]",
                ))
    floor = bounds.get("margin_floor_gross")
    ceiling = bounds.get("margin_ceiling_gross")
    glide = (forecast.get("margin_glide") or {}).get("gross", [])
    for i, m in enumerate(glide):
        if floor is not None and m < floor:
            findings.append(_finding(
                "forecast_bounds", f"margin-floor:{i + 1}",
                f"gross margin year {i + 1} = {m} below mandate floor {floor}",
                ["forecast_assumptions.json"],
                required_evidence=f"gross margin year {i + 1} >= {floor}",
            ))
        if ceiling is not None and m > ceiling:
            findings.append(_finding(
                "forecast_bounds", f"margin-ceiling:{i + 1}",
                f"gross margin year {i + 1} = {m} above mandate ceiling {ceiling}",
                ["forecast_assumptions.json"],
                required_evidence=f"gross margin year {i + 1} <= {ceiling}",
            ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 5: candidate-manifest integrity
# ---------------------------------------------------------------------------

def gate_candidate_manifest_integrity(manifest: dict, case_dir: Path | str) -> tuple[bool, list[dict]]:
    """Every listed candidate artifact must exist with the recorded sha256."""
    findings: list[dict] = []
    case_dir = Path(case_dir)
    for rep in manifest.get("reports", []):
        rel = rep.get("artifact", "")
        path = case_dir / rel
        if not path.exists():
            findings.append(_finding(
                "candidate_manifest_integrity", f"missing-artifact:{rel}",
                f"candidate artifact missing on disk: {rel}",
                [rel], required_evidence="the artifact listed in candidate-manifest.json",
            ))
            continue
        actual = statestore.sha256_file(path)
        if actual != rep.get("sha256"):
            findings.append(_finding(
                "candidate_manifest_integrity", f"hash-mismatch:{rel}",
                f"candidate artifact sha256 mismatch for {rel}: manifest records "
                f"{rep.get('sha256')[:12]}…, disk holds {actual[:12]}…",
                [rel], required_evidence="artifact bytes matching the recorded sha256",
            ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 7: report evidence-link rule
# ---------------------------------------------------------------------------

EV_MARKER = re.compile(r"\[EV:([^\]\s]+)\]")
NUMERIC_TOKEN = re.compile(r"(?<![\w])\d[\d,\.]*")


def gate_report_evidence_links(report_md: str,
                               evidence_index: dict | None = None) -> tuple[bool, list[dict]]:
    """Every numeric token in report.md carries an evidence reference.

    Rule (deterministic): each standalone numeric token (not glued to word
    characters, e.g. Q3/FY26 are exempt) must be followed by an [EV:<key>]
    marker on the same line before the next numeric token. When an
    *evidence_index* is supplied, the marker key must exist in it.
    """
    findings: list[dict] = []
    if not report_md:
        return True, findings
    lines = report_md.splitlines()
    for ln, line in enumerate(lines, start=1):
        markers = [(m.start(), m.end(), m.group(1)) for m in EV_MARKER.finditer(line)]
        if evidence_index is not None:
            for _, _, key in markers:
                if key not in evidence_index:
                    findings.append(_finding(
                        "report_evidence_links", f"unknown-ev:{key}",
                        f"evidence marker [EV:{key}] on line {ln} is not in the candidate "
                        f"evidence index",
                        [REPORT_ARTIFACT],
                        required_evidence=f"a marker key present in the candidate "
                                          f"evidence index",
                    ))
        marker_spans = [(s, e) for s, e, _ in markers]
        tokens = [
            (m.start(), m.end(), m.group(0))
            for m in NUMERIC_TOKEN.finditer(line)
            if not any(s <= m.start() < e for s, e in marker_spans)  # inside [EV:..]
        ]
        if not tokens:
            continue
        marker_starts = [s for s, _, _ in markers]
        for i, (ts, te, tok) in enumerate(tokens):
            boundary = tokens[i + 1][0] if i + 1 < len(tokens) else len(line)
            covered = any(ms >= te and ms < boundary for ms in marker_starts)
            if not covered:
                findings.append(_finding(
                    "report_evidence_links", f"unlinked-number:{ln}:{tok}",
                    f'numeric token "{tok}" on line {ln} lacks an evidence reference '
                    f"[EV:key] before the next number",
                    [REPORT_ARTIFACT],
                    required_evidence=f'an [EV:...] evidence reference for "{tok}"',
                ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 6 + pipeline: recomputation via injected engines
# ---------------------------------------------------------------------------

@dataclass
class CandidateContext:
    """Everything a gate pipeline needs to independently verify a candidate."""
    facts: dict
    forecast: dict
    report_md: str
    candidate_manifest: dict
    input_manifest: dict
    mandate: dict
    model_json: dict | None = None
    valuation_json: dict | None = None
    case_dir: Path | str = "."
    mapping_version: str = "1.0"


def _num_close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= max(tol, tol * max(abs(a), abs(b)))


class GateEngine:
    """Gate pipeline with injected engine callables (contracts interfaces §4).

    build_model_fn  : ``(facts, mapping_version) -> model``
    value_fn        : ``(model, forecast, assumptions) -> valuation``
    In production the orchestrator injects the real callables from
    coverageflow.model_engine / coverageflow.valuation_engine (stream A1);
    tests inject stub doubles. Never imported here — import stays at the
    orchestrator seam.
    """

    def __init__(self, schema_path: Path | str = DEFAULT_SCHEMA_PATH,
                 build_model_fn: Callable | None = None,
                 value_fn: Callable | None = None,
                 tolerance: float = 1e-6):
        self.schema_path = Path(schema_path)
        self.build_model_fn = build_model_fn
        self.value_fn = value_fn
        self.tolerance = tolerance

    # ------------------------------------------------------------ schema
    def _schema_findings(self, facts: Any) -> tuple[bool, list[dict]]:
        return gate_facts_schema(facts, self.schema_path)

    # --------------------------------------------------------- recompute
    def run_recompute(self, facts: dict, forecast: dict, mandate: dict,
                      model_json: dict | None,
                      valuation_json: dict | None) -> PipelineReport:
        findings: list[dict] = []
        if self.build_model_fn is None:
            return PipelineReport([GateOutcome("recompute", False, [_finding(
                "recompute", "engine:missing",
                "no model engine callable injected — cannot recompute",
                ["model.json"], required_evidence="engine callables injected")])])
        try:
            model = self.build_model_fn(facts, (model_json or {}).get(
                "mapping_tables") or forecast.get("mapping_version") or "1.0")
        except Exception as exc:  # engine faults become findings, never crashes
            return PipelineReport([GateOutcome("recompute", False, [_finding(
                "recompute", "engine:model-fault",
                f"model engine failed during recomputation: {exc}",
                ["model.json"], required_evidence="a runnable model engine")])])
        # series comparison
        submitted = (model_json or {}).get("series", {})
        recomputed = model.get("series", {}) if isinstance(model, dict) else {}
        for series_key, values in recomputed.items():
            sub_vals = submitted.get(series_key, {})
            if not sub_vals:
                findings.append(_finding(
                    "recompute", f"series-missing:{series_key}",
                    f"recomputed series {series_key} absent from submitted model.json",
                    ["model.json"], required_evidence=f"series {series_key} in model.json"))
                continue
            for period, value in values.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                sub = sub_vals.get(period)
                if not isinstance(sub, (int, float)) or isinstance(sub, bool):
                    findings.append(_finding(
                        "recompute", f"series-value:{series_key}:{period}",
                        f"recomputed {series_key} for {period} = {value} has no numeric "
                        f"counterpart in submitted model.json",
                        ["model.json"],
                        required_evidence=f"matching {series_key}[{period}]"))
                elif not _num_close(float(value), float(sub)):
                    findings.append(_finding(
                        "recompute", f"series-value:{series_key}:{period}",
                        f"recomputed {series_key} for {period} = {value} disagrees with "
                        f"submitted model.json value {sub}",
                        ["model.json"],
                        required_evidence=f"recomputed {series_key}[{period}] agreeing "
                                          f"with the engine output"))
        # valuation comparison
        if valuation_json is not None and self.value_fn is not None:
            try:
                val = self.value_fn(model, forecast, mandate.get("assumptions", {}))
            except Exception as exc:
                findings.append(_finding(
                    "recompute", "engine:valuation-fault",
                    f"valuation engine failed during recomputation: {exc}",
                    ["valuation.json"], required_evidence="a runnable valuation engine"))
                val = None
            if val is not None:
                val = val.get("payload", val) if not hasattr(val, "payload") else val.payload
                for section, keys in (("dcf", ("equity_value", "per_share")),):
                    for k in keys:
                        rec_v = (val.get(section) or {}).get(k)
                        sub_v = (valuation_json.get(section) or {}).get(k)
                        if isinstance(rec_v, (int, float)) and not isinstance(rec_v, bool):
                            if not isinstance(sub_v, (int, float)) or isinstance(sub_v, bool):
                                findings.append(_finding(
                                    "recompute", f"valuation:{section}:{k}",
                                    f"recomputed {section}.{k} = {rec_v} missing in "
                                    f"submitted valuation.json",
                                    ["valuation.json"],
                                    required_evidence=f"numeric {section}.{k} in valuation.json"))
                            elif not _num_close(float(rec_v), float(sub_v), max(self.tolerance, 1e-4)):
                                findings.append(_finding(
                                    "recompute", f"valuation:{section}:{k}",
                                    f"recomputed {section}.{k} = {rec_v} disagrees with "
                                    f"submitted valuation.json value {sub_v}",
                                    ["valuation.json"],
                                    required_evidence=f"recomputed {section}.{k} agreeing "
                                                      f"with the engine output"))
        return PipelineReport([GateOutcome("recompute", not findings, findings)])

    # ------------------------------------------------------------ pipeline
    def run_all(self, ctx: CandidateContext) -> PipelineReport:
        """Run the full gate pipeline in GATE_ORDER; deterministic, no LLM."""
        outcomes: list[GateOutcome] = []
        for gate in GATE_ORDER:
            ok, findings = self._run_gate(gate, ctx)
            outcomes.append(GateOutcome(gate, ok, findings))
        return PipelineReport(outcomes)

    def _run_gate(self, gate: str, ctx: CandidateContext) -> tuple[bool, list[dict]]:
        mandate = ctx.mandate
        if gate == "input_cutoff":
            return gate_manifest_cutoff(ctx.input_manifest, mandate["cutoff_date"])
        if gate == "facts_schema":
            return self._schema_findings(ctx.facts)
        if gate == "citation_presence":
            return gate_citation_presence(ctx.facts, manifest=ctx.input_manifest)
        if gate == "forecast_bounds":
            return gate_forecast_bounds(
                ctx.forecast, mandate["assumptions"]["forecast_bounds"])
        if gate == "candidate_manifest_integrity":
            return gate_candidate_manifest_integrity(ctx.candidate_manifest, ctx.case_dir)
        if gate == "recompute":
            return self.run_recompute_helper(ctx)
        if gate == "report_evidence_links":
            return gate_report_evidence_links(
                ctx.report_md,
                evidence_index=ctx.candidate_manifest.get("evidence_index"),
            )
        raise ValueError(f"unknown gate: {gate}")

    def run_recompute_helper(self, ctx: CandidateContext) -> tuple[bool, list[dict]]:
        rep = self.run_recompute(ctx.facts, ctx.forecast, ctx.mandate,
                                 ctx.model_json, ctx.valuation_json)
        assert len(rep.results) == 1
        return rep.results[0].ok, rep.results[0].findings
