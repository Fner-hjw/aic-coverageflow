"""Authoritative sanitiser for human-supplied mandate values.

Everything a person types into the new-case form (or passes to
``case accept --set``) can end up inside an LLM prompt, so this layer never
trusts the client. ``sanitize_mandate`` (1) normalises every string (NFC,
control / zero-width / bidi characters removed, whitespace collapsed),
(2) REJECTS strings that look like prompt injection (it does not silently
strip them), (3) applies the cross-field numeric rules and (4) for a full
mandate validates against references/mandate-schema.json, whose patterns and
ranges are the per-field allow-list.

Returns ``(clean, errors)``; ``errors`` are field-specific ([] = ok).
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

from . import mandate_check

# Machine-written or reviewer-owned blocks: normalised but not injection-checked
# (they hold fetched provenance text, URLs and approval records, not human input).
_MACHINE_KEYS = frozenset({"provenance", "warnings", "missing", "approval",
                           "proposed_at", "prefill"})

_INJECTION: list[tuple[str, re.Pattern]] = [(name, re.compile(rx, re.I)) for name, rx in (
    ("instruction override",
     r"\b(ignore|forget|override|bypass)\b[^.]{0,40}\b(instructions?|prompts?|rules|guidelines)\b"),
    ("instruction override", r"\bdisregard\b"),
    ("new instructions", r"\b(new|updated|real|actual) instructions?\b"),
    ("system prompt reference", r"\b(system|developer)\s+(prompt|message|instructions?)\b"),
    ("role reassignment", r"\byou\s+are\s+(now|no longer)\b|\bact\s+as\s+(a|an|the)\b|\bpretend\s+(to\s+be|you)\b"),
    ("role marker", r"(^|[\s\n])(system|assistant|user|human|developer|ai)\s*:"),
    ("markdown heading / fence", r"###|```|~~~"),
    ("chat template token", r"<\||\|>|\[/?INST\]|<<\s*SYS|<\s*/?\s*(system|assistant|user|instructions?)\b"),
    ("URL", r"\bhttps?://|\bwww\.|\b(ftp|file|data|javascript):[^\s]"),
)]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_TICKER_FIELDS_NOTE = "ticker"


def normalize_text(s: str) -> str:
    """NFC, drop control/format (zero-width, bidi) chars, collapse whitespace."""
    s = unicodedata.normalize("NFC", s)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch in "\t\n\r\v\f" or cat in ("Zs", "Zl", "Zp"):
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return re.sub(r" +", " ", "".join(out)).strip()


def injection_reason(s: str) -> str | None:
    for name, rx in _INJECTION:
        if rx.search(s):
            return name
    return None


def _clean(node: Any, path: str, errors: list[str], check: bool) -> Any:
    if isinstance(node, str):
        s = normalize_text(node)
        if check:
            why = injection_reason(s)
            if why:
                errors.append(f"{path}: rejected, looks like prompt injection ({why})")
        return s
    if isinstance(node, list):
        return [_clean(v, f"{path}[{i}]", errors, check) for i, v in enumerate(node)]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            sub = f"{path}.{k}" if path else str(k)
            skip = not check or (not path and k in _MACHINE_KEYS)
            nk = k
            if isinstance(k, str):
                nk = normalize_text(k)
                if not skip:
                    why = injection_reason(nk)
                    if why:
                        errors.append(f"{sub}: name rejected, looks like prompt injection ({why})")
            if nk in out and nk != k:
                errors.append(f"{sub}: duplicates another name after cleaning")
            out[nk] = _clean(v, sub, errors, not skip)
        return out
    return node


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _cross_rules(m: dict, errors: list[str]) -> None:
    rp = m.get("report_policy") or {}
    w = rp.get("length_words")
    if isinstance(w, list) and len(w) == 2 and None not in (_num(w[0]), _num(w[1])):
        if not 100 <= w[0] <= 10000 or not 100 <= w[1] <= 10000:
            errors.append("report_policy.length_words: words must be 100-10000")
        if w[0] >= w[1]:
            errors.append("report_policy.length_words: min words must be below max words")
    a = m.get("assumptions") or {}
    wacc, tg = _num(a.get("wacc")), _num(a.get("terminal_growth"))
    if wacc is not None and not 0.01 <= wacc <= 0.30:
        errors.append(f"assumptions.wacc: {wacc:g} outside 0.01-0.30")
    if tg is not None and not -0.02 <= tg <= 0.06:
        errors.append(f"assumptions.terminal_growth: {tg:g} outside -0.02 to 0.06")
    if wacc is not None and tg is not None and tg >= wacc:
        errors.append(f"assumptions.terminal_growth: {tg:g} must be below wacc {wacc:g}")
    tax = _num(a.get("tax_rate"))
    if tax is not None and not 0 <= tax <= 0.5:
        errors.append(f"assumptions.tax_rate: {tax:g} outside 0-0.5")
    mult = _num(a.get("exit_multiple_ebitda"))
    if mult is not None and not 0 < mult <= 200:
        errors.append(f"assumptions.exit_multiple_ebitda: {mult:g} outside 0-200")
    price = _num((a.get("market") or {}).get("price"))
    if price is not None and price <= 0:
        errors.append("assumptions.market.price: must be above 0")
    fb = a.get("forecast_bounds") or {}
    lo, hi = _num(fb.get("margin_floor_gross")), _num(fb.get("margin_ceiling_gross"))
    for name, v in (("margin_floor_gross", lo), ("margin_ceiling_gross", hi)):
        if v is not None and not 0 <= v <= 1:
            errors.append(f"assumptions.forecast_bounds.{name}: {v:g} outside 0-1")
    if lo is not None and hi is not None and lo >= hi:
        errors.append("assumptions.forecast_bounds: margin floor must be below margin ceiling")
    for seg, b in (fb.get("segments") or {}).items():
        if isinstance(b, list) and len(b) == 2 and None not in (_num(b[0]), _num(b[1])) and b[0] >= b[1]:
            errors.append(f"assumptions.forecast_bounds.segments.{seg}: min must be below max")


def sanitize_mandate(payload: dict, validate: bool | None = None) -> tuple[dict, list[str]]:
    """Clean + check a mandate (or a new-case payload). Returns (clean, errors).

    ``validate`` runs the JSON Schema too; default: only when ``payload`` is a
    complete mandate (has mandate_id). A new-case payload is schema-checked by
    the caller once it has been assembled into a mandate.
    """
    if not isinstance(payload, dict):
        return payload, ["mandate must be an object"]
    errors: list[str] = []
    clean = _clean(copy.deepcopy(payload), "", errors, True)
    for key in ("ticker",):
        if isinstance(clean.get(key), str):
            clean[key] = clean[key].upper()
    peers = ((clean.get("assumptions") or {}).get("peer_block") or {}).get("peers")
    if isinstance(peers, list):
        for p in peers:
            if isinstance(p, dict) and isinstance(p.get("ticker"), str):
                p["ticker"] = p["ticker"].upper()
    for key in ("case_id", "period", "bundle_id", "trigger_document"):
        v = clean.get(key)
        if isinstance(v, str) and v and not _ID_RE.match(v):
            errors.append(f"{key}: {v!r} must be letters, digits, '.', '_' or '-' (max 80)")
    _cross_rules(clean, errors)
    if validate is None:
        validate = "mandate_id" in clean
    if validate and not errors:
        errors += mandate_check.schema_errors(clean)
    return clean, list(dict.fromkeys(errors))


def sanitize_or_raise(mandate: dict, validate: bool | None = None) -> dict:
    """Return the clean mandate or raise ValueError listing every problem."""
    clean, errors = sanitize_mandate(mandate, validate)
    if errors:
        raise ValueError("mandate rejected:\n  - " + "\n  - ".join(errors))
    return clean
