"""Fan-out extraction: unit planning, compact-row validation, deterministic merge.

The extractor used to be one call over every statement of every scoped
filing, emitting ~300 facts with a full citation object each (~100k chars of
output). Output generation dominated the stage (535-630 s measured). Now:

  * ``plan_units`` splits the scope into one unit per (document, statement
    slice kind), each a small call that sees only its own slice;
  * a unit answers with compact pipe rows
    ``field|period|value|unit|locator`` -- the document, slice and anchor are
    known to the harness, so code expands each row into the fact object the
    rest of the pipeline expects (``expand_unit``);
  * ``merge_units`` concatenates the units' facts into one facts.json and
    reconciles overlaps (equal duplicates collapse, a restated figure takes the
    newer filing's value and says so in ``notes``).

Pure functions only; the orchestrator owns dispatch, budgets and the trace.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

# slice kinds that become extraction units, in the order they are dispatched.
# The slicer calls the cash-flow slice ``cash_flow_statement``; the ingest
# card calls it ``cash_flow``. Both mean the same unit.
UNIT_KINDS = ("income_statement", "balance_sheet", "cash_flow", "segments")
_KIND_ALIASES = {"cash_flow_statement": "cash_flow"}

CANONICAL_UNITS = ("USDmm", "USD", "shares_mm", "ratio")
# YYYY-QN, YYYY-QN-YTD, or a fiscal year as FYYYYY (also accepted as YYYY and
# normalised to FYYYYY, the label the model and valuation engines use).
PERIOD_RE = re.compile(r"^(FY\d{4}|\d{4}(-Q[1-4](-YTD)?)?)$")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
_SEGMENT_FIELD_RE = re.compile(r"^segments\.[^|]+\.(revenue|operating_income)$")
_SEGMENT_RESIDUALS = ("segments.all_other_revenue",
                      "segments.all_other_operating_income")

MAX_REPORTED_ROW_ERRORS = 12
CONFIDENCE = 1.0      # rows carry no confidence; located rows are expanded as 1.0

_MODEL_SCHEMA = Path(__file__).resolve().parent.parent / "references" / "model-schema.json"


def normalize_kind(kind: Any) -> str:
    k = str(kind or "")
    return _KIND_ALIASES.get(k, k)


# --------------------------------------------------------------- field catalogue
def load_catalogue(path: Path | str | None = None) -> dict:
    """Field ids per statement from references/model-schema.json.

    Returns ``{"fields": {statement: [{id, note?}, ...]}, "ids": set}``;
    segment fields are patterns (the as-reported segment name is free text), so
    they are listed as templates and matched by regex in ``field_known``.
    """
    schema = json.loads(Path(path or _MODEL_SCHEMA).read_text(encoding="utf-8"))
    statements = schema.get("statements") or {}
    fields: dict[str, list[dict]] = {}
    ids: set[str] = set()
    for name in ("income_statement", "balance_sheet", "cash_flow"):
        entries = []
        for f in (statements.get(name) or {}).get("fields") or []:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            entry = {"id": f["id"]}
            if f.get("note"):
                entry["note"] = f["note"]
            if f.get("sign_convention"):
                entry["sign"] = f["sign_convention"]
            entries.append(entry)
            ids.add(f["id"])
        fields[name] = entries
    seg = statements.get("segments") or {}
    suffixes = [s["suffix"] for s in seg.get("per_segment_fields") or []
                if s.get("suffix") in ("revenue", "operating_income")]
    residuals = [r["id"] for r in seg.get("residuals") or [] if r.get("id")]
    fields["segments"] = (
        [{"id": f"segments.<segment name as reported>.{s}"} for s in suffixes]
        + [{"id": r} for r in residuals])
    ids.update(residuals or _SEGMENT_RESIDUALS)
    return {"fields": fields, "ids": ids}


def field_known(field: str, catalogue: dict) -> bool:
    return field in catalogue["ids"] or bool(_SEGMENT_FIELD_RE.match(field))


def field_guide(kind: str, catalogue: dict) -> dict:
    """The field list the unit's card rules apply to: ``{"fields": [ids],
    "notes": {id: rule}}``."""
    entries = catalogue["fields"].get(normalize_kind(kind)) or []
    notes = {}
    for e in entries:
        text = " ".join(x for x in (e.get("note"), e.get("sign")) if x)
        if text:
            notes[e["id"]] = text
    return {"fields": [e["id"] for e in entries], "notes": notes}


# ------------------------------------------------------------------ unit planning
def unit_name(doc_id: str, kind: str) -> str:
    return f"{doc_id}/{normalize_kind(kind)}"


def plan_units(scope: list[dict], slices: list[dict]) -> list[dict]:
    """One unit per (scoped document, statement slice kind) that has a slice.

    Order: scope order (trigger first, annual anchor second), then
    income statement, balance sheet, cash flow, segments. Slices of other
    kinds (MD&A, full text) and slices of documents outside the scope are
    skipped. A kind a document has no slice for yields no unit; if a document
    has several slices of one kind the first declared one is used.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for sl in slices or []:
        if not isinstance(sl, dict):
            continue
        kind = normalize_kind(sl.get("kind"))
        doc_id = str(sl.get("slice_id") or "").split("/")[0]
        if kind in UNIT_KINDS and doc_id:
            by_key.setdefault((doc_id, kind), sl)
    units: list[dict] = []
    for doc in scope or []:
        doc_id = doc.get("doc_id")
        for kind in UNIT_KINDS:
            sl = by_key.get((doc_id, kind))
            if sl is None:
                continue
            units.append({
                "unit": unit_name(doc_id, kind),
                "doc_id": doc_id,
                "kind": kind,
                "slice_id": sl.get("slice_id"),
                "form": doc.get("form"),
                "filed": doc.get("filed"),
                "scope_role": doc.get("role"),
                "slice": sl,
            })
    return units


# ----------------------------------------------------------------- row parsing
def parse_row(row: Any, catalogue: dict) -> tuple[dict | None, str | None]:
    """Parse ``field|period|value|unit|locator`` -> (parsed, error)."""
    if not isinstance(row, str):
        return None, f"not a string (got {type(row).__name__})"
    parts = row.split("|", 4)        # the locator is last and may hold a pipe
    if len(parts) != 5:
        return None, ("expected 5 pipe-separated parts "
                      "field|period|value|unit|locator, got "
                      f"{len(parts)}")
    field, period, value, unit, locator = (p.strip() for p in parts)
    if not field_known(field, catalogue):
        return None, (f"unknown field {field!r}: use a canonical field id from "
                      "the request's field list (segment rows are "
                      "segments.<name as reported>.revenue|operating_income)")
    if not PERIOD_RE.match(period):
        return None, (f"period {period!r} is not canonical: use YYYY-QN, "
                      "YYYY-QN-YTD or FYYYYY")
    if re.fullmatch(r"\d{4}", period):
        period = f"FY{period}"
    if not _NUMBER_RE.match(value.replace(" ", "")):
        return None, (f"value {value!r} is not a plain number (no commas, "
                      "currency signs, parentheses or text; negatives use '-')")
    number = float(value.replace(" ", ""))
    if not math.isfinite(number):
        return None, f"value {value!r} is not finite"
    if unit not in CANONICAL_UNITS:
        return None, f"unit {unit!r} must be one of {list(CANONICAL_UNITS)}"
    if not locator:
        return None, "locator is empty: name the row/line it was read from"
    return {"field": field, "period": period, "value": number,
            "unit": unit, "locator": locator}, None


def _anchor_of(slice_entry: dict, kind: str) -> str:
    anchors = slice_entry.get("anchors") or {}
    return str(anchors.get("heading") or anchors.get("first_line") or kind)


def expand_unit(parsed: dict, unit: dict, catalogue: dict
                ) -> tuple[str | None, dict | None]:
    """Validate a unit submission and expand its rows into fact objects.

    Returns ``(error, None)`` -- one repairable message naming every bad row
    (up to a cap) -- or ``(None, {"facts", "missing", "notes"})``.
    """
    rows = parsed.get("rows")
    if not isinstance(rows, list):
        return "rows must be an array of 'field|period|value|unit|locator' strings", None
    errors: list[str] = []
    facts: list[dict] = []
    seen: dict[tuple[str, str], int] = {}
    anchor = _anchor_of(unit["slice"], unit["kind"])
    for i, row in enumerate(rows):
        got, err = parse_row(row, catalogue)
        if err:
            errors.append(f"rows[{i}] {str(row)[:90]!r}: {err}")
            continue
        key = (got["field"], got["period"])
        if key in seen:
            errors.append(f"rows[{i}] duplicates rows[{seen[key]}] "
                          f"({got['field']} {got['period']}): one row per "
                          "field+period")
            continue
        seen[key] = i
        facts.append({
            "field": got["field"], "period": got["period"],
            "value": got["value"], "unit": got["unit"],
            "confidence": CONFIDENCE,
            "citation": {"doc_id": unit["doc_id"], "slice_id": unit["slice_id"],
                         "anchor": anchor, "locator": got["locator"]},
        })
    missing, merr = _clean_missing(parsed.get("missing"))
    if merr:
        errors.append(merr)
    notes = parsed.get("notes")
    if notes is None:
        notes = []
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        errors.append("notes must be an array of strings")
        notes = []
    if not facts and not missing and not errors:
        errors.append("no rows and no missing entries: extract the slice's "
                      "line items or say why each is missing")
    if errors:
        shown = errors[:MAX_REPORTED_ROW_ERRORS]
        more = len(errors) - len(shown)
        tail = f" (+{more} more)" if more > 0 else ""
        return (f"unit {unit['unit']}: {len(errors)} invalid "
                f"entr{'y' if len(errors) == 1 else 'ies'}{tail}: "
                + "; ".join(shown)), None
    return None, {"facts": facts, "missing": missing, "notes": list(notes)}


def _clean_missing(raw: Any) -> tuple[list[dict], str | None]:
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], "missing must be an array of {field, reason} objects"
    out: list[dict] = []
    for i, m in enumerate(raw):
        if isinstance(m, str) and m.strip():
            m = {"field": m.strip(), "reason": "not reported in the slice"}
        if (not isinstance(m, dict) or not str(m.get("field") or "").strip()):
            return [], f"missing[{i}] needs a field (and a reason)"
        entry = {"field": str(m["field"]).strip(),
                 "reason": str(m.get("reason") or "not reported in the slice")}
        if m.get("period"):
            entry["period"] = str(m["period"])
        out.append(entry)
    return out, None


# ------------------------------------------------------------------------ merge
def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def merge_units(done: list[dict], *, request_id: str, candidate_id: str | None,
                prompt_version: str, conformance: str = "schema 1.0") -> dict:
    """Merge validated units into one facts.json artifact.

    *done* items (in dispatch order): ``{"unit": <plan unit>, "request_id",
    "facts", "missing", "notes"}``.

    Rules: facts concatenate in unit order. The same (field, period) from two
    units with equal values collapses to the first. With different values the
    more recently filed document wins and ``notes`` names both (a restated
    figure); within one document the first wins and the conflict is noted.
    A ``missing`` entry is dropped when another unit supplied that field (for
    that period when the entry names one); identical entries collapse.
    """
    facts: list[dict] = []
    index: dict[tuple[str, str], int] = {}
    origin: dict[tuple[str, str], dict] = {}
    notes: list[str] = []
    for item in done:
        u = item["unit"]
        for fact in item["facts"]:
            key = (fact["field"], fact["period"])
            if key not in index:
                index[key] = len(facts)
                origin[key] = u
                facts.append(fact)
                continue
            old = facts[index[key]]
            if _same(old["value"], fact["value"]):
                continue
            prev = origin[key]
            if prev["doc_id"] == u["doc_id"]:
                notes.append(
                    f"conflict within {u['doc_id']}: {fact['field']} "
                    f"{fact['period']} read as {old['value']:g} "
                    f"({prev['unit']}) and {fact['value']:g} ({u['unit']}); "
                    f"kept {old['value']:g}")
                continue
            newer = str(u.get("filed") or "") >= str(prev.get("filed") or "")
            keep, drop = (((fact, u), (old, prev)) if newer
                          else ((old, prev), (fact, u)))
            if newer:
                facts[index[key]] = fact
                origin[key] = u
            notes.append(
                f"restated: {fact['field']} {fact['period']} is "
                f"{keep[0]['value']:g} in {keep[1]['doc_id']} "
                f"(filed {keep[1].get('filed')}) vs {drop[0]['value']:g} in "
                f"{drop[1]['doc_id']} (filed {drop[1].get('filed')}); "
                f"kept the more recent filing")
    have_field = {f["field"] for f in facts}
    have_pair = set(index)
    missing: list[dict] = []
    seen_missing: set[tuple[str, str | None]] = set()
    for item in done:
        for m in item["missing"]:
            period = m.get("period")
            if (m["field"], period) in seen_missing:
                continue
            supplied = ((m["field"], period) in have_pair if period
                        else m["field"] in have_field)
            if supplied:
                continue
            seen_missing.add((m["field"], period))
            missing.append(m)
    for item in done:
        notes.extend(n for n in item["notes"] if n not in notes)
    return {
        "role": "extractor",
        "request_id": request_id,
        "candidate_id": candidate_id,
        "facts": facts,
        "missing": missing,
        "notes": notes,
        "conformance": conformance,
        "prompt_version": prompt_version,
        # which units/requests produced this file (rows are not repeated:
        # the facts carry them)
        "extraction_units": [{
            "unit": item["unit"]["unit"],
            "doc_id": item["unit"]["doc_id"],
            "slice_id": item["unit"]["slice_id"],
            "request_id": item["request_id"],
            "facts": len(item["facts"]),
        } for item in done],
    }
