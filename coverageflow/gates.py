"""Independent numerical/structural gates — NO LLM (owner: stream A2).

Gates are pure, deterministic checks over candidate artifacts. Same inputs
always yield the same outcomes (quality bar #2). The gate set:

  input_cutoff            every manifest doc filed <= mandate cutoff
  facts_schema            facts.json vs JSON schema (default
                          references/model-schema.json, owned by stream A1;
                          tests inject a minimal fixture schema)
  citation_presence       every fact carries a full citation; cited docs must
                          be in the input manifest
  period_coverage         every statement present in facts carries the trigger
                          filing's current period
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
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import statestore

DEFAULT_SCHEMA_PATH = Path("references") / "model-schema.json"

GATE_ORDER = (
    "input_cutoff",
    "facts_schema",
    "citation_presence",
    "period_coverage",
    "forecast_bounds",
    "candidate_manifest_integrity",
    "recompute",
    "terminal_reconciliation",
    "report_evidence_links",
    "controls_conformance",
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


_SCHEMA_KEYWORDS = frozenset({
    "type", "enum", "required", "properties", "items", "minItems",
    "minLength", "minimum", "maximum", "anyOf", "allOf", "oneOf", "$ref",
})


def is_json_schema(schema: Any) -> bool:
    """True when *schema* actually constrains an instance.

    ``references/model-schema.json`` is a field catalogue (canonical field
    names, units, period labels), not a JSON Schema. Validating against it
    finds no keywords and passes everything, so the gate must refuse it.
    """
    if not isinstance(schema, dict):
        return False
    return any(keyword in schema for keyword in _SCHEMA_KEYWORDS)


def validate_against_schema(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """Validate *instance* against a JSON-schema subset.

    Supported keywords: type, enum, required, properties, items, minItems,
    minLength, minimum, maximum. Returns human-readable error strings.
    """
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        # JSON Schema allows "type": ["string", "null"]; a list is not a dict
        # key, so _TYPE_MAP.get(t) would raise TypeError on it.
        types = t if isinstance(t, list) else [t]
        unknown = [x for x in types if x not in _TYPE_MAP]
        if unknown:
            errors.append(f"{path}: unknown schema type {unknown[0]!r}")
        elif not any(isinstance(instance, _TYPE_MAP[x]) for x in types):
            errors.append(
                f"{path}: expected {'|'.join(str(x) for x in types)}, "
                f"got {type(instance).__name__}"
            )
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")
    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for key, sub in props.items():
                if key in instance and isinstance(sub, dict):
                    errors.extend(validate_against_schema(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list):
        if isinstance(schema.get("items"), dict):
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

def _parse_iso_date(value: object) -> date | None:
    """Parse an ISO-8601 date, or None when absent/unparsable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def gate_manifest_cutoff(manifest: dict, cutoff_date: str) -> tuple[bool, list[dict]]:
    """Every document in the manifest must be filed on or before the cutoff.

    Dates are parsed, never compared as strings: lexicographic comparison
    admits ``"11/15/2026"`` (which sorts before ``"2026-10-30"``) and treats a
    missing ``filed`` as ``""``, i.e. always before the cutoff.
    """
    findings: list[dict] = []
    cutoff = _parse_iso_date(cutoff_date)
    if cutoff is None:
        return False, [_finding(
            "input_cutoff", "cutoff:unparsable",
            f"information cutoff {cutoff_date!r} is not an ISO-8601 date "
            "(YYYY-MM-DD); the gate cannot compare and fails closed",
            ["inputs/manifest.json"],
            required_evidence="an ISO-8601 cutoff date",
        )]
    for doc in manifest.get("documents", []):
        doc = doc or {}
        doc_id = doc.get("doc_id")
        filed = _parse_iso_date(doc.get("filed"))
        if filed is None:
            findings.append(_finding(
                "input_cutoff", f"filed:unparsable:{doc_id}",
                f"document {doc_id} has no parsable ISO-8601 filing date "
                f"({doc.get('filed')!r}); the cutoff cannot be verified",
                ["inputs/manifest.json"],
                required_evidence="an ISO-8601 filed date for every document",
            ))
            continue
        if filed > cutoff:
            findings.append(_finding(
                "input_cutoff", f"post-cutoff:{doc_id}",
                f"document {doc_id} filed {doc.get('filed')} is after "
                f"the information cutoff {cutoff_date}",
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
    if not is_json_schema(schema):
        # references/model-schema.json is a field catalogue, not a JSON Schema:
        # validating against it silently passes every instance.
        return False, [_finding(
            "facts_schema", "schema:not-a-schema",
            f"{schema_path} carries no JSON Schema validation keywords at its "
            "root (type/required/properties/items/enum/...), so it cannot "
            "constrain facts.json — gate fails closed",
            ["facts.json"],
            required_evidence="a JSON Schema for facts.json at the configured path",
        )]
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
# gate: the trigger filing's current period is covered for every statement
# ---------------------------------------------------------------------------

PERIOD_COVERAGE_STATEMENTS = (
    "income_statement", "balance_sheet", "cash_flow", "cash_flow_statement",
    "segments",
)


def gate_period_coverage(facts: dict, current_period: str,
                         statements: tuple[str, ...] = PERIOD_COVERAGE_STATEMENTS,
                         ) -> tuple[bool, list[dict]]:
    """Every statement type present in the facts must carry the current period.

    ``current_period`` is the trigger filing's period label (``YYYY-QN``, as
    the extractor emits it). For each statement prefix (the part of ``field``
    before the first dot) that appears in ``facts`` at all, at least one fact
    must be for ``current_period`` (a ``-YTD`` variant counts: a 10-Q cash-flow
    statement is year-to-date only). This catches an extraction that, for
    example, has only prior-year segment facts.
    """
    findings: list[dict] = []
    if not current_period:
        return (True, findings)
    accepted = {current_period, f"{current_period}-YTD"}
    seen: dict[str, set[str]] = {}
    for fact in (facts or {}).get("facts", []):
        field_name = str(fact.get("field", ""))
        prefix = field_name.split(".", 1)[0]
        if prefix in statements:
            seen.setdefault(prefix, set()).add(str(fact.get("period", "")))
    for prefix in statements:
        periods = seen.get(prefix)
        if periods is None or periods & accepted:
            continue
        findings.append(_finding(
            "period_coverage", f"period-missing:{prefix}",
            f"{prefix} facts cover periods {sorted(periods)} but not the trigger "
            f"filing's current period {current_period}",
            ["facts.json"],
            required_evidence=f"{prefix} facts for {current_period} cited to the "
                              f"trigger filing's slice",
        ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# terminal value reconciliation (valuation_engine@2.0)
# ---------------------------------------------------------------------------

def gate_terminal_reconciliation(valuation: dict, mandate: dict) -> tuple[bool, list[dict]]:
    """Exit-multiple and Gordon terminal values must agree within tolerance.

    Fails when ``dcf.terminal.reconciliation.within`` is false. The tolerance
    is the engine's (mandate ``terminal_tolerance``); the mandate argument is
    used only to report it if the artifact lacks one.
    """
    term = ((valuation or {}).get("dcf") or {}).get("terminal") or {}
    rec = term.get("reconciliation")
    if not isinstance(rec, dict):
        return False, [_finding(
            "terminal_reconciliation", "terminal:reconciliation-missing",
            "valuation.json carries no dcf.terminal.reconciliation block",
            ["valuation.json"],
            required_evidence="terminal reconciliation from valuation_engine@2.0")]
    # Two thresholds. ``terminal_tolerance`` (default 35%) only decides how the
    # engine forms the target (mean of both terminal values, or Gordon alone).
    # The gate blocks on ``terminal_gate_ratio_max`` (default 3x): a candidate
    # whose terminal values differ by more than that, or whose Gordon value is
    # not positive, is broken, not merely debatable. The exit multiple is a
    # human mandate input, so blocking on the tight band would stop cases no
    # agent can repair.
    assumptions = (mandate or {}).get("assumptions") or {}
    hard = float(assumptions.get("terminal_gate_ratio_max", 3.0))
    ratio = rec.get("ratio")
    if rec.get("within") or (ratio is not None and 1.0 / hard <= ratio <= hard):
        return True, []
    tol = f"{hard:.1f}x"
    tv_exit = (term.get("exit_multiple") or {}).get("terminal_value")
    tv_g = (term.get("gordon") or {}).get("terminal_value")
    mult = (term.get("exit_multiple") or {}).get("multiple")
    g = (term.get("gordon") or {}).get("terminal_growth")
    imp_mult = term.get("implied_exit_multiple_from_gordon")
    imp_g = term.get("implied_growth_from_exit")

    def f(v, spec):
        return "n/a" if v is None else format(v, spec)

    finding = _finding(
        "terminal_reconciliation", "terminal:outside-tolerance",
        f"exit-multiple terminal value {f(tv_exit, ',.0f')} (multiple {f(mult, '.1f')}x) "
        f"and Gordon terminal value {f(tv_g, ',.0f')} (growth {f(g, '.2%')}) "
        f"differ: ratio {f(ratio, '.2f')} outside the gate limit {tol}; "
        f"the Gordon value implies an exit multiple of {f(imp_mult, '.1f')}x and "
        f"the exit multiple implies terminal growth of {f(imp_g, '.2%')}",
        ["valuation.json"],
        required_evidence="terminal assumptions (exit multiple, terminal growth, "
                          "WACC) that reconcile within the mandate tolerance")
    # read back by the controller next round to tell "the repair moved the
    # ratio" from "nothing a role can change is moving it"
    finding["metrics"] = {"ratio": ratio}
    return False, [finding]


terminal_reconciliation = gate_terminal_reconciliation


# ---------------------------------------------------------------------------
# terminal feasibility: can ANY forecast satisfy the gate under this mandate?
# ---------------------------------------------------------------------------

DEFAULT_EBIT_TO_EBITDA_MAX = 0.95


def terminal_feasibility(mandate: dict) -> dict:
    """Best terminal-value ratio any forecast could reach under the mandate.

    The engine's Gordon value is ``NOPAT * (1+g) / (WACC - g)`` less working
    capital, and its exit value is ``multiple * EBITDA``. So
    ``TV_gordon / EBITDA <= (1 - tax) * (EBIT/EBITDA) * (1+g) / (WACC - g)``.
    The model role can move EBIT/EBITDA (and nothing else in that bound); the
    WACC, growth, tax rate and exit multiple are mandate inputs. With EBIT /
    EBITDA at its ceiling (``terminal_ebit_to_ebitda_max``, default 0.95, as
    depreciation is never ~0) the ratio ``TV_exit / TV_gordon`` is as low as it
    can go. If that is still above the gate limit, no role can repair it.
    """
    a = (mandate or {}).get("assumptions") or {}
    try:
        w, g = float(a["wacc"]), float(a["terminal_growth"])
        mult = float(a["exit_multiple_ebitda"])
        tax = float(a.get("tax_rate", 0.21))
    except (KeyError, TypeError, ValueError):
        return {"checked": False}
    if g >= w or mult <= 0:
        return {"checked": False}
    hard = float(a.get("terminal_gate_ratio_max", 3.0))
    cap = float(a.get("terminal_ebit_to_ebitda_max", DEFAULT_EBIT_TO_EBITDA_MAX))
    best_multiple = (1.0 - tax) * cap * (1.0 + g) / (w - g)
    best_ratio = mult / best_multiple
    return {
        "checked": True, "feasible": best_ratio <= hard,
        "best_case_ratio": best_ratio, "gate_ratio_max": hard,
        "best_case_gordon_multiple": best_multiple,
        "wacc": w, "terminal_growth": g, "exit_multiple": mult,
        "tax_rate": tax, "ebit_to_ebitda_max": cap,
        "max_exit_multiple": hard * best_multiple,
    }


def terminal_feasibility_finding(mandate: dict) -> dict | None:
    """A mandate-owned finding when the gate cannot be met, else None."""
    r = terminal_feasibility(mandate)
    if not r.get("checked") or r["feasible"]:
        return None
    f = _finding(
        "terminal_reconciliation", "terminal:mandate-infeasible",
        f"the mandate cannot pass the terminal gate: exit multiple "
        f"{r['exit_multiple']:.1f}x with WACC {r['wacc']:.2%} and terminal growth "
        f"{r['terminal_growth']:.2%} gives a terminal-value ratio of at least "
        f"{r['best_case_ratio']:.2f} even at EBIT/EBITDA {r['ebit_to_ebitda_max']:.0%}, "
        f"above the gate limit {r['gate_ratio_max']:.1f}x. Under this WACC and growth "
        f"the exit multiple can be at most {r['max_exit_multiple']:.1f}x",
        ["mandate.json"],
        required_evidence="a mandate whose exit multiple, WACC and terminal growth "
                          "can reconcile within the gate limit")
    f["owner"] = "mandate"
    f["metrics"] = r
    return f


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
    # A short or empty glide path silently passes every per-year bound check
    # below, so its length is checked against the horizon explicitly.
    if not isinstance(glide, list) or len(glide) != horizon:
        findings.append(_finding(
            "forecast_bounds", "margin-glide:horizon",
            f"margin_glide.gross has {len(glide) if isinstance(glide, list) else 'no'} "
            f"values but the mandate horizon is {horizon} years",
            ["forecast_assumptions.json"],
            required_evidence=f"exactly {horizon} gross-margin values",
        ))
        glide = glide if isinstance(glide, list) else []
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
        if not isinstance(rep, dict):
            findings.append(_finding(
                "candidate_manifest_integrity", "malformed-entry",
                f"candidate-manifest reports entry is not an object: {rep!r}",
                ["candidate-manifest.json"],
                required_evidence="each reports entry as {artifact, sha256, ...}",
            ))
            continue
        rel = rep.get("artifact", "")
        recorded = rep.get("sha256")
        if not isinstance(recorded, str) or not recorded:
            findings.append(_finding(
                "candidate_manifest_integrity", f"missing-sha256:{rel}",
                f"candidate-manifest entry for {rel!r} records no sha256",
                ["candidate-manifest.json"],
                required_evidence="a sha256 for every listed candidate artifact",
            ))
            continue
        path = case_dir / rel
        if not path.exists():
            findings.append(_finding(
                "candidate_manifest_integrity", f"missing-artifact:{rel}",
                f"candidate artifact missing on disk: {rel}",
                [rel], required_evidence="the artifact listed in candidate-manifest.json",
            ))
            continue
        actual = statestore.sha256_file(path)
        if actual != recorded:
            findings.append(_finding(
                "candidate_manifest_integrity", f"hash-mismatch:{rel}",
                f"candidate artifact sha256 mismatch for {rel}: manifest records "
                f"{recorded[:12]}…, disk holds {actual[:12]}…",
                [rel], required_evidence="artifact bytes matching the recorded sha256",
            ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 7: report evidence-link rule
# ---------------------------------------------------------------------------

# The key may contain spaces: as-reported segment ids are
# `segments.Intel Foundry.revenue`, and a marker the regex cannot see makes
# every number on the line look unlinked.
EV_MARKER = re.compile(r"\[EV:([^\]]+)\]", re.IGNORECASE)
NUMERIC_TOKEN = re.compile(r"(?<![\w])\d[\d,]*(?:\.\d+)?")

# Labels that are not claims and therefore need no evidence link. Without
# these exemptions "10-Q" and "2026-10-30" tokenize as numbers and no report
# could ever satisfy the rule.
_EXEMPT_SPAN = re.compile(
    r"\b(?:10|8)-[QK]\b"        # SEC form names: 10-Q, 10-K, 8-K
    r"|\b\d{4}-\d{2}-\d{2}\b"  # ISO dates
    r"|\bFY\s?\d{2,4}\b"       # fiscal-year labels
    r"|\bQ[1-4]\b"              # quarter labels
    r"|\b(?:19|20)\d{2}\b"      # a bare year is a label, not a claim
    r"|'\d{2}\b"                # Q3'26 shorthand
    r"|\bItem\s+\d+[A-Z]?\b"    # SEC item references: Item 2, Item 1A
    # "the shares do not sum to 100%" is arithmetic on the cited values, not a
    # measurement of the company. Only this idiom is exempt: a bare "100%"
    # elsewhere is still a claim and still needs a marker.
    r"|\bsum(?:s|ming)?\s+to\s+100(?:\.0+)?\s*%"
    r"|\b(?:writer\s+prompt|conformance\s+schema)\s+\d+\.\d+(?:\.\d+)?\b"  # run metadata
    r"|^\s*\d+(?=[.)]\s)"        # markdown ordered-list marker: "1. **Risk**"
    # "year 1 is FY2026E" numbers a forecast column, it does not measure
    # the company. The column is cited by the value it carries, not by
    # its index, so the ordinal needs no marker of its own.
    r"|(?i:\b(?:year|yr|y)[\s-]*\d+\b)"
    # "9M FY2026" is a nine-month span label, the same kind of label as
    # "Q3" and "FY2026". The 9 is not a measurement of the company.
    r"|\b\d{1,2}M\b(?=\s+(?:FY|Q)\d)"
    # Identifiers are labels too. The provenance header block carries a
    # request id, a candidate id, a bundle id and a sha256; without these the
    # header the writer is required to emit fails the gate on its own metadata.
    r"|\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b"   # req-0676, cand-bb62, intc-2026-10
    r"|\b[0-9a-f]{16,}\b"                   # hex digests
)


# A marker's reach ends at a sentence period (followed by whitespace or end
# of line, so a decimal point is not a break). An earlier rule also broke at commas, colons and dashes; live notes
# state "rose to USD 6,697mm from USD 4,235mm, while ... [EV:a] [EV:b]", one
# key covering both periods, and that rule rejected every such sentence: four
# writer attempts in a row failed on 30-35 numbers each. A marker therefore
# cites its sentence (back to the previous marker group or sentence period);
# semicolon-joined claims cited together at the sentence end are covered too.
_CLAUSE_BREAK = re.compile(r"\.(?=\s|$)")


def _clause_start(line: str, pos: int, skip_spans: list[tuple[int, int]]) -> int:
    """Offset where the clause containing *pos* begins."""
    start = 0
    for m in _CLAUSE_BREAK.finditer(line, 0, pos):
        if any(s <= m.start() < e for s, e in skip_spans):
            continue
        start = m.end()
    return start


def _provenance_header_end(lines: list[str]) -> int:
    """1-based line number of the last line of the provenance header block.

    The header is the run of blockquote lines at the top of the document,
    before the first ``##`` section. Returns 0 when the report has no header.
    """
    end = 0
    for ln, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith(">"):
            end = ln
    return end


def _suggest_keys(key: str, evidence_index: dict, limit: int = 3) -> list[str]:
    """Index keys that plausibly mean the same thing as an unknown *key*.

    A writer that shortens ``dcf.terminal.gordon.terminal_value`` to
    ``dcf.terminal_value`` gets told the real keys, so the repair is one round
    instead of another guess.
    """
    tail = key.rsplit(".", 1)[-1]
    hits = [k for k in evidence_index if k == tail or k.endswith("." + tail)]
    if not hits:
        hits = [k for k in evidence_index if tail in k]
    return sorted(hits)[:limit]


def _evidence_key(key: str, evidence_index: dict) -> str | None:
    """Resolve a marker key against the index, tolerating locator decoration.

    The index is keyed by canonical field id. A writer may decorate a marker
    with the document/slice it came from and the period it covers
    (``doc/IS:income_statement.revenue:2026-Q3``); that still names a real
    evidence key, so strip the decoration and look the field up.

    A writer may also prefix the key with the artifact it lives in
    (``dcf.sensitivity.grid`` for ``sensitivity.grid``). The leaf path is the
    part that identifies the evidence, so a suffix match resolves it. Without
    this the writer was told "the index has sensitivity.grid" and could only
    fix it by guessing the prefix, which it guessed wrong every round.
    """
    if key in evidence_index:
        return key
    for part in re.split(r"[:/]", key):
        if part in evidence_index:
            return part
    parts = key.split(".")
    for i in range(1, len(parts)):
        suffix = ".".join(parts[i:])
        if suffix in evidence_index:
            return suffix
    return None


def _num_matches(num: float, decimals: int, value: float) -> bool:
    """Does the stated ``num`` (shown to ``decimals`` places) state ``value``?

    Notes state a figure as the ledger holds it, as a percentage of a ratio,
    or in thousands/millions of the ledger unit; rounding to the shown
    decimals is allowed.
    """
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    return any(abs(num - c) <= tol * max(1.0, 1.0)
               for c in (value, value * 100.0, value / 1e3, value / 1e6,
                         value / 1e9))


_KEY_STOPWORDS = frozenset({
    "dcf", "base", "derived", "years", "segments", "assumptions", "forecast",
    "income", "statement", "balance", "sheet", "cash", "flow", "comps",
    "prior", "total", "value", "per", "and", "the", "of"})


def _token_number(tok: str) -> tuple[float, int] | None:
    try:
        text = tok.replace(",", "")
        return float(text), (len(text.split(".")[1]) if "." in text else 0)
    except ValueError:
        return None


def gate_report_evidence_links(report_md: str,
                               evidence_index: dict | None = None,
                               evidence_values: dict | None = None
                               ) -> tuple[bool, list[dict]]:
    """Every numeric token in report.md carries an evidence reference.

    Rule (deterministic): each standalone numeric token (not glued to word
    characters, e.g. Q3/FY26 are exempt) must be followed by an [EV:<key>]
    marker on the same line before the next numeric token. When an
    *evidence_index* is supplied, the marker key must exist in it.

    With *evidence_values* (key -> the numbers that key holds) a stated number
    must also be one of the values of the keys its sentence cites, when some
    other key does hold it: the writer then has a better key to cite. A number
    no key holds is a derived figure and is left to the critics.
    """
    findings: list[dict] = []
    if not report_md:
        return True, findings
    # a container key ("comps") holds every number beneath it, so it would
    # "match" almost anything: only leaf keys are offered as the better cite
    leaf_keys: set[str] = set()
    if evidence_values and evidence_index is not None:
        parents = {k.rsplit(".", 1)[0] for k in evidence_values if "." in k}
        leaf_keys = {k for k in evidence_values if k not in parents}
    lines = report_md.splitlines()
    header_end = _provenance_header_end(lines)
    for ln, line in enumerate(lines, start=1):
        if ln <= header_end:
            # The provenance header is metadata the writer is required to emit
            # (cutoff, bundle, candidate, request, card versions). Its numbers
            # identify the run, they do not assert anything about the company,
            # so there is nothing for an evidence marker to attach to.
            continue
        markers = [(m.start(), m.end(), m.group(1).strip())
                   for m in EV_MARKER.finditer(line)]
        marker_spans = [(s, e) for s, e, _ in markers]
        exempt_spans = [(m.start(), m.end()) for m in _EXEMPT_SPAN.finditer(line)]
        tokens = [
            (m.start(), m.end(), m.group(0))
            for m in NUMERIC_TOKEN.finditer(line)
            if not any(s <= m.start() < e for s, e in marker_spans)  # inside [EV:..]
            and not any(s <= m.start() < e for s, e in exempt_spans)  # form/date label
        ]
        if not tokens:
            # A line with no claims has nothing to link. A marker here is prose
            # -- writers quote the rule itself -- not a citation, so it cannot
            # be an unknown key.
            continue
        if evidence_index is not None:
            for _, _, key in markers:
                if _evidence_key(key, evidence_index) is None:
                    near = _suggest_keys(key, evidence_index)
                    hint = (f"; the index has {', '.join(near)}" if near else "")
                    findings.append(_finding(
                        "report_evidence_links", f"unknown-ev:{key}",
                        f'evidence marker on line {ln} names "{key}", which is not in '
                        f"the candidate evidence index{hint}",
                        [REPORT_ARTIFACT],
                        required_evidence=(
                            f"one of {', '.join(near)}" if near else
                            "a marker key present in the candidate evidence index"),
                    ))
        # A marker cites the sentence it closes: every number from the last
        # sentence break to the marker. Requiring a marker after *each* number
        # made a cited range ("10.0x to 16.0x implies 27.97 to 44.31 USD
        # [EV:comps.own_band]") impossible to write, and a band cannot be
        # cited any other way. The break keeps a marker from reaching back
        # into a previous sentence's claim.
        marker_starts = [s for s, _, _ in markers]
        covered: set[int] = set()
        cited: dict[int, set[str]] = {}
        for (ms, _me, mkey) in markers:
            # a thousands separator is not a clause break
            start = _clause_start(line, ms, marker_spans + exempt_spans
                                  + [(ts, te) for ts, te, _ in tokens])
            for i, (ts, _, _) in enumerate(tokens):
                if start <= ts < ms:
                    covered.add(i)
                    cited.setdefault(i, set()).add(mkey)
        if evidence_values and evidence_index is not None:
            for i in sorted(covered):
                ts, te, tok = tokens[i]
                parsed = _token_number(tok)
                if parsed is None or ("." not in tok and abs(parsed[0]) < 100):
                    continue    # counts and small integers are not figures
                num, dec = parsed
                keys = {_evidence_key(k, evidence_index) or k for k in cited[i]}
                held = [v for k in keys for v in evidence_values.get(k, ())]
                if not held:
                    continue    # the cited keys hold no number to compare with
                if any(_num_matches(num, dec, v) for v in held):
                    continue
                words = set(re.findall(r"[a-z]+", line.lower()))
                scored = []
                for k, vals in evidence_values.items():
                    if k not in leaf_keys or k in keys:
                        continue
                    if not any(_num_matches(num, dec, v) for v in vals):
                        continue
                    toks = {t for t in re.split(r"[._]", k.lower())
                            if t.isalpha() and t not in _KEY_STOPWORDS}
                    score = len(toks & words)
                    if score:       # the sentence is about what that key holds
                        scored.append((-score, len(k), k))
                better = [k for _s, _l, k in sorted(scored)]
                if not better:
                    continue
                findings.append(_finding(
                    "report_evidence_links", f"value-mismatch:{ln}:{ts}:{tok}",
                    f'numeric token "{tok}" on line {ln} is cited to '
                    f'{", ".join("[EV:" + k + "]" for k in sorted(keys))}, none of '
                    f'which holds that value; [EV:{better[0]}] does'
                    + (f' (also {", ".join(better[1:3])})' if len(better) > 1 else ""),
                    [REPORT_ARTIFACT],
                    required_evidence=f'a marker for the key that holds "{tok}", '
                                      f'such as {better[0]}'))
        for i, (ts, te, tok) in enumerate(tokens):
            if i in covered:
                continue
            findings.append(_finding(
                # the column keeps two identical numbers on one line
                # from collapsing into a single dedup_key
                "report_evidence_links", f"unlinked-number:{ln}:{ts}:{tok}",
                f'numeric token "{tok}" on line {ln} lacks an evidence marker '
                f"in its sentence",
                [REPORT_ARTIFACT],
                required_evidence=f'an evidence marker for "{tok}"',
            ))
    return (not findings, findings)


# ---------------------------------------------------------------------------
# gate 8: controls conformance (header, pins, budgets, write scope)
# ---------------------------------------------------------------------------

# What the generator directory may hold: the six candidate artifacts and the
# lead's manifest. Anything else there is a write-scope breach.
CANDIDATE_DIR_FILES = frozenset({
    "ingest-report.json", "facts.json", "forecast_assumptions.json",
    "report.md", "model.json", "valuation.json", "candidate-manifest.json",
    "ingest-inputs.sha256",    # sidecar proving ingest reuse
    "discarded"})              # where superseded artifacts are quarantined
_CANDIDATE_PATH = re.compile(r"^rounds/round-\d{3}/generator/([^/]+)$")
_NOTE_CANDIDATE = re.compile(r"\*\*Candidate:\*\*\s*([^\s·]+)")
_NOTE_REQUEST = re.compile(r"\*\*Request:\*\*\s*([^\s·]+)")


def gate_controls_conformance(
        manifest: dict, report_md: str, input_manifest: dict, mandate: dict,
        controls: dict | None, case_dir: Path | str = ".") -> tuple[bool, list[dict]]:
    """Exact control properties that used to be critic checks (rubric v1.1).

    Header completeness (request ID, candidate ID, cutoff, input-manifest
    hash, prompt/engine versions), version pins and drift, per-role token
    budget and write scope by path. Failures are ``owner: "system"``: the
    writer cannot repair them, so they are not writer work. ``controls``
    carries what only the orchestrator knows (header fields of the JSON
    artifacts, expected prompt versions, pinned mapping version, token
    ledger); None means nothing was supplied to check and the gate passes.
    """
    if controls is None:
        return True, []
    gate = "controls_conformance"
    findings: list[dict] = []
    manifest = manifest or {}

    def fail(key: str, summary: str, affected: list[str], need: str) -> None:
        f = _finding(gate, key, summary, affected, required_evidence=need)
        f["owner"] = "system"
        findings.append(f)

    cutoff = mandate.get("cutoff_date")
    cand = manifest.get("candidate_id")
    # -- header: manifest
    if not cand:
        fail("header:candidate-manifest.json:candidate_id",
             "candidate-manifest.json carries no candidate_id",
             ["candidate-manifest.json"], "candidate_id in the manifest header")
    if not manifest.get("request_id"):
        fail("header:candidate-manifest.json:request_id",
             "candidate-manifest.json carries no request_id",
             ["candidate-manifest.json"], "request_id in the manifest header")
    if manifest.get("cutoff_date") != cutoff:
        fail("header:candidate-manifest.json:cutoff_date",
             f"manifest cutoff_date {manifest.get('cutoff_date')!r} != mandate "
             f"cutoff {cutoff!r}", ["candidate-manifest.json"],
             "manifest cutoff_date equal to the mandate cutoff")
    want_sha = statestore.sha256_json(input_manifest)
    if manifest.get("input_manifest_sha256") != want_sha:
        fail("header:candidate-manifest.json:input_manifest_sha256",
             "manifest input_manifest_sha256 does not match the frozen input manifest",
             ["candidate-manifest.json"], "input_manifest_sha256 of the frozen manifest")
    for key in ("prompt_versions", "engine_versions"):
        if not isinstance(manifest.get(key), dict) or not manifest[key]:
            fail(f"header:candidate-manifest.json:{key}",
                 f"candidate-manifest.json carries no {key}",
                 ["candidate-manifest.json"], f"{key} in the manifest header")
    # -- header: JSON artifacts
    for name, hdr in sorted((controls.get("artifact_headers") or {}).items()):
        hdr = hdr or {}
        # the engines are deterministic code, not a dispatch: no request id
        if name not in ("model.json", "valuation.json") and \
                not hdr.get("request_id"):
            fail(f"header:{name}:request_id", f"{name} carries no request_id",
                 [name], "request_id in the artifact header")
        if cand and hdr.get("candidate_id") != cand:
            fail(f"header:{name}:candidate_id",
                 f"{name} candidate_id {hdr.get('candidate_id')!r} != manifest {cand!r}",
                 [name], "candidate_id equal to the manifest's")
        prov = hdr.get("provenance") or {}
        if prov.get("cutoff_date") != cutoff:
            fail(f"header:{name}:cutoff_date",
                 f"{name} provenance cutoff {prov.get('cutoff_date')!r} != "
                 f"mandate cutoff {cutoff!r}", [name],
                 "provenance.cutoff_date equal to the mandate cutoff")
        if prov.get("input_manifest_sha256") != want_sha:
            fail(f"header:{name}:input_manifest_sha256",
                 f"{name} provenance does not reference the frozen input manifest",
                 [name], "provenance.input_manifest_sha256 of the frozen manifest")
    # -- header: note
    m_c = _NOTE_CANDIDATE.search(report_md or "")
    m_r = _NOTE_REQUEST.search(report_md or "")
    if not m_c or (cand and m_c.group(1) != cand):
        fail("header:report.md:candidate_id",
             "report.md header lacks the manifest's candidate_id",
             ["report.md"], "**Candidate:** <candidate_id> in the note header")
    if not m_r:
        fail("header:report.md:request_id", "report.md header lacks a request id",
             ["report.md"], "**Request:** <request_id> in the note header")
    # -- version pins and drift
    expected = controls.get("expected_prompt_versions") or {}
    got = manifest.get("prompt_versions") or {}
    for role, ver in sorted(expected.items()):
        if got.get(role) != ver:
            fail(f"pin:prompt:{role}",
                 f"prompt version drift for {role}: manifest {got.get(role)!r}, "
                 f"card on disk {ver!r}", ["candidate-manifest.json"],
                 "manifest prompt_versions equal to the pinned cards")
    pinned_map = controls.get("pinned_mapping_version")
    if pinned_map is not None:
        seen = (manifest.get("engine_versions") or {}).get("mapping")
        if seen != pinned_map:
            fail("pin:mapping", f"mapping version {seen!r} != pinned {pinned_map!r}",
                 ["candidate-manifest.json"], "mapping version equal to the case pin")
    # -- budgets
    cap = controls.get("role_tokens_max")
    if isinstance(cap, (int, float)):
        for role, used in sorted((controls.get("tokens_by_role") or {}).items()):
            if isinstance(used, (int, float)) and used > cap:
                fail(f"budget:tokens:{role}",
                     f"role {role} used {int(used)} tokens, cap {int(cap)}",
                     ["candidate-manifest.json"], "role token usage within the cap")
    # -- write scope by path
    cand_dirs: set[str] = set()
    for rep in manifest.get("reports") or []:
        rel = rep.get("artifact", "") if isinstance(rep, dict) else ""
        m = _CANDIDATE_PATH.match(rel)
        if not m or m.group(1) not in CANDIDATE_DIR_FILES:
            fail(f"scope:{rel}", f"candidate artifact outside the generator write "
                 f"scope: {rel!r}", [rel or "candidate-manifest.json"],
                 "candidate artifacts only under rounds/round-NNN/generator/")
        else:
            cand_dirs.add(rel.rsplit("/", 1)[0])
    for d in sorted(cand_dirs):
        path = Path(case_dir) / d
        if path.is_dir():
            for entry in sorted(path.iterdir()):
                if entry.name not in CANDIDATE_DIR_FILES:
                    fail(f"scope:{d}/{entry.name}",
                         f"unexpected file in the generator directory: "
                         f"{d}/{entry.name}", [f"{d}/{entry.name}"],
                         "only candidate artifacts in the generator directory")
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
    # The harness computes the evidence index from the verified artifacts. The
    # lead echoes it into the manifest, but an echo is not a source of truth:
    # a truncated echo made every marker in a correct report look unknown.
    evidence_index: dict | None = None
    # key -> the numbers that key holds, for the cited-value check; None = off
    evidence_values: dict | None = None
    # Trigger filing's period label as the extractor emits it ("2026-Q3", or
    # "FY2025" for an annual trigger); None disables the period_coverage gate.
    current_period: str | None = None
    # Control facts only the orchestrator holds (see gate_controls_conformance).
    # None = nothing supplied, the controls_conformance gate passes.
    controls: dict | None = None


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
        # period-end dates come from the input manifest, not the facts; carry
        # the submitted ones so the recomputed discounting is the same
        if (model_json or {}).get("period_ends"):
            if isinstance(model, dict):
                model["period_ends"] = dict(model_json["period_ends"])
            elif hasattr(model, "period_ends"):
                model.period_ends = dict(model_json["period_ends"])
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
        if self.value_fn is not None and valuation_json is None:
            # a missing valuation.json used to skip this whole block silently
            findings.append(_finding(
                "recompute", "valuation:missing",
                "no valuation.json submitted; the valuation engine output "
                "cannot be verified",
                ["valuation.json"],
                required_evidence="valuation.json produced by the valuation engine"))
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
                # per_diluted_share is what the engine emits; per_share is the
                # stub fallback. comps was never compared at all.
                for section, keys in (
                    ("dcf", ("equity_value", "per_diluted_share", "per_share",
                             "ev", "dcf_upside")),
                    ("comps", ("comps_price", "comps_upside", "per_share")),
                ):
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
        if gate == "terminal_reconciliation":
            return gate_terminal_reconciliation(ctx.valuation_json or {}, mandate)
        if gate == "period_coverage":
            return gate_period_coverage(ctx.facts, ctx.current_period or "")
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
                evidence_index=(ctx.evidence_index
                                if ctx.evidence_index is not None
                                else ctx.candidate_manifest.get("evidence_index")),
                evidence_values=ctx.evidence_values,
            )
        if gate == "controls_conformance":
            return gate_controls_conformance(
                ctx.candidate_manifest, ctx.report_md, ctx.input_manifest,
                mandate, ctx.controls, ctx.case_dir)
        raise ValueError(f"unknown gate: {gate}")

    def run_recompute_helper(self, ctx: CandidateContext) -> tuple[bool, list[dict]]:
        rep = self.run_recompute(ctx.facts, ctx.forecast, ctx.mandate,
                                 ctx.model_json, ctx.valuation_json)
        assert len(rep.results) == 1
        return rep.results[0].ok, rep.results[0].findings
