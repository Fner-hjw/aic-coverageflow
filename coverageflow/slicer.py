"""Deterministic HTML slicing of SEC filings into bounded plain-text sections.

Owner stream: A1 (runtime & data).

Contract (docs/contracts/interfaces.md section 4)::

    def slice_document(doc_path: Path, form: str) -> list[Slice]

No LLM, no third-party parser: stdlib ``html.parser`` only. For 10-K/10-Q the
slicer emits income statement, balance sheet, cash flow statement, segment
table, and MD&A slices; for 8-K the full document text as one slice. Slices
are written as UTF-8 text files (``<out_dir>/slices/<stem>/<TAG>.txt``) with
citation anchors (matched heading + first line) and a sha256 per slice, so
the extractor role receives bounded, hash-tied evidence. Selection is
deterministic and keyword-guarded: a title line match alone is not enough —
the following table must contain expected statement vocabulary (so the table
of contents and cover page never masquerade as statements).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

STATEMENT_KINDS = {
    "IS": "income_statement",
    "BS": "balance_sheet",
    "CF": "cash_flow_statement",
    "SEG": "segments",
    "MDA": "md_and_a",
    "FULL": "full_text",
}

_ANCHORS: dict[str, re.Pattern] = {
    "IS": re.compile(
        r"consolidated\s+(?:condensed\s+)?statements?\s+of\s+operations", re.I
    ),
    "BS": re.compile(r"consolidated\s+(?:condensed\s+)?balance\s+sheets?", re.I),
    "CF": re.compile(
        r"consolidated\s+(?:condensed\s+)?statements?\s+of\s+cash\s*flows", re.I
    ),
    "SEG": re.compile(r"operating\s+segments?", re.I),
    "MDA": re.compile(r"management['\u2019]s\s+discussion\s+and\s+analysis", re.I),
}

_KEYWORDS: dict[str, list[re.Pattern]] = {
    "IS": [re.compile(r"(net\s+revenue|revenue)", re.I),
           re.compile(r"(net\s+income|operating\s+income|gross\s+margin)", re.I)],
    "BS": [re.compile(r"total\s+assets", re.I)],
    # a real cash-flow statement carries all three sections; a chart caption
    # ("Cash from Operating Activities and Adjusted Free Cash Flow $B") carries
    # one, and used to win the keyword test over the statement itself
    "CF": [re.compile(r"operating\s+activities", re.I),
           re.compile(r"(investing|financing)\s+activities", re.I)],
    "SEG": [re.compile(r"(revenue|operating\s+income)", re.I),
            re.compile(
                r"(client\s+computing|data\s+center|network\s+and\s+edge|nex|"
                r"foundry|altera|mobileye|all\s+other)", re.I)],
}

_MDA_STOP = re.compile(
    r"(^item\s+(?:3|7a|8)\b|quantitative\s+and\s+qualitative\s+disclosures|"
    r"^\s*report\s+of\s+independent)", re.I
)
_MDA_MAX_CHARS = 120_000
# The MD&A slice feeds the writer's commentary/outlook: the overview, significant
# events and the start of results of operations. A full 10-Q MD&A is ~50k chars,
# far more than a bounded evidence slice should carry; 12k chars (~3k tokens)
# covers the overview and segment trends. Cut on a block boundary (never
# mid-line) so the result is deterministic.
_MDA_SLICE_CHARS = 12_000
_MDA_NOISE = re.compile(r"^(?:table\s+of\s+contents|md&a\s*\|\s*\d+)\s*$", re.I)
# segment note: from its heading to the next note heading
_SEG_NOTE_HEAD = re.compile(
    r"^note\s*\d+\s*[:|.\-\u2013\u2014]?\s*(?:\|\s*)?.*segment", re.I)
_NOTE_HEAD = re.compile(r"^note\s*\d+\s*[:|.\-\u2013\u2014]", re.I)
_PAGE_HEADER = re.compile(r"^(?:financial\s+statements|md&a)\s*\|", re.I)
_SEG_NOTE_MAX_CHARS = 30_000

_BLOCK_END = {"p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class SliceError(Exception):
    """Raised when a form cannot be sliced."""


@dataclass(frozen=True)
class Slice:
    slice_id: str
    kind: str
    path: str
    sha256: str
    anchors: dict[str, str]
    text: str

    def to_dict(self) -> dict:
        d = {
            "slice_id": self.slice_id,
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "anchors": self.anchors,
        }
        return d


@dataclass
class _Block:
    is_table: bool
    lines: list[str] = field(default_factory=list)
    heading: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class _TableCtx:
    lines: list[str] = field(default_factory=list)
    row_cells: list[str] = field(default_factory=list)
    cur_cell: str = ""


class _FilingTextParser(HTMLParser):
    """Convert filing HTML to a normalized block stream.

    Blocks are flow lines or groups of table-row lines rendered as
    ``cell | cell | cell``. Content inside <script>/<style>/<ix:header> and
    visually-hidden elements (XBRL ix:hidden facts) is dropped. Hidden
    elements are tracked on a matching tag stack so visibility is restored
    when they close.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Block] = []
        self._flow: list[str] = []
        self._skip_depth = 0
        self._hidden_stack: list[str] = []
        self._was_heading = False
        self._tables: list[_TableCtx] = []

    # -- flow helpers ----------------------------------------------------
    def _flush_flow(self, force: bool = False) -> None:
        text = " ".join(self._flow).strip()
        self._flow = []
        if text or force:
            self.blocks.append(
                _Block(is_table=False, lines=[text.strip()], heading=self._was_heading)
            )
            self._was_heading = False

    # -- tag handlers ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style") or tag == "ix:header":
            self._skip_depth += 1
            return
        if tag == "ix:hidden":
            self._hidden_stack.append(tag)
            return
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        style = attrs_d.get("style", "").replace(" ", "").lower()
        if "hidden" in attrs_d or "display:none" in style or "visibility:hidden" in style:
            self._hidden_stack.append(tag)
            return
        if self._skip_depth or self._hidden_stack:
            return
        if tag in _HEADING_TAGS:
            self._flush_flow()
            self._was_heading = True
        elif tag == "table":
            self._flush_flow()
            self._tables.append(_TableCtx())
        elif tag == "tr" and self._tables:
            self._push_row_cell()
            self._tables[-1].row_cells = []
        elif tag in ("td", "th") and self._tables:
            self._push_row_cell()
        elif tag in _BLOCK_END:
            self._flush_flow()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") or tag == "ix:header":
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._hidden_stack:
            if tag == self._hidden_stack[-1]:
                self._hidden_stack.pop()
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS:
            self._flush_flow()
        elif tag == "td" or tag == "th":
            self._push_row_cell()
        elif tag == "tr" and self._tables:
            self._finalize_row()
        elif tag == "table" and self._tables:
            self._finalize_row()
            ctx = self._tables.pop()
            rendered = "\n".join(ctx.lines).strip()
            if self._tables:
                # nested table: fold into the enclosing cell text
                outer = self._tables[-1]
                outer.cur_cell = (outer.cur_cell + " " + rendered).strip()
            elif rendered:
                self.blocks.append(_Block(is_table=True, lines=ctx.lines))
        elif tag in _BLOCK_END:
            self._flush_flow()

    def _push_row_cell(self) -> None:
        if self._tables:
            ctx = self._tables[-1]
            if ctx.cur_cell.strip():
                ctx.row_cells.append(" ".join(ctx.cur_cell.split()))
            ctx.cur_cell = ""

    def _finalize_row(self) -> None:
        self._push_row_cell()
        if self._tables:
            ctx = self._tables[-1]
            if ctx.row_cells:
                ctx.lines.append(" | ".join(ctx.row_cells))
            ctx.row_cells = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._hidden_stack:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._tables:
            ctx = self._tables[-1]
            ctx.cur_cell = (ctx.cur_cell + " " + text).strip()
        else:
            self._flow.append(text)

    def close(self) -> None:
        super().close()
        self._flush_flow()


def _blocks_from_html(html_text: str) -> list[_Block]:
    parser = _FilingTextParser()
    parser.feed(html_text)
    parser.close()
    return [b for b in parser.blocks if b.text.strip()]


def _kw_pass(block: _Block, kind: str) -> bool:
    text = block.text.lower()
    return all(p.search(text) for p in _KEYWORDS[kind])


def _nonempty_line(block: _Block) -> str | None:
    for line in block.lines:
        if line.strip():
            return line.strip()
    return None


def _first_nonempty(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _next_statement_boundary(blocks: list[_Block], start: int) -> int:
    """Index of the next anchor line of ANY statement kind on/after start."""
    for i in range(start, len(blocks)):
        if blocks[i].is_table:
            continue
        for kind in ("IS", "BS", "CF"):
            if _ANCHORS[kind].search(blocks[i].text):
                return i
    return len(blocks)


# A financial statement table opens with a units or period header. The audit
# report and the MD&A also name the statements and also mention "total assets"
# or "operating activities", so keyword matching alone picks the wrong block:
# in a 10-K it selected the critical-audit-matter table as the balance sheet
# and a 570-char MD&A excerpt as the cash-flow statement.
_STATEMENT_HEADER = re.compile(
    r"^\s*(?:\(?\s*(?:in\s+(?:millions|thousands|billions)|unaudited)"
    r"|(?:three|six|nine|twelve)\s+months\s+ended"
    r"|years?\s+ended"
    r"|(?:january|february|march|april|may|june|july|august|september"
    r"|october|november|december)\s+\d)",
    re.I,
)


def _anchor_score(block: _Block, pattern: re.Pattern) -> int:
    """A statement's own title is a short heading.

    The same phrase also appears in the table of contents and in prose that
    refers back to the statement; those are long blocks and must not win.
    """
    text = block.text.strip()
    return 4 if len(text) <= 120 and pattern.search(text) else 0


def _statement_score(block: _Block) -> int:
    """How much a table looks like the statement itself, not prose about it."""
    first = _first_nonempty(block.text)
    score = 0
    if _STATEMENT_HEADER.match(first):
        score += 4
    if len(block.text) > 400:
        score += 1
    return score


def _select_statement(blocks: list[_Block], kind: str) -> tuple[int, _Block, str] | None:
    """Pick the statement table among all anchor hits.

    Candidates are every table passing the keyword guard; the best-scoring one
    wins (period header first, then the longer table), with the earliest hit
    breaking ties. Returns (block_idx, table_block, anchor_line).
    """
    anchor_re = _ANCHORS[kind]
    hits = [i for i, b in enumerate(blocks) if anchor_re.search(b.text)]
    fallback: tuple[int, _Block, str] | None = None
    best: tuple[int, int, _Block, str] | None = None
    for hit in hits:
        anchor = _anchor_line(blocks[hit], anchor_re)
        head = _anchor_score(blocks[hit], anchor_re)
        end = _next_statement_boundary(blocks, hit + 1)
        for i in range(hit + 1, end):
            if not blocks[i].is_table:
                continue
            if _kw_pass(blocks[i], kind):
                score = head + _statement_score(blocks[i])
                # on an equal score the longer table wins: a 10-K prints a
                # short summary of the cash-flow statement in the MD&A before
                # the statement itself, and both open with the same header
                if best is None or (score, len(blocks[i].text)) > (
                        best[0], len(best[2].text)):
                    best = (score, i, blocks[i], anchor)
            elif fallback is None and blocks[i].lines:
                fallback = (i, blocks[i], anchor)
    if best is not None:
        return best[1], best[2], best[3]
    return fallback


def _select_segment_note(blocks: list[_Block]) -> tuple[int, str, str] | None:
    """Every table between the segment note heading and the next note heading.

    A 10-Q segment note prints quarter and year-to-date tables for the current
    and the prior year (plus reconciliations); picking one "best" table drops
    the current period. Returns (heading_idx, text, anchor) or None when no
    segment note heading exists (callers fall back to ``_select_statement``).
    """
    start = None
    for i, b in enumerate(blocks):
        t = b.text.strip()
        if len(t) <= 100 and _SEG_NOTE_HEAD.search(t):
            start = i  # last match: the note itself follows the table of contents
    if start is None:
        return None
    anchor = " ".join(blocks[start].text.split())
    parts: list[str] = []
    for j in range(start + 1, len(blocks)):
        b = blocks[j]
        t = b.text.strip()
        if len(t) <= 100 and _NOTE_HEAD.search(t):
            break
        if b.is_table and not _PAGE_HEADER.match(t) and _kw_pass(b, "SEG"):
            parts.append(b.text)
        elif b.is_table and not _PAGE_HEADER.match(t) and b.lines and len(t) > 150:
            parts.append(b.text)  # reconciliation tables lacking segment names
    if not parts:
        return None
    text = anchor + "\n\n" + "\n\n".join(parts)
    return start, text[:_SEG_NOTE_MAX_CHARS].rstrip(), anchor


_ITEM_MARK = re.compile(r"item\s*\d", re.I)


def _anchor_line(block: _Block, pattern: re.Pattern) -> str:
    for ln in block.lines:
        if ln.strip() and pattern.search(ln):
            return ln.strip()
    return block.text.strip()


def _mda_body_len(blocks: list[_Block], start: int) -> int:
    total = 0
    for j in range(start + 1, len(blocks)):
        if _MDA_STOP.search(blocks[j].text):
            break
        total += len(blocks[j].text)
    return total


def _select_mda(blocks: list[_Block]) -> tuple[int, str, str] | None:
    """MD&A anchor: any block whose text matches the title; the section header
    is preferred over the table-of-contents entry (hits marked ``Item N`` win,
    otherwise the last occurrence — the section follows the notes; the TOC is
    early). The slice runs until the next Item heading / market-risk line,
    including tables in the section body."""
    anchor_re = _ANCHORS["MDA"]
    hits = [i for i, b in enumerate(blocks) if anchor_re.search(b.text)]
    if not hits:
        return None
    # The section's own heading is a short block. Long hits are the table of
    # contents or prose cross-references, and the trailing "Item Number | Item"
    # index table also carries the phrase plus an "Item N" marker (it used to be
    # picked as the start, leaving a 550-byte slice). Prefer short headings; if
    # several, take the one with the longest body, which skips TOC entries.
    headings = [i for i in hits if len(blocks[i].text.strip()) <= 120]
    if headings:
        start = max(headings, key=lambda i: (_mda_body_len(blocks, i), -i))
    else:
        item_marked = [i for i in hits if _ITEM_MARK.search(blocks[i].text)]
        start = (item_marked or hits)[-1]
    anchor = _anchor_line(blocks[start], anchor_re)
    parts: list[str] = [anchor]
    size = len(anchor)
    for j in range(start + 1, len(blocks)):
        nxt = blocks[j]
        if _MDA_STOP.search(nxt.text):
            break
        if _MDA_NOISE.match(nxt.text.strip()):
            continue
        if size + len(nxt.text) + 1 > _MDA_SLICE_CHARS and len(parts) > 1:
            break
        parts.append(nxt.text)
        size += len(nxt.text) + 1
    text = "\n".join(parts)[:_MDA_MAX_CHARS].rstrip()
    if len(text) <= len(anchor) + 10:  # nothing meaningful following
        return None
    return start, text, anchor


def candidate_slices(doc_path: Path, form: str) -> list[Slice]:
    """The slices this document yields, computed without writing anything.

    Ingest's whole job is to classify a document and declare its slices, and
    both are a pure function of the filing. Handing it these candidates lets
    it answer from the request instead of reading twelve filings with tools,
    which is what its card already says it does.
    """
    doc_path = Path(doc_path)
    form = form.upper()
    stem = doc_path.stem
    text_html = doc_path.read_bytes().decode("utf-8", errors="replace")
    blocks = _blocks_from_html(text_html)

    results: list[tuple[str, str, str, str]] = []  # (tag, anchor_heading, text, relpath)
    if form == "8-K":
        body = "\n".join(
            b.text for b in blocks if b.text.strip()
        )[:_MDA_MAX_CHARS] or "(empty document)"
        results.append(("FULL", _first_nonempty(body), body, f"slices/{stem}/FULL.txt"))
    elif form in ("10-K", "10-Q"):
        for tag in ("IS", "BS", "CF"):
            sel = _select_statement(blocks, tag)
            if sel is None:
                continue
            _, block, anchor = sel
            results.append((tag, anchor, block.text, f"slices/{stem}/{tag}.txt"))
        seg_note = _select_segment_note(blocks)
        if seg_note:
            _, seg_text, anchor = seg_note
            results.append(("SEG", anchor, seg_text, f"slices/{stem}/SEG.txt"))
        else:
            sel = _select_statement(blocks, "SEG")
            if sel:
                _, block, anchor = sel
                results.append(("SEG", anchor, block.text, f"slices/{stem}/SEG.txt"))
        mda = _select_mda(blocks)
        if mda:
            _, mda_text, anchor = mda
            results.append(("MDA", anchor, mda_text, f"slices/{stem}/MDA.txt"))
    else:
        raise SliceError(f"unsupported form for slicing: {form!r}")

    out: list[Slice] = []
    for tag, anchor, text, relpath in results:
        blob = (text.rstrip() + "\n").encode("utf-8")
        out.append(Slice(
            slice_id=f"{stem}/{tag}",
            kind=STATEMENT_KINDS[tag],
            path=relpath,
            sha256=hashlib.sha256(blob).hexdigest(),
            anchors={"heading": anchor, "first_line": _first_nonempty(text)},
            text=blob.decode("utf-8"),
        ))
    return out


def slice_document(doc_path: Path, form: str, out_dir: Path | None = None) -> list[Slice]:
    """Slice one filing. Writes slice .txt files, returns list[Slice].

    ``out_dir`` is the case/bundle inputs directory; slice files land under
    ``out_dir/slices/<doc_stem>/<TAG>.txt`` and ``Slice.path`` is relative to
    ``out_dir``. Defaults to the document's own directory.
    """
    doc_path = Path(doc_path)
    out_dir = Path(out_dir) if out_dir is not None else doc_path.parent
    slices = candidate_slices(doc_path, form)
    for sl in slices:
        target = out_dir / sl.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(target) + ".tmp")
        tmp.write_bytes(sl.text.encode("utf-8"))
        os.replace(tmp, target)
    return slices
