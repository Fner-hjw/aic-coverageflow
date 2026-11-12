"""LaTeX renderer for the page-1 cover of the redesigned note.

Reads only a ``report_doc`` dict (see ``report_doc.build_report_doc``) and
fills ``references/note-template.tex``.  Compilation lives in
``report_doc.build_cover``.
"""

from __future__ import annotations

from pathlib import Path

from .export_dossier import _tex_escape
from .report_doc import MINUS, format_refs

TEMPLATE = Path(__file__).resolve().parent.parent / "references" / "note-template.tex"


def tx(text) -> str:
    """Escape for LaTeX; keep Unicode minus/dash/dot (utf8 inputenc) usable."""
    s = _tex_escape("" if text is None else str(text))
    s = s.replace("USD ", "USD~").replace(MINUS, r"\textminus{}")
    return s.replace("–", "--").replace("—", "---").replace("×", r"\texttimes{}")


def _sentence(s: dict) -> str:
    text = tx(s["text"])
    if s["refs"]:
        cl = r"\cite{" + tx(format_refs(s["refs"])) + "}"
        text = (text[:-1] + cl + text[-1]) if text and text[-1] in ".;" else text + cl
    return text


def _kp(doc: dict) -> str:
    return "\n".join(rf"\item {{\sffamily\fontsize{{9}}{{11.5}}\selectfont\bfseries\color{{navy}}{tx(k['label'])}\ }}"
                     rf"{{\fontsize{{9.5}}{{12.2}}\selectfont {tx(k['text'])}}}" for k in doc["key_points"])


def _summary(doc: dict) -> str:
    return "\n\n\\vspace{5pt}\n".join(" ".join(_sentence(s) for s in p) for p in doc["summary"])


def _sources(doc: dict) -> str:
    if not doc["citations"]:
        return ""
    groups: dict[str, list[int]] = {}
    for c in doc["citations"]:
        groups.setdefault(c["group"], []).append(c["n"])
    parts = [rf"\textbf{{{tx(format_refs(ns))}}}\,{tx(g)}" for g, ns in groups.items()]
    return (r"{\RaggedRight\sffamily\fontsize{6.6}{8.2}\selectfont\color{mute}"
            r"\textbf{Sources.} " + "; ".join(parts) + r".\par}")


def _status(doc: dict) -> str:
    st = doc["status"]
    ex = st["exceptions"]
    if ex:
        n = len(ex)
        body = (rf"\textbf{{{tx(st['label'])}}} \textemdash{{}} {n} open finding{'s' if n != 1 else ''}: "
                + "; ".join(rf"\textbf{{{tx(e['id'])}}} ({tx(e['severity'])}) {tx(e['short'])}" for e in ex)
                + ".")
    else:
        body = rf"\textbf{{{tx(st['label'])}}}"
    return (r"{\color{hair}\fboxrule 0.4pt\fboxsep 5pt\fbox{{\color{ink}\parbox{\dimexpr\linewidth-10pt-0.8pt\relax}"
            r"{\RaggedRight\sffamily\fontsize{7.8}{9.8}\selectfont " + body + r"}}}}")


def _sidebar(doc: dict) -> str:
    r, out = doc["rating"], []
    out.append(r"{\sffamily\fontsize{7.5}{9}\selectfont\bfseries\color{navy}\textls[90]{RATING}}\par")
    out.append(rf"{{\sffamily\fontsize{{25}}{{27}}\selectfont\bfseries\color{{navy}}{tx(r['rating'] or 'n/a')}}}\par")
    out.append(r"\vspace{2pt}{\color{navy}\rule{\linewidth}{0.6pt}}\par\vspace{3pt}")
    out.append(rf"\kv{{Price target}}{{{tx(r['price_target'])}}}{{{tx(r['basis'])}}}")
    out.append(rf"\kv{{Price}}{{{tx(r['price'])}}}{{close, {tx(r['price_as_of'])}}}")
    out.append(rf"\kv{{Implied return}}{{{tx(r['implied_return'])}}}{{}}")
    out.append(r"\sech{Key data}")
    for k in doc["key_data"]:
        note = tx(k["note"]) if k["note"] else ""
        out.append(rf"\kv{{{tx(k['label'])}}}{{{tx(k['value'])}}}{{{note}}}")
    if doc.get("valuation_methods"):
        out.append(r"\sech{DCF value per share}")
        for k in doc["valuation_methods"]:
            out.append(rf"\kv{{{tx(k['label'])}}}{{{tx(k['value'])}}}{{{tx(k['note'])}}}")
    fs = doc["financial_summary"]
    out.append(r"\sech{Financial summary}")
    out.append(r"{\sffamily\fontsize{6.5}{8}\selectfont\color{mute}USD mm unless stated; EPS in USD; fiscal year\par}\vspace{2pt}")
    n = len(fs["columns"])
    spec = r"@{}p{18.5mm}" + "".join(r">{\raggedleft\arraybackslash}p{8.3mm}" for _ in range(n)) + r"@{}"
    spec = r"@{}l" + "".join(r"@{\extracolsep{\fill}}r" for _ in range(n)) + r"@{}"
    head = " & ".join(rf"\textbf{{{c['label'].replace('FY20', 'FY')}{c['kind']}}}" for c in fs["columns"])
    rule = r"\arrayrulecolor{%s}\specialrule{%s}{0pt}{0pt}"
    lines = [r"{\sffamily\fontsize{7}{9}\selectfont\setlength{\tabcolsep}{0pt}\renewcommand{\arraystretch}{1.28}",
             rf"\begin{{tabular*}}{{\linewidth}}{{{spec}}}",
             rf" & {head}\\" + rule % ("navy", "0.6pt")]
    for r_ in fs["rows"]:
        vals = " & ".join(tx(v) if v is not None else r"\textendash" for v in r_["values"])
        lines.append(rf"{tx(r_['label'])} & {vals}\\" + rule % ("hair", "0.3pt"))
    lines += [r"\end{tabular*}}", r"\par\vspace{3pt}"]
    for note in fs["notes"]:
        lines.append(rf"{{\sffamily\fontsize{{6.3}}{{7.6}}\selectfont\color{{mute}}{tx(note)}\par}}")
    out += lines
    return "\n".join(out)


def _figure(doc: dict, chart_file: str | None) -> str:
    if not doc.get("chart") or not chart_file:
        return ""
    c = doc["chart"]
    return (r"\vspace{9pt}\figtitle{Figure 1: " + tx(c["title"]) + r" (" + tx(c["unit"]) + r")}"
            r"\noindent\includegraphics[width=\linewidth]{" + chart_file + r"}\par\vspace{1pt}"
            r"{\RaggedRight\sffamily\fontsize{6.3}{7.6}\selectfont\color{mute}" + tx(c["source"]) + r"\par}")


def render_cover_tex(doc: dict, chart_file: str | None = None) -> str:
    m = doc["meta"]
    toks = {
        "DRAFT_FLAG": r"\drafttrue" if m["draft"] else r"\draftfalse",
        "HEADER_LEFT": tx(f"CoverageFlow Research  |  {m['doc_type']}  |  {m['date']}"),
        "TICKER": tx(m["ticker"]),
        "FOOTER_STATUS": tx(f"{doc['status']['label']}  ·  {m['report_version']}  ·  information cutoff {m['cutoff']}"),
        "COLUMN_HEIGHT": "229mm",
        "KICKER": tx(m["kicker"]),
        "HEADLINE": tx(doc["headline"]),
        "BYLINE": tx(doc["byline"]["text"]),
        "KEY_POINTS": _kp(doc),
        "SUMMARY": _summary(doc),
        "FIGURE": _figure(doc, chart_file),
        "SOURCES": _sources(doc),
        "STATUS_BOX": _status(doc),
        "SIDEBAR": _sidebar(doc),
        "DISCLAIMER": tx(doc["disclaimer"]),
    }
    t = TEMPLATE.read_text(encoding="utf-8")
    for k, v in toks.items():
        t = t.replace("{{" + k + "}}", v)
    return t
