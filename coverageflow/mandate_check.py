"""One mandate validator: references/mandate-schema.json plus business rules.

``schema_errors(mandate)`` validates against the authoritative JSON Schema
(references/mandate-schema.json) with a small stdlib interpreter for the
draft 2020-12 subset the schema uses (jsonschema is not a dependency).
``semantic_errors(payload)`` holds the plausibility rules a schema cannot
express (wacc band, terminal growth below wacc, margin ordering, ...).
``validate_mandate_full`` runs both for a complete mandate.

``refuse_unaccepted`` is the gate that keeps an automatic prefill (status
"proposed") from being used for anything: a human must accept it first.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "references" / "mandate-schema.json"


@lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- validator

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool))
    or (isinstance(v, float) and v.is_integer()),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _check(value: Any, schema: Any, root: dict, path: str, out: list[str]) -> None:
    if schema is True or schema == {}:
        return
    if schema is False:
        out.append(f"{path}: not allowed")
        return
    if "$ref" in schema:
        _check(value, _resolve(schema["$ref"], root), root, path, out)
    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_TYPES[n](value) for n in names):
            out.append(f"{path}: expected {' or '.join(names)}, got "
                       f"{type(value).__name__ if value is not None else 'null'}")
            return
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: must be one of {schema['enum']}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} is above the maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.append(f"{path}: {value} must be above {schema['exclusiveMinimum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: must not be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: at most {schema['maxItems']} item(s)")
        prefix = schema.get("prefixItems") or []
        for i, sub in enumerate(prefix):
            if i < len(value):
                _check(value[i], sub, root, f"{path}[{i}]", out)
        if "items" in schema:
            for i, item in enumerate(value[len(prefix):], start=len(prefix)):
                _check(item, schema["items"], root, f"{path}[{i}]", out)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}.{key}: required" if path else f"{key}: required")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, root, f"{path}.{key}" if path else key, out)
        extra = schema.get("additionalProperties", True)
        if extra is not True:
            for key, item in value.items():
                if key not in props:
                    _check(item, extra, root, f"{path}.{key}" if path else key, out)
    for sub in schema.get("allOf", []):
        _check(value, sub, root, path, out)
    if "if" in schema:
        probe: list[str] = []
        _check(value, schema["if"], root, path, probe)
        branch = schema.get("then") if not probe else schema.get("else")
        if branch is not None:
            _check(value, branch, root, path, out)
    if "oneOf" in schema:
        hits = 0
        for sub in schema["oneOf"]:
            probe = []
            _check(value, sub, root, path, probe)
            hits += not probe
        if hits != 1:
            out.append(f"{path}: must match exactly one allowed shape")
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            probe = []
            _check(value, sub, root, path, probe)
            if not probe:
                break
        else:
            out.append(f"{path}: matches none of the allowed shapes")


def validate_against(value: Any, schema: dict) -> list[str]:
    out: list[str] = []
    _check(value, schema, schema, "", out)
    return out


def schema_errors(mandate: dict) -> list[str]:
    """Human-readable problems with a mandate's structure ([] = valid)."""
    return validate_against(mandate, load_schema())


# ------------------------------------------------------------ business rules

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def semantic_errors(payload: dict, require_trigger: bool = True) -> list[str]:
    """Business-rule checks (plausible bands, ordering); [] = ok.

    Shared by the new-case wizard (ui.service.validate_mandate) and
    ``case accept``. Structure and types are checked by schema_errors().
    """
    errors: list[str] = []
    ticker = str(payload.get("ticker") or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,6}", ticker):
        errors.append("ticker is required (1-6 letters, e.g. INTC)")
    cutoff = str(payload.get("cutoff_date") or "")
    if not _DATE_RE.match(cutoff):
        errors.append("cutoff_date must be a date YYYY-MM-DD")
    if require_trigger and not payload.get("trigger_document"):
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


def validate_mandate_full(mandate: dict, require_trigger: bool = False) -> list[str]:
    """Schema plus business rules for a complete mandate (case accept / new)."""
    errs = schema_errors(mandate)
    if errs:
        return errs
    return semantic_errors(mandate, require_trigger=require_trigger)


# ------------------------------------------------------------ acceptance gate

class UnacceptedMandateError(ValueError):
    """Raised when a proposed (unaccepted) prefill is used for a case."""


def is_unaccepted(mandate: dict | None) -> bool:
    return bool(mandate) and (mandate or {}).get("status") == "proposed"


def refuse_unaccepted(mandate: dict | None, what: str = "use") -> None:
    """Nothing runs on a proposed mandate: a human must accept it first."""
    if is_unaccepted(mandate):
        raise UnacceptedMandateError(
            f"refusing to {what}: this mandate is a machine proposal "
            "(status: proposed). A human must review it and run "
            "`case accept <draft>` (or submit the reviewed form in the UI) first.")
