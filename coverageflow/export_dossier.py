"""LaTeX dossier renderer (owner: stream A5; contracts interfaces section 4).

Minimal, dependency-free renderer that turns a case's VERIFIED artifacts
into a GS/MS-style institutional research note:

  report.md (writer role, evidence-linked)
    + model.json / valuation.json (deterministic engines)
    + case metadata (mandate, result, input manifest, approval record)
    -> references/dossier-template.tex ({{TOKENS}})
    -> <case>/export/dossier.tex -> pdflatex -> <case>/export/dossier.pdf

All numbers are read from the evidence artifacts (nothing hand-typed), so
the dossier cannot drift from the verified record. If pdflatex is not on
PATH the .tex is left on disk (graceful fallback).

CLI:   python -m coverageflow.export_dossier --case <case-dir> [--out-dir export]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .render_note import render_clean, strip_process_sections

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "references" \
    / "dossier-template.tex"

SEGMENT_ORDER = ["ccg", "dcai", "nex", "foundry", "altera", "mobileye",
                 "all_other"]
_PRETTY_SEG = {"ccg": "Client Computing Group (CCG)", "dcai": "Data Center & AI (DCAI)",
               "nex": "Network & Edge (NEX)", "foundry": "Intel Foundry",
               "altera": "Altera", "mobileye": "Mobileye",
               "all_other": "All other / intersegment eliminations"}


# ----------------------------------------------------------------- helpers
def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_or_none(path: Path) -> dict | None:
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def _tex_escape(text: str) -> str:
    for ch, rep in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"),
                    ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
                    ("~", r"\textasciitilde{}"),
                    ("^", r"\textasciicircum{}")):
        text = text.replace(ch, rep)
    return text


def _fmt_mm(value: Any) -> str:
    if value is None:
        return "--"
    return f"{float(value):,.0f}"


def _fmt_usd(value: Any, digits: int = 2) -> str:
    if value is None:
        return "--"
    return f"{float(value):,.{digits}f}"


def _fmt_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "--"
    return f"{float(value) * 100:.{digits}f}\\%"


def _fmt_x(value: Any) -> str:
    if value is None:
        return "--"
    return f"{float(value):.1f}x"


def _md_to_tex(text: str) -> str:
    """Minimal, robust markdown -> LaTeX for the report body."""
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        # stash evidence-link tokens before escaping (they need raw keys)
        stashed: list[str] = []

        def _stash(m: re.Match) -> str:
            stashed.append(m.group(1))
            return f"\x00{len(stashed) - 1}\x00"

        line = re.sub(r"\[EV:([A-Za-z0-9_.]+)\]", _stash, stripped)
        line = _tex_escape(line)
        for i, key in enumerate(stashed):
            line = line.replace(f"\x00{i}\x00",
                                f"\\texttt{{[EV:{_tex_escape(key)}]}}")
        # bold **...**
        line = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", line)
        if line.startswith("- "):
            line = r"\quad$\bullet$~" + line[2:]
        out_lines.append(line)
    return "\n".join(out_lines).strip()


def _format_evidence_reference(key: str, number: int, evidence: dict) -> str:
    """Describe candidate-specific sources without inventing period-level attribution."""
    facts = (evidence.get("facts") or {}).get("facts") or []
    docs = {d.get("doc_id"): d for d in (evidence.get("manifest") or {}).get("documents", [])}
    model = evidence.get("model") or {}
    valuation = evidence.get("valuation") or {}

    def readable_field(field: str) -> str:
        return field.replace(".", " / ").replace("_", " ")

    def sources_for(field: str) -> list[str]:
        seen: set[tuple] = set()
        entries = []
        for fact in facts:
            if fact.get("field") != field or not fact.get("citation"):
                continue
            citation = fact["citation"]
            source_id = (citation.get("doc_id"), citation.get("anchor"), citation.get("locator"))
            if source_id in seen:
                continue
            seen.add(source_id)
            doc = docs.get(citation.get("doc_id")) or {}
            details = [citation.get("doc_id") or "Document not identified"]
            if doc.get("form"):
                details.append(doc["form"])
            if doc.get("filed"):
                details.append("Filed " + str(doc["filed"]))
            if citation.get("anchor"):
                details.append(citation["anchor"])
            if citation.get("locator"):
                details.append(citation["locator"])
            entry = _tex_escape("; ".join(map(str, details)))
            url = doc.get("source_url") or ""
            if re.fullmatch(r"https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+", url):
                entry += r". \href{" + url.replace("%", r"\%") + r"}{Open source document}"
            entries.append(entry)
        return entries

    def dig(obj: dict, dotted: str) -> bool:
        cur: Any = obj
        for segment in dotted.split("."):
            if not isinstance(cur, dict) or segment not in cur:
                return False
            cur = cur[segment]
        return True

    source_list = sources_for(key)
    if source_list:
        details = r"\textbf{Filing source.} " + r"; ".join(source_list)
    elif key in (model.get("derivations") or {}):
        formula = str(model["derivations"][key])
        fields = list(dict.fromkeys(re.findall(r"\b[a-z_]+(?:\.[a-z_]+)+\b", formula)))
        inputs = [(field, sources_for(field)) for field in fields]
        readable_formula = re.sub(r"\b[a-z_]+(?:\.[a-z_]+)+\b",
                                  lambda match: readable_field(match.group()), formula)
        details = r"\textbf{Model calculation.} Formula: " + _tex_escape(readable_formula) + "."
        for field, sources in inputs:
            details += r"\newline " + _tex_escape(readable_field(field)) + ": " + ("; ".join(sources) if sources else "Source not resolved in candidate facts") + "."
    elif key in (model.get("series") or {}):
        details = r"\textbf{Model output.} " + _tex_escape(readable_field(key)) + r" in model.json. Underlying source not resolved for this field."
    elif dig(valuation, key):
        details = r"\textbf{Valuation calculation.} " + _tex_escape(readable_field(key)) + r" in valuation.json. Review the model and assumptions for its inputs."
    else:
        details = r"\textbf{Unresolved.} No matching source or calculation was found in this candidate's facts, model, or valuation."
    return r"\noindent\textbf{[" + str(number) + r"]} " + details + r"\par"


def _report_tex(report_md: str, evidence: dict | None = None) -> tuple[str, str, str]:
    """Render the full report and a numbered, searchable evidence-key index."""
    references: dict[str, int] = {}

    def inline(text: str) -> str:
        parts = re.split(r"(\[EV:[^\]]+\])", text)
        output = []
        for part in parts:
            if part.startswith("[EV:") and part.endswith("]"):
                key = part[4:-1]
                number = references.setdefault(key, len(references) + 1)
                if output:
                    output[-1] = output[-1].rstrip()
                output.append(r"\evref{" + str(number) + "}")
            else:
                escaped = _tex_escape(part)
                output.append(re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", escaped))
        return "".join(output)

    lines = report_md.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "Research report")
    body: list[str] = []
    in_list = False
    in_table = False
    in_report = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            continue
        if stripped.startswith("> ") and not in_report:
            continue  # Header metadata is displayed in the document masthead.
        if stripped.startswith("| ") or (stripped.startswith("|") and stripped.endswith("|")):
            if re.fullmatch(r"\|[-\s|:]+\|?", stripped):
                continue
            if in_list:
                body.append(r"\end{itemize}")
                in_list = False
            # Preserve all cells even when the original table is wider than the page.
            cells = [inline(cell.strip()) for cell in stripped.strip("|").split("|")]
            body.append(r"\noindent " + r" \quad | \quad ".join(cells) + r"\par")
            in_table = True
            continue
        if in_table:
            body.append("")
            in_table = False
        if re.match(r"^[-*] ", stripped):
            if not in_list:
                body.append(r"\begin{itemize}")
                in_list = True
            body.append(r"\item " + inline(stripped[2:]))
            continue
        if in_list:
            body.append(r"\end{itemize}")
            in_list = False
        heading = re.match(r"^(#{2,6})\s+(.+)$", stripped)
        if heading:
            in_report = True
            command = "section" if len(heading.group(1)) == 2 else "subsection"
            body.append(r"\Needspace{9\baselineskip}" + "\n" + "\\" + command + "*{" + inline(heading.group(2)) + "}")
        elif stripped.startswith("> "):
            body.append(inline(stripped[2:]) + "\n")
        elif stripped:
            body.append(inline(stripped) + "\n")
        else:
            body.append("")
    if in_list:
        body.append(r"\end{itemize}")
    entries = [_format_evidence_reference(key, number, evidence or {})
               for key, number in references.items()]
    return _tex_escape(title), "\n".join(body), "\n".join(entries) or r"\textit{No evidence references in this report.}"


def _md_sections(report_md: str) -> dict[str, str]:
    """Split report.md into {lowercased heading: body} chunks."""
    sections: dict[str, str] = {}
    current, body = None, []
    for line in report_md.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line.strip())
        if m:
            if current is not None:
                sections[current.lower()] = "\n".join(body).strip()
            current, body = m.group(1), []
        elif current is not None:
            body.append(line)
    if current is not None:
        sections[current.lower()] = "\n".join(body).strip()
    return {k: _md_to_tex(v) for k, v in sections.items()}


def _series_value(model: dict, field: str, period: str) -> Any:
    entry = (model.get("series") or {}).get(field) or {}
    return entry.get(period)


def _fallback(tex_body: str, section: str) -> str:
    return tex_body if tex_body else f"\\textit{{(No ``{section}'' section " \
                                     f"in the verified report.)}}"


# ------------------------------------------------------------------ tables
def _key_data_table(model: dict, valuation: dict, mandate: dict) -> str:
    s = model.get("series") or {}
    assumptions = mandate.get("assumptions") or {}

    def row(label, value, prev=None):
        if prev is not None:
            return f"{label} & {value} & {prev} \\\\"
        return f"{label} & {value} &  \\\\"

    rows = [
        row("Revenue (2026-Q3)", _fmt_mm(_series_value(model, "income_statement.revenue", "2026-Q3")),
            _fmt_mm(_series_value(model, "income_statement.revenue", "2025-Q3"))),
        row("Gross profit", _fmt_mm(_series_value(model, "income_statement.gross_profit", "2026-Q3")),
            _fmt_mm(_series_value(model, "income_statement.gross_profit", "2025-Q3"))),
        row("Operating income", _fmt_mm(_series_value(model, "income_statement.operating_income", "2026-Q3")),
            _fmt_mm(_series_value(model, "income_statement.operating_income", "2025-Q3"))),
        row("Net income", _fmt_mm(_series_value(model, "income_statement.net_income", "2026-Q3")),
            _fmt_mm(_series_value(model, "income_statement.net_income", "2025-Q3"))),
        row("Diluted EPS (USD)", _fmt_usd(_series_value(model, "income_statement.diluted_eps", "2026-Q3"), 2),
            _fmt_usd(_series_value(model, "income_statement.diluted_eps", "2025-Q3"), 2)),
        row("Cash \\& equivalents", _fmt_mm(_series_value(model, "balance_sheet.cash_and_equivalents", "2026-Q3")),
            _fmt_mm(_series_value(model, "balance_sheet.cash_and_equivalents", "2025-Q3"))),
        row("Long-term debt", _fmt_mm(_series_value(model, "balance_sheet.long_term_debt", "2026-Q3")),
            _fmt_mm(_series_value(model, "balance_sheet.long_term_debt", "2025-Q3"))),
        row("Net debt (valuation)", _fmt_mm((valuation.get("dcf") or {}).get("net_debt")), ""),
        row("Diluted shares (mm)", _fmt_mm((valuation.get("dcf") or {}).get("diluted_shares")), ""),
        row("WACC (mandate)", _fmt_pct(assumptions.get("wacc")), ""),
        row("Terminal growth (mandate)", _fmt_pct(assumptions.get("terminal_growth")), ""),
        row("Exit multiple (mandate, EBITDA)", _fmt_x(assumptions.get("exit_multiple_ebitda")), ""),
    ]
    return ("\\begin{tabular}{@{}lrr@{}}\n\\toprule\n"
            "Metric & 2026-Q3 & 2025-Q3 \\\\\n\\midrule\n"
            + "\n".join(rows) + "\n\\bottomrule\n\\end{tabular}")


def _segment_table(model: dict) -> str:
    lines = ["\\begin{tabular}{@{}lrrr@{}}", "\\toprule",
             "Segment & Revenue Q3'26 & Op.\\ income Q3'26 & Revenue FY2025 \\\\",
             "\\midrule"]
    seen = set()
    segs = SEGMENT_ORDER + [k for k in (model.get("series") or {})
                            if k.startswith("segments.")]
    for seg in segs:
        if seg in seen:
            continue
        seen.add(seg)
        if seg == "all_other":
            rev_field, oi_field = "segments.all_other_revenue", \
                "segments.all_other_operating_income"
        else:
            rev_field, oi_field = f"segments.{seg}.revenue", \
                f"segments.{seg}.operating_income"
        rev_q = _series_value(model, rev_field, "2026-Q3")
        oi_q = _series_value(model, oi_field, "2026-Q3")
        rev_fy = _series_value(model, rev_field, "FY2025")
        if rev_q is None and oi_q is None and rev_fy is None:
            continue
        lines.append(f"{_tex_escape(_PRETTY_SEG.get(seg, seg))} & "
                     f"{_fmt_mm(rev_q)} & {_fmt_mm(oi_q)} & {_fmt_mm(rev_fy)} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def _dcf_table(valuation: dict) -> str:
    dcf = valuation.get("dcf") or {}
    lines = ["\\begin{tabular}{@{}lrrrr@{}}", "\\toprule",
             "Year & Revenue & FCF & Discount factor & PV \\\\", "\\midrule"]
    base = dcf.get("base") or {}
    if base:
        lines.append(f"Base ({_tex_escape(str(base.get('period', 'base')))}) & "
                     f"{_fmt_mm(base.get('revenue'))} & {_fmt_mm(base.get('fcf'))} & "
                     f"1.00 & {_fmt_mm(base.get('fcf'))} \\\\")
    for yr in dcf.get("years") or []:
        lines.append(f"Y{yr.get('year')} & {_fmt_mm(yr.get('revenue'))} & "
                     f"{_fmt_mm(yr.get('fcf'))} & {float(yr.get('discount_factor', 0)):.3f} & "
                     f"{_fmt_mm(yr.get('pv'))} \\\\")
    lines += ["\\midrule",
              f"PV of forecast FCF & \\multicolumn{{4}}{{r}}{{{_fmt_mm(dcf.get('pv_sum'))}}} \\\\",
              f"EV (exit multiple) & \\multicolumn{{4}}{{r}}{{{_fmt_mm((dcf.get('ev') or {}).get('exit_multiple'))}}} \\\\",
              f"EV (Gordon) & \\multicolumn{{4}}{{r}}{{{_fmt_mm((dcf.get('ev') or {}).get('gordon'))}}} \\\\",
              f"Net debt & \\multicolumn{{4}}{{r}}{{({_fmt_mm(dcf.get('net_debt'))})}} \\\\",
              "\\midrule",
              f"Equity value (exit multiple) & \\multicolumn{{4}}{{r}}{{{_fmt_mm((dcf.get('equity_value') or {}).get('exit_multiple'))}}} \\\\",
              f"Equity value (Gordon) & \\multicolumn{{4}}{{r}}{{{_fmt_mm((dcf.get('equity_value') or {}).get('gordon'))}}} \\\\",
              f"Per diluted share (exit multiple) & \\multicolumn{{4}}{{r}}{{\\textbf{{{_fmt_usd((dcf.get('per_diluted_share') or {}).get('exit_multiple'))}}}}} \\\\",
              f"Per diluted share (Gordon) & \\multicolumn{{4}}{{r}}{{\\textbf{{{_fmt_usd((dcf.get('per_diluted_share') or {}).get('gordon'))}}}}} \\\\",
              "\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def _comps_table(valuation: dict, mandate: dict) -> str:
    comps = valuation.get("comps") or {}
    peers = [p for p in (mandate.get("assumptions") or {}).get("peer_block", {})
             .get("peers", []) if p]
    applied = comps.get("applied") or {}
    lines = ["\\begin{tabular}{@{}lrrrr@{}}", "\\toprule",
             "Peer & P/E (TTM) & EV/EBITDA & P/S & Rev growth \\\\", "\\midrule"]
    if applied:
        for ticker, m in applied.items():
            lines.append(f"{_tex_escape(str(ticker))} & {_fmt_x(m.get('p_e_ttm'))} & "
                         f"{_fmt_x(m.get('ev_ebitda'))} & {_fmt_x(m.get('p_s'))} & "
                         f"{_fmt_pct(m.get('rev_growth_yoy'))} \\\\")
    elif peers:
        lines.append("\\textit{(Peer block present but multiples not applied.)} & & & & \\\\")
    else:
        lines.append("\\textit{(No peer multiples supplied in this mandate; "
                     "comps cross-check not computed.)} & & & & \\\\")
    lines += ["\\midrule"]
    if comps.get("comps_price") is not None:
        lines.append(f"Comps-implied price per share & \\multicolumn{{4}}{{r}}"
                     f"{{\\textbf{{{_fmt_usd(comps.get('comps_price'))}}}}} \\\\")
    note = valuation.get("cross_check_note") or "DCF vs.\\ comps cross-check."
    lines += ["\\end{tabular}", "",
              f"\\textit{{Cross-check: {_tex_escape(str(note))}}}"]
    return "\n".join(lines)


def _sensitivity_table(valuation: dict) -> str:
    sens = valuation.get("sensitivity") or {}
    wacc_points = sens.get("wacc_points") or []
    tg_points = sens.get("terminal_growth_points") or []
    grid = sens.get("grid") or []
    if not (wacc_points and tg_points and grid):
        return "\\textit{(no sensitivity grid in the valuation artifact)}"
    lines = ["\\begin{tabular}{@{}" + "r" * (len(tg_points) + 1) + "@{}}",
             "\\toprule",
             "WACC \\textbackslash{} g & " + " & ".join(
                 _fmt_pct(g, 1) for g in tg_points) + " \\\\", "\\midrule"]
    for i, w in enumerate(wacc_points):
        row_grid = grid[i] if i < len(grid) else []
        cells = " & ".join(_fmt_usd(v) for v in row_grid[:len(tg_points)])
        lines.append(f"{_fmt_pct(w, 1)} & {cells} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def _inputs_table(manifest_path: Path) -> str:
    manifest = _read_json_or_none(manifest_path) or {}
    lines = ["\\begin{tabular}{@{}llll@{}}", "\\toprule",
             "Document & Form & Filed & SHA-256 (first 16) \\\\", "\\midrule"]
    for doc in manifest.get("documents", []):
        sha = (doc.get("sha256") or "")[:16] or "--"
        lines.append(f"{_tex_escape(str(doc.get('doc_id')))} & "
                     f"{_tex_escape(str(doc.get('form')))} & "
                     f"{_tex_escape(str(doc.get('filed')))} & \\texttt{{{sha}}} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines) if len(lines) > 5 else "\\textit{(no input manifest)}"


def _artifacts_table(case_manifest: dict | None) -> str:
    lines = ["\\begin{tabular}{@{}lll@{}}", "\\toprule",
             "Role & Artifact & SHA-256 (first 16) \\\\", "\\midrule"]
    reports = (case_manifest or {}).get("reports") or []
    for rep in reports[:9]:
        lines.append(f"{_tex_escape(str(rep.get('role')))} & "
                     f"\\texttt{{{_tex_escape(str(rep.get('artifact')))}}} & "
                     f"\\texttt{{{(rep.get('sha256') or '')[:16]}}} \\\\")
    if not reports:
        lines.append("\\textit{(candidate manifest not present)} & & \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines)


def _approval_line(approval: dict | None, result: dict | None) -> str:
    if approval:
        return (f"Decision \\textbf{{{_tex_escape(approval.get('decision', ''))}}} "
                f"by {_tex_escape(approval.get('by', ''))} at "
                f"\\texttt{{{_tex_escape(approval.get('ts', ''))}}} "
                f"(record \\texttt{{{_tex_escape(approval.get('decision_id', ''))}}}).")
    if result:
        return f"Terminal: {_tex_escape(str(result.get('terminal', '')))}; "
    return "\\textit{(case not yet approved)}"


# ------------------------------------------------------------------- main
def collect_case(case_dir: Path) -> dict:
    """Load the verified artifacts of a case directory into an evidence dict."""
    case_dir = Path(case_dir)
    mandate = _read_json(case_dir / "mandate.json")
    result = _read_json_or_none(case_dir / "result.json")
    state = _read_json_or_none(case_dir / "state.json") or {}
    manifest = _read_json_or_none(case_dir / "inputs" / "manifest.json")

    # candidate round: prefer the closed result, else the last built round
    rounds_dir = case_dir / "rounds"
    built_round = (state.get("candidate") or {}).get("built_round") or 1
    round_dir = rounds_dir / f"round-{built_round:03d}"
    gen_dir = round_dir / "generator"

    report_version = None
    report_md = ""
    if result:
        report_version = result.get("report_version")
        report_path = case_dir / "report" / f"{report_version}.md"
        if report_path.exists():
            report_md = report_path.read_text(encoding="utf-8")
        else:
            for p in sorted(q for q in (case_dir / "report").glob("*.md")
                            if not q.name.endswith(".clean.md")) if (case_dir / "report").exists() else []:
                report_md = p.read_text(encoding="utf-8")
    if not report_md:
        fallback = gen_dir / "report.md"
        if fallback.exists():
            report_md = fallback.read_text(encoding="utf-8")

    approval = None
    appr_dir = case_dir / "approvals"
    if appr_dir.exists():
        for p in sorted(appr_dir.glob("*.json")):
            approval = _read_json(p)
    candidate_manifest = _read_json_or_none(gen_dir / "candidate-manifest.json")
    return {
        "case_dir": case_dir,
        "case_id": (state.get("case_id") or case_dir.name),
        "mandate": mandate,
        "result": result,
        "state": state,
        "manifest": manifest,
        "manifest_path": case_dir / "inputs" / "manifest.json",
        "facts": _read_json_or_none(gen_dir / "facts.json") or {},
        "model": _read_json_or_none(gen_dir / "model.json") or {},
        "valuation": _read_json_or_none(gen_dir / "valuation.json") or {},
        "report_md": report_md,
        "report_version": report_version,
        "candidate_manifest": candidate_manifest,
        "approval": approval,
        "built_round": built_round,
    }


def render_dossier_tex(evidence: dict, template_path: Path | None = None) -> str:
    """Fill the {{TOKEN}} placeholders of the dossier template."""
    template = (Path(template_path) if template_path else TEMPLATE_PATH) \
        .read_text(encoding="utf-8")
    ev = evidence
    mandate, model, valuation = ev["mandate"], ev["model"], ev["valuation"]
    # The marked report stays the verified record; the dossier body drops
    # process/control sections (open findings, dispositions, harness text).
    report_md = strip_process_sections(ev["report_md"] or "")
    sections = _md_sections(report_md)

    company_name = {"INTC": "Intel Corporation"}.get(
        (mandate.get("ticker") or "").upper(), mandate.get("ticker", "Covered company"))
    dcf = valuation.get("dcf") or {}
    pt_exit = _fmt_usd((dcf.get("per_diluted_share") or {}).get("exit_multiple"))
    pt_gordon = _fmt_usd((dcf.get("per_diluted_share") or {}).get("gordon"))
    cand = ev["state"].get("candidate", {}).get("candidate_id") or ""
    result = ev["result"] or {}
    title, report_body, evidence_keys = _report_tex(report_md, ev)
    approval = ev.get("approval") or {}
    approved = approval.get("decision") == "APPROVED"

    tokens = {
        "REPORT_TITLE": title,
        "REPORT_BODY": report_body,
        "EVIDENCE_KEYS": evidence_keys,
        "DOCUMENT_STATUS": "Approved report" if approved else "Draft for review - not approved",
        "APPROVAL_SUMMARY": ("Approved by " + _tex_escape(str(approval.get("by") or "Not recorded"))) if approved else "No publication approval recorded",
        "COMPANY": _tex_escape(company_name),
        "TICKER": _tex_escape(str(mandate.get("ticker", ""))),
        "CUTOFF": _tex_escape(str(mandate.get("cutoff_date", ""))),
        "REPORT_VERSION": _tex_escape(str(result.get("report_version") or "draft")),
        "CANDIDATE_ID8": _tex_escape(cand[:16]),
        "CASE_ID": _tex_escape(ev["case_id"]),
        "RATING": "Not rated --- verification run (course project)",
        "PT_EXIT": pt_exit,
        "PT_GORDON": pt_gordon,
        "ANALYST": _tex_escape(str((ev.get("approval") or {}).get("by") or "CoverageFlow")),
        "PUBLISHED_TS": _tex_escape(str((ev.get("approval") or {}).get("ts") or "unpublished")),
        "ENGINE_NOTE": (_tex_escape(str(model.get("engine", ""))) + " / "
                        + _tex_escape(str(valuation.get("engine", "")))),
        "KEY_DATA_TABLE": _key_data_table(model, valuation, mandate),
        "SUMMARY": _fallback(sections.get("summary", ""), "Summary"),
        "RESULTS": _fallback(sections.get("results", ""), "Results"),
        "SEGMENT_BODY": _fallback(sections.get("segment detail", ""), "Segment detail"),
        "SEGMENT_TABLE": _segment_table(model),
        "OUTLOOK": _fallback(sections.get("outlook", ""), "Outlook"),
        "VALUATION_BODY": _fallback(sections.get("valuation", ""), "Valuation"),
        "DCF_TABLE": _dcf_table(valuation),
        "COMPS_TABLE": _comps_table(valuation, mandate),
        "SENSITIVITY_TABLE": _sensitivity_table(valuation),
        "PEER_AS_OF": _tex_escape(str((valuation.get("comps") or {}).get("as_of")
                                      or mandate.get("cutoff_date", ""))),
        "RISKS": _fallback(sections.get("risks", ""), "Risks"),
        "INPUTS_TABLE": _inputs_table(ev["manifest_path"]),
        "ARTIFACTS_TABLE": _artifacts_table(ev["candidate_manifest"]),
        "APPROVAL_LINE": _approval_line(ev["approval"], result),
    }
    tex = template
    for key, value in tokens.items():
        tex = tex.replace("{{" + key + "}}", str(value))
    leftover = sorted(set(re.findall(r"\{\{[A-Z_0-9]+\}\}", tex)))
    if leftover:
        raise RuntimeError(f"unfilled dossier template tokens: {leftover}")
    return tex


def evidence_index_from_facts(evidence: dict) -> dict[str, dict]:
    """key -> {doc_id, anchor, locator} from the candidate's cited facts."""
    idx: dict[str, dict] = {}
    for fact in (evidence.get("facts") or {}).get("facts") or []:
        cit = fact.get("citation") or {}
        if fact.get("field") and fact["field"] not in idx:
            idx[fact["field"]] = {k: cit[k] for k in ("doc_id", "anchor", "locator")
                                  if cit.get(k)}
    return idx


def write_clean_note(evidence: dict, out_path: Path) -> Path:
    """Write the reader-facing note (footnotes, no process text) to *out_path*.

    The marked report in report/ remains the verified record.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_clean(evidence.get("report_md") or "",
                     evidence_index_from_facts(evidence)), encoding="utf-8")
    return out_path


def build_dossier(case_dir: Path, out_dir: Path | None = None,
                  compile_pdf: bool = True) -> dict:
    """Render + compile; returns written paths and compilation status."""
    evidence = collect_case(Path(case_dir))
    out_dir = Path(out_dir) if out_dir else evidence["case_dir"] / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / "dossier.tex"
    tex_path.write_text(render_dossier_tex(evidence), encoding="utf-8")

    clean_path = write_clean_note(evidence, out_dir / "note.clean.md")
    pdf_path = out_dir / "dossier.pdf"
    compiled = False
    if compile_pdf and shutil.which("pdflatex"):
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             "-output-directory", str(out_dir), str(tex_path)],
            capture_output=True, text=True, timeout=120)
        compiled = proc.returncode == 0 and pdf_path.exists()
        if not compiled:
            (out_dir / "dossier_pdflatex.log").write_text(
                proc.stdout[-4000:] + "\n---STDERR---\n" + proc.stderr[-2000:],
                encoding="utf-8")
    return {"evidence_case": str(evidence["case_dir"]), "tex": str(tex_path),
            "clean_note": str(clean_path),
            "pdf": str(pdf_path) if compiled else None, "compiled": compiled,
            "report_version": evidence["report_version"]}


# -------------------------------------------------------------- contract
def export_dossier_pdf(report_md: Path, evidence: dict, out: Path) -> Path:
    """Contract entry (interfaces section 4): render + compile to *out*.

    evidence keys: model, valuation, mandate, manifest_path, candidate_manifest,
    approval, state, result (as produced by collect_case).
    """
    ev = dict(evidence)
    ev["report_md"] = Path(report_md).read_text(encoding="utf-8")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".tex").write_text(render_dossier_tex(ev), encoding="utf-8")
    if shutil.which("pdflatex"):
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             "-output-directory", str(out.parent), str(out.with_suffix(".tex"))],
            capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and out.exists():
            return out
    # fallback: the .tex is the deliverable when pdflatex is unavailable
    return out.with_suffix(".tex")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", required=True, help="case directory "
                    "(e.g. cases/intc-fy2026q3-20261030)")
    ap.add_argument("--out-dir", default=None, help="output directory "
                    "(default: <case>/export)")
    ap.add_argument("--no-pdf", action="store_true",
                    help="render dossier.tex only (skip pdflatex)")
    args = ap.parse_args(argv)
    out = build_dossier(Path(args.case), out_dir=Path(args.out_dir)
                        if args.out_dir else None,
                        compile_pdf=not args.no_pdf)
    print(json.dumps(out, indent=2))
    return 0 if (out["pdf"] or not args.no_pdf) else 0


if __name__ == "__main__":
    raise SystemExit(main())
