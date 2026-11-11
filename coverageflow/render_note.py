"""Clean, reader-facing rendering of a verified note.

The marked report (``report/vN-*.md`` with ``[EV:key]`` markers and, in the
candidate, a process section) is the *verified record*: the evidence gate and
critics check exactly that text. A reader should not see markers, finding
dispositions, harness text or the Candidate/Request header, so exports go
through :func:`render_clean`, which never touches the record itself.

``render_clean(report_md, evidence_index)``:
  (a) replaces each ``[EV:key]`` marker with a numbered footnote ``[^n]`` and
      lists ``key`` (plus doc / locator when the index carries them) under
      the note;
  (b) drops process / control sections (Open findings, Finding dispositions,
      harness or budget text);
  (c) moves the Candidate / Request / Cutoff header blockquote out of the
      reader body into a one-paragraph provenance footer.

``evidence_index`` maps a key to either the artifact path that holds it (the
orchestrator's ``_evidence_index`` shape, which carries no reader-facing
source and is therefore not printed) or a dict such as
``{"doc_id": ..., "locator": ..., "anchor": ...}`` (optionally nested under
``"citation"``).
"""

from __future__ import annotations

import re
from typing import Any

EV_MARKER = re.compile(r"\s*\[EV:([^\]]+)\]", re.IGNORECASE)

# Section headings that are process/control content, never reader content.
PROCESS_HEADING = re.compile(
    r"^(open\s+findings?|finding\s+dispositions?|dispositions?|carried\s+findings?|"
    r"review\s+(notes?|findings?|log)|critic\s+(notes?|findings?)|"
    r"process\s+notes?|harness(\s+\w+)*|budget(\s+\w+)*|"
    r"verification\s+(notes?|log)|gate\s+(results?|log))\b",
    re.IGNORECASE)

# Paragraph-level process text that can leak into reader sections.
PROCESS_PARAGRAPH = re.compile(
    r"\b(harness|per-role token cap|token cap|dispatch[- ]accounting|"
    r"finding disposition|carried findings?|critic round)\b", re.IGNORECASE)

# Header lines (blockquote) that belong in the provenance footer.
_HEADER_KEYS = re.compile(
    r"\*{0,2}(Candidate|Request|Cutoff|Bundle)\*{0,2}\s*:", re.IGNORECASE)
# A notice that must stay visible to the reader.
_KEEP_NOTICE = re.compile(r"PUBLISHED WITH EXCEPTIONS", re.IGNORECASE)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _split_header(lines: list[str]) -> tuple[list[str], list[str]]:
    """Return (body_lines, header_lines): blockquote header lines before the
    first section heading are provenance, except exception notices."""
    header: list[str] = []
    body: list[str] = []
    in_front = True
    for line in lines:
        s = line.strip()
        if in_front and _HEADING.match(s) and not s.startswith("# "):
            in_front = False
        if in_front and s.startswith(">") and not _KEEP_NOTICE.search(s):
            header.append(re.sub(r"^>\s?", "", s))
            continue
        body.append(line)
    return body, header


def strip_process_sections(report_md: str) -> str:
    """Remove process/control sections and process paragraphs; keep markers."""
    out: list[str] = []
    skip_level: int | None = None
    for line in report_md.splitlines():
        m = _HEADING.match(line.strip())
        if m:
            level = len(m.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None and level >= 2 and PROCESS_HEADING.match(m.group(2)):
                skip_level = level
                continue
        if skip_level is not None:
            continue
        out.append(line)
    # paragraph-level process text
    paras = re.split(r"\n\s*\n", "\n".join(out))
    kept = [p for p in paras
            if not (PROCESS_PARAGRAPH.search(p) and not p.lstrip().startswith(("#", "|")))]
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept)).strip() + "\n"


def _source_text(entry: Any) -> str:
    """Reader-facing source description for one index entry ('' if none)."""
    if isinstance(entry, dict):
        cit = entry.get("citation") if isinstance(entry.get("citation"), dict) else entry
        parts = [str(cit[k]) for k in ("doc_id", "doc", "anchor", "locator")
                 if cit.get(k)]
        return ", ".join(parts)
    if isinstance(entry, str) and entry and not entry.endswith((".json", ".md")):
        return entry
    return ""


def render_clean(report_md: str, evidence_index: dict | None = None) -> str:
    """Reader-facing note: footnotes, no process text, provenance in a footer."""
    index = evidence_index or {}
    lines, header = _split_header(report_md.splitlines())
    body = strip_process_sections("\n".join(lines))

    numbers: dict[str, int] = {}

    def _sub(m: re.Match) -> str:
        notes = []
        for key in [k.strip() for k in re.split(r"[;,]", m.group(1)) if k.strip()]:
            n = numbers.setdefault(key, len(numbers) + 1)
            notes.append(f"[^{n}]")
        return "".join(notes)

    body = EV_MARKER.sub(_sub, body).rstrip() + "\n"

    out = [body]
    if numbers:
        out.append("\n---\n\n## Sources\n")
        for key, n in numbers.items():
            src = _source_text(index.get(key))
            out.append(f"[^{n}]: `{key}`" + (f" ({src})" if src else ""))
    if header:
        out.append("\n---\n")
        out.append("*Provenance: " + "; ".join(
            re.sub(r"\s*·\s*", "; ", h.replace("**", "")).strip()
            for h in header if h.strip()) + "*")
    return "\n".join(out).rstrip() + "\n"
