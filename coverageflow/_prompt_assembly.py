"""Deterministic prompt assembly (private helper for dispatch.py).

Owner: stream A3. Per docs/design-decisions.md "agent assembly model", every
role call is:

    base harness preamble (shared)
  + role card (versioned, hashed)
  + pinned references (exactly what the role card manifest pins)
  + bound request payload
  -> one deterministic single-text prompt

Same inputs = byte-identical prompt = the same sha256, which is the backbone
of replay matching (replay_backends) and trace evidence. Note: creating a
prompt on a copy of this module with different section wording changes all
shas - treat the section-marker format as stable interface.

The module also exposes the default repo-root layout:
``<root>/prompts/*.md`` role cards, ``<root>/references/*.md`` pinned
references (``root`` defaults to the package's parent directory).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

PREAMBLE_MARK = "<CF-HARNESS-PREAMBLE>"
ROLE_CARD_MARK = "<CF-ROLE-CARD>"
REFERENCE_MARK = "<CF-REFERENCE>"
REQUEST_MARK = "<CF-BOUND-REQUEST>"

_HEADER_RE = re.compile(r"^--([a-z_]+):\s*(.*)$")
REFERENCE_SEPARATOR_RE = re.compile(r"[,\s]+")

CARD_HEADERS = ("version", "role", "produces", "read")
HARNESS_PREAMBLE_FILE = "harness-preamble.md"


class AssemblyError(RuntimeError):
    """Raised when prompt assembly cannot be completed deterministically."""


def write_text(path: Path, text: str) -> None:
    """Test/fixture helper: write a newline-normalized UTF-8 file."""
    Path(path).write_text(_normalize(text), encoding="utf-8")


def _normalize(text: str) -> str:
    """Normalize line endings to LF so file history never shifts the sha256."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read(path: Path) -> str:
    try:
        return _normalize(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise AssemblyError(f"cannot read {path}: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_sha(path: Path) -> str:
    """Sha256 of a file canonicalized to LF.

    Hashing the raw bytes would let a checkout's line-ending style fork the
    sha (and thus break replay matching) without any semantic change, so the
    normalized text is hashed instead.
    """
    return sha256_bytes(_normalize(Path(path).read_text(encoding="utf-8")).encode("utf-8"))


def parse_role_card(path: Path) -> dict:
    """Parse a role card's manifest header into a dict.

    Header format (design-decisions "prompt cards"):

        --version: 1.0
        --role: ingest
        --produces: ingest-report.json
        --read: mandate.json, new document
        --references: accounting-definitions.md

    ``--references`` lists pinned extra references (space or comma separated);
    the shared harness preamble is always included and never needs pinning.
    """
    text = _read(path)
    headers: dict[str, str] = {}
    in_header = True
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        m = _HEADER_RE.match(stripped)
        if m:
            if not in_header:
                # A header below the body used to be dropped without a word,
                # so a card could silently lose its --references pinning.
                raise AssemblyError(
                    f"role card {path} line {lineno}: header --{m.group(1)} "
                    "appears after the card body has started; every --header "
                    "must be in the block at the top of the file"
                )
            key, value = m.group(1), m.group(2).strip()
            if key not in headers:  # first occurrence wins
                headers[key] = value
            continue
        if stripped and headers.get("role"):
            # first non-header, non-empty line ends the header block
            in_header = False
    for required in CARD_HEADERS:
        if not headers.get(required):
            raise AssemblyError(
                f"role card {path} missing required --{required} header"
            )
    refs_raw = headers.get("references", "").strip()
    references = [
        r for r in REFERENCE_SEPARATOR_RE.split(refs_raw) if r
    ] if refs_raw else []
    return {
        "version": headers["version"],
        "role": headers["role"],
        "produces": headers["produces"],
        "read": headers["read"],
        "references": references,
        "body": text,
    }


def assembled_prompt_sha256(prompt: str) -> str:
    """Canonical helper: the sha256 used for replay matching and traces.

    The prompt is normalized to LF before hashing so any CRLF sneaking in
    through files cannot fork the sha.
    """
    return sha256_bytes(_normalize(prompt).encode("utf-8"))


def _canonical_request_json(request_block: dict) -> str:
    if not isinstance(request_block, dict):
        raise AssemblyError(
            f"bound request payload must be a JSON object (dict), "
            f"got {type(request_block).__name__}"
        )
    try:
        return json.dumps(
            request_block, sort_keys=True, indent=2, ensure_ascii=False
        )
    except (TypeError, ValueError) as exc:
        raise AssemblyError(f"bound request payload is not JSON-serializable: {exc}") from exc


def assemble_prompt(
    role_card_path: Path | str,
    request_block: dict,
    *,
    root: Path | str | None = None,
) -> str:
    """Assemble the single deterministic prompt for one role call.

    Sections in fixed order: harness preamble, role card, pinned references
    (in the card's pinned order), bound request payload. Every section
    header pins the content sha256, making any drift detectable. Returns the
    prompt text; use assembled_prompt_sha256() to compute its hash.
    """
    root = Path(root) if root is not None else DEFAULT_ROOT
    card_path = Path(role_card_path)
    if not card_path.is_file():
        raise AssemblyError(f"role card not found: {card_path}")

    card = parse_role_card(card_path)
    card_body = _read(card_path)
    card_sha = _content_sha(Path(card_path))

    preamble_path = root / "references" / HARNESS_PREAMBLE_FILE
    if not preamble_path.is_file():
        raise AssemblyError(
            f"harness preamble missing: {preamble_path} "
            "(must exist at <root>/references/harness-preamble.md)"
        )
    preamble = _read(preamble_path)
    preamble_sha = _content_sha(preamble_path)

    sections: list[str] = [
        f"{PREAMBLE_MARK} sha256={preamble_sha}",
        preamble,
        "</CF-HARNESS-PREAMBLE>",
        f"{ROLE_CARD_MARK} role={card['role']} version={card['version']} sha256={card_sha}",
        card_body,
        "</CF-ROLE-CARD>",
    ]

    seen_refs = set()
    for ref in card["references"]:
        ref_path = root / "references" / ref
        if not ref_path.is_file():
            raise AssemblyError(
                f"role card {card_path} pins reference '{ref}' but "
                f"{ref_path} does not exist"
            )
        if ref in seen_refs:
            continue  # duplicate pins are idempotent, not an error
        seen_refs.add(ref)
        ref_text = _read(ref_path)
        ref_sha = _content_sha(ref_path)
        sections.append(f"{REFERENCE_MARK} name={ref} sha256={ref_sha}")
        sections.append(ref_text)
        sections.append("</CF-REFERENCE>")

    sections.append(REQUEST_MARK)
    sections.append(_canonical_request_json(request_block))
    sections.append("</CF-BOUND-REQUEST>")

    return "\n".join(sections) + "\n"


def default_root() -> Path:
    """The repo root used when none is given (package parent directory)."""
    return DEFAULT_ROOT
