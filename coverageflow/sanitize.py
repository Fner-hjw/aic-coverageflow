"""Typed input cleaners: the one authoritative sanitising layer.

Every place the system accepts user input (HTTP API, CLI, launcher answers)
routes values through these functions. Each cleaner normalises (NFC, control /
zero-width / bidi characters removed, whitespace collapsed), enforces a length
cap and a format, and raises ``InputError`` with a field-specific message.
Text that can reach an LLM prompt or a published artifact is also screened by
the prompt-injection heuristic, which REJECTS (never silently strips).

The browser has a matching normaliser (ui/static/app.js) for feedback only;
nothing here trusts it.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class InputError(ValueError):
    """A rejected input; ``field`` names it, ``str(exc)`` is user-readable."""

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"{field}: {message}")


# ------------------------------------------------------------------ text core

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


INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [(name, re.compile(rx, re.I)) for name, rx in (
    ("instruction override",
     r"\b(ignore|forget|override|bypass)\b[^.]{0,40}\b(instructions?|prompts?|rules|guidelines)\b"),
    ("instruction override", r"\bdisregard\b"),
    ("new instructions", r"\b(new|updated|real|actual) instructions?\b"),
    ("system prompt reference", r"\b(system|developer)\s+(prompt|message|instructions?)\b"),
    ("role reassignment",
     r"\byou\s+are\s+(now|no longer)\b|\bact\s+as\s+(a|an|the)\b|\bpretend\s+(to\s+be|you)\b"),
    ("role marker", r"(^|\s)(system|assistant|user|human|developer|ai)\s*:"),
    ("markdown heading / fence", r"###|```|~~~"),
    ("chat template token",
     r"<\||\|>|\[/?INST\]|<<\s*SYS|<\s*/?\s*(system|assistant|user|instructions?)\b"),
    ("URL", r"\bhttps?://|\bwww\.|\b(ftp|file|data|javascript):[^\s]"),
)]


def injection_reason(s: str) -> str | None:
    for name, rx in INJECTION_PATTERNS:
        if rx.search(s):
            return name
    return None


def _str(field: str, v: Any, required: bool) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise InputError(field, "is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise InputError(field, "must be text")
    return normalize_text(str(v))


def text(field: str, v: Any, *, max_len: int = 200, charset: str | re.Pattern | None = None,
         required: bool = False, injection: bool = True, min_len: int = 0) -> str:
    """Clean free text. ``charset`` is a regex every character must match
    (e.g. ``r"[A-Za-z0-9 ]"``). Returns "" for an optional empty value."""
    s = _str(field, v, required)
    if s is None:
        return ""
    if len(s) < min_len:
        raise InputError(field, f"needs at least {min_len} characters")
    if len(s) > max_len:
        raise InputError(field, f"too long ({len(s)} characters, max {max_len})")
    if charset is not None:
        rx = re.compile(charset) if isinstance(charset, str) else charset
        bad = sorted({c for c in s if not rx.fullmatch(c)})
        if bad:
            raise InputError(field, "contains characters that are not allowed: "
                             + " ".join(repr(c) for c in bad[:5]))
    if injection:
        why = injection_reason(s)
        if why:
            raise InputError(field, f"rejected, looks like prompt injection ({why})")
    return s


_NOTE_BAD = re.compile(r"[<>`{}\\\[\]|#]")


def note(field: str, v: Any, *, max_len: int = 500, required: bool = False) -> str:
    """Free-text note / reason / comment: no markup characters, injection-screened.

    Newlines are collapsed to spaces (one paragraph)."""
    s = text(field, v, max_len=max_len, required=required)
    if _NOTE_BAD.search(s):
        raise InputError(field, "must not contain < > ` { } [ ] | # or backslashes")
    return s


def person_name(field: str, v: Any, *, required: bool = True) -> str:
    """Approver / author label: letters, digits, space and . ' _ - @, max 80."""
    return text(field, v, max_len=80, required=required,
                charset=r"[\w .'@-]") or ""


# ---------------------------------------------------------------- identifiers

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def identifier(field: str, v: Any, *, required: bool = True, max_len: int = 80) -> str:
    """Slug for case_id, period label, bundle name, doc_id, job_id, request_id.

    Letters, digits, '.', '_', '-'; starts alphanumeric; never contains a path
    separator or '..', so it is safe as one path component."""
    s = _str(field, v, required)
    if s is None:
        return ""
    if len(s) > max_len or not _ID.fullmatch(s) or ".." in s:
        raise InputError(field, "must be letters, digits, '.', '_' or '-' "
                         f"(start with a letter or digit, max {max_len}, no '..')")
    return s


def filename(field: str, v: Any, *, required: bool = True) -> str:
    """A bare file name (no directory part)."""
    return identifier(field, v, required=required, max_len=120)


def ticker(field: str, v: Any, *, required: bool = True, letters_only: bool = False) -> str:
    s = _str(field, v, required)
    if s is None:
        return ""
    s = s.upper()
    rx = r"[A-Z]{1,6}" if letters_only else r"[A-Z0-9.-]{1,10}"
    if not re.fullmatch(rx, s):
        raise InputError(field, "must be a ticker"
                         + (" (1-6 letters, e.g. INTC)" if letters_only
                            else " (1-10 of A-Z 0-9 . -, e.g. BRK.B)"))
    return s


def tickers(field: str, v: Any, *, max_items: int = 20, letters_only: bool = False) -> list[str]:
    """Comma/space separated string or list of tickers; de-duplicated."""
    if v is None or v == "":
        return []
    items = re.split(r"[,\s]+", v) if isinstance(v, str) else v
    if not isinstance(items, list):
        raise InputError(field, "must be a list of tickers")
    out = [ticker(f"{field}[{i}]", x, letters_only=letters_only)
           for i, x in enumerate(items) if str(x).strip()]
    if len(out) > max_items:
        raise InputError(field, f"at most {max_items} tickers")
    return list(dict.fromkeys(out))


def cik(field: str, v: Any, *, required: bool = True, allow_unknown: bool = False) -> str:
    """Digits only, zero-padded to 10."""
    s = _str(field, v, required)
    if s is None:
        return ""
    if allow_unknown and s.lower() == "unknown":
        return "unknown"
    s = s.upper().removeprefix("CIK").strip()
    if not re.fullmatch(r"\d{1,10}", s):
        raise InputError(field, "must be digits only (up to 10), e.g. 0000050863")
    return s.zfill(10)


def iso_date(field: str, v: Any, *, required: bool = True) -> str:
    s = _str(field, v, required)
    if s is None:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise InputError(field, "must be a date YYYY-MM-DD")
    try:
        _dt.date.fromisoformat(s)
    except ValueError:
        raise InputError(field, f"{s} is not a real calendar date") from None
    return s


def choice(field: str, v: Any, allowed: Any, *, required: bool = True) -> str:
    s = _str(field, v, required)
    if s is None:
        return ""
    if s not in allowed:
        raise InputError(field, f"must be one of {', '.join(map(str, allowed))}")
    return s


def api_url(field: str, v: Any, *, required: bool = False) -> str:
    """http/https URL without credentials, query or fragment."""
    s = _str(field, v, required)
    if s is None:
        return ""
    if len(s) > 300:
        raise InputError(field, "URL too long")
    p = urlsplit(s)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise InputError(field, "must be an http:// or https:// URL")
    if p.username or p.password or p.query or p.fragment or re.search(r"[\s<>\"'`{}\\]", s):
        raise InputError(field, "must not contain credentials, query, fragment or special characters")
    return s


def secret(field: str, v: Any, *, required: bool = False) -> str:
    """API key: printable ASCII, no whitespace, max 300. Never injection-screened
    (it is not sent to a prompt) and never echoed in errors."""
    if v is None or v == "":
        if required:
            raise InputError(field, "is required")
        return ""
    if not isinstance(v, str):
        raise InputError(field, "must be text")
    s = v.strip()
    if len(s) > 300 or not re.fullmatch(r"[\x21-\x7e]+", s):
        raise InputError(field, "must be printable characters without spaces (max 300)")
    return s


# -------------------------------------------------------------------- numbers

_MINUS = str.maketrans({"−": "-", "–": "-", "—": "-"})


def number(field: str, v: Any, *, kind: str = "plain", lo: float | None = None,
           hi: float | None = None, required: bool = True) -> float | None:
    """Parse a number. kind: plain | rate | multiple.

    rate: "9.5%" -> 0.095; a bare number is a fraction when |x| <= 1, a
    percentage when 1 < |x| <= 100 (documented convention, shown in the form
    placeholders); anything beyond is rejected. multiple: "14x"/"14×" -> 14.
    Thousands commas allowed ("1,234.5"). JSON numbers pass through as-is."""
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise InputError(field, "is required")
        return None
    if isinstance(v, bool):
        raise InputError(field, "must be a number")
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = normalize_text(str(v)).translate(_MINUS).replace(" ", "")
        pct = s.endswith("%")
        mul = kind == "multiple" and s[-1:] in ("x", "X", "×")
        if pct and kind != "rate":
            raise InputError(field, "a percentage sign is only accepted for rates")
        if mul:
            s = s[:-1]
        elif pct:
            s = s[:-1]
        if not re.fullmatch(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?|-?\.\d+", s):
            raise InputError(field, f"{str(v)[:30]!r} is not a number")
        x = float(s.replace(",", ""))
        if kind == "rate" and (pct or 1 < abs(x) <= 100):
            x = x / 100.0
    if not math.isfinite(x):
        raise InputError(field, "must be a finite number")
    if kind == "rate" and abs(x) > 1:
        raise InputError(field, "rate too large (use a fraction like 0.095 or a percentage up to 100%)")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise InputError(field, f"{x:g} outside the allowed range {lo:g} to {hi:g}"
                         if lo is not None and hi is not None else f"{x:g} out of range")
    return x


# ---------------------------------------------------------------------- paths

def confined_path(field: str, v: Any, root: Path | str, *, must_exist: bool = False,
                  allow_outside: bool = False) -> Path:
    """Resolve ``v`` (relative paths are taken against ``root``) and require it
    to stay inside ``root`` (symlinks resolved). ``allow_outside`` is for
    explicitly user-chosen external files (e.g. --mandate): it still normalises
    and rejects control characters and NUL bytes."""
    if not isinstance(v, (str, Path)) or not str(v).strip():
        raise InputError(field, "is required")
    raw = str(v)
    if any(ord(c) < 32 for c in raw) or len(raw) > 1024:
        raise InputError(field, "path contains control characters or is too long")
    root_r = Path(root).resolve()
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else root_r / p).resolve()
    if not allow_outside and p != root_r and root_r not in p.parents:
        raise InputError(field, f"must be inside {root_r}")
    if must_exist and not p.exists():
        raise InputError(field, f"not found: {p}")
    return p


def safe_relative(field: str, v: Any, *, max_parts: int = 8) -> str:
    """A relative path made of safe components (artifact paths): no '..',
    no absolute path, no empty or hidden-dot components, no backslashes."""
    s = _str(field, v, True)
    if "\\" in s or s.startswith("/") or len(s) > 300:
        raise InputError(field, "must be a relative path")
    parts = s.split("/")
    if len(parts) > max_parts or any(
            not p or p in (".", "..") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,119}", p)
            for p in parts):
        raise InputError(field, "contains an unsafe path component")
    return "/".join(parts)


def collect(fn, *args, **kw) -> tuple[Any, str | None]:
    """Run one cleaner; return (value, None) or (None, message)."""
    try:
        return fn(*args, **kw), None
    except InputError as exc:
        return None, str(exc)


def clean_json(obj: Any, field: str = "", *, injection: bool = True, max_len: int = 2000) -> Any:
    """Recursively normalise every string in a loaded JSON document (keys too)
    and reject injection-like text. For user-supplied files (evolve --spec)."""
    if isinstance(obj, str):
        return text(field or "value", obj, max_len=max_len, injection=injection)
    if isinstance(obj, list):
        return [clean_json(v, f"{field}[{i}]", injection=injection, max_len=max_len)
                for i, v in enumerate(obj)]
    if isinstance(obj, dict):
        return {normalize_text(str(k)): clean_json(v, f"{field}.{k}" if field else str(k),
                                                    injection=injection, max_len=max_len)
                for k, v in obj.items()}
    return obj
