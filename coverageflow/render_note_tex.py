"""LaTeX renderer for the redesigned research note (cover, body, controls page).

Reads only a ``report_doc`` dict (see ``report_doc.build_report_doc``) and
fills ``references/note-template.tex``.  Compilation lives in
``note_build.build_note``.
"""

from __future__ import annotations

from pathlib import Path

from .report_doc import MINUS, format_refs

TEMPLATE = Path(__file__).resolve().parent.parent / "references" / "note-template.tex"

_ESC = (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
        ("_", r"\_"), ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}"))


def tx(text) -> str:
    """Escape for LaTeX; Unicode minus, dagger and times become TeX macros."""
    s = "" if text is None else str(text)
    out = []
    for ch in s:
        for a, b in _ESC:
            if ch == a:
                ch = b
                break
        out.append(ch)
    s = "".join(out)
    s = s.replace("USD ", "USD~").replace(MINUS, r"\textminus{}")
    s = s.replace("†", r"\textsuperscript{\textdagger}").replace("×", r"\texttimes{}")
    return s.replace("–", "--")


# ----------------------------------------------------------------- prose
def _sentence(s: dict) -> str:
    text = tx(s["text"])
    if s["refs"]:
        cl = r"\cite{" + tx(format_refs(s["refs"])) + "}"
        text = (text[:-1] + cl + text[-1]) if text and text[-1] in ".;" else text + cl
    return text


def _para(sents: list[dict]) -> str:
    return " ".join(_sentence(s) for s in sents)


def _prose(blocks: list[dict]) -> str:
    """Section prose in two balanced columns."""
    if not blocks:
        return ""
    parts = []
    for b in blocks:
        if "sentences" in b:
            parts.append(_para(b["sentences"]))
        elif "items" in b:
            items = "\n".join(r"\item " + _para(it) for it in b["items"])
            parts.append(r"\begin{itemize}[leftmargin=9pt,labelwidth=5pt,labelsep=4pt,itemsep=2pt,parsep=0pt,topsep=0pt,"
                         r"label={\raisebox{0.25ex}{\rule{3pt}{3pt}}}]" + "\n" + items + "\n" + r"\end{itemize}")
        elif "text" in b:  # h3
            parts.append(r"{\sffamily\fontsize{9}{11}\selectfont\bfseries " + tx(b["text"]) + r"\par}\nopagebreak")
        elif "header" in b:
            parts.append(r"\noindent" + _tabular({"header": b["header"], "rows": [{"cells": r, "style": ""} for r in b["rows"]],
                                                  "align": ["l"] + ["r"] * (len(b["header"]) - 1)}, "half"))
    return (r"\begin{multicols}{2}" + "\n" + "\n\n".join(parts) + "\n" + r"\end{multicols}")


# ---------------------------------------------------------------- tables
def _hdr(label: str) -> str:
    if len(label) <= 9 or " " not in label:
        return rf"\textbf{{{tx(label)}}}"
    words = label.split(" ")
    best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return (r"\begin{tabular}[b]{@{}r@{}}\textbf{" + tx(" ".join(words[:best])) + r"}\\\textbf{"
            + tx(" ".join(words[best:])) + r"}\end{tabular}")


def _tabular(t: dict, width: str) -> str:
    n = len(t["header"])
    full = width == "full"
    first = max(17, min(60, 87 - 15 * (n - 1))) if not full else max(28, min(60, 174 - 17 * (n - 1)))
    spec = rf">{{\RaggedRight}}p{{{first}mm}}" + "".join(r"@{\extracolsep{\fill}}r" for _ in range(n - 1))
    rule = r"\arrayrulecolor{%s}\specialrule{%s}{0pt}{0pt}"
    lines = [r"{\setlength{\tabcolsep}{1.5pt}\renewcommand{\arraystretch}{1.2}\RaggedRight",
             rf"\begin{{tabular*}}{{\linewidth}}{{@{{}}{spec}@{{}}}}",
             " & ".join(_hdr(h) if h else "" for h in t["header"]) + r"\\" + rule % ("accent", "0.6pt")]
    for r in t["rows"]:
        cells = []
        for i, c in enumerate(r["cells"]):
            s = tx(c)
            if r.get("style") == "total":
                s = rf"\textbf{{{s}}}"
            elif r.get("style") == "sub":
                s = rf"\textcolor{{mute}}{{{s}}}"
            if i in (r.get("box") or []):
                s = rf"\setlength{{\fboxsep}}{{1.2pt}}\setlength{{\fboxrule}}{{0.6pt}}\fcolorbox{{accent}}{{white}}{{\strut {s}}}"
            cells.append(s)
        lines.append(" & ".join(cells) + r"\\" + rule % ("hair", "0.3pt"))
    lines.append(r"\end{tabular*}}")
    return "\n".join(lines)


def _exhibit(e: dict, width: str) -> str:
    """One exhibit in a non-breaking minipage; width is 'full' or 'half'."""
    w = r"\linewidth" if width == "full" else r"0.485\linewidth"
    out = [rf"\begin{{minipage}}[t]{{{w}}}", r"\sffamily\fontsize{7.5}{9.5}\selectfont",
           rf"{{\RaggedRight\bfseries\color{{accent}}{tx(e['id'])}\hspace{{0.7em}}\color{{ink}}{tx(e['title'])}\par}}",
           rf"{{\color{{mute}}\fontsize{{6.5}}{{8}}\selectfont {tx(e.get('unit', ''))}\par}}\hrl{{2pt}}{{accent}}{{0.6pt}}\vspace{{1pt}}"]
    if e.get("table"):
        out.append(_tabular(e["table"], width))
    elif e.get("rows") is not None:  # football
        out.append(r"\vspace{2pt}\noindent\includegraphics[width=\linewidth]{" + e["_file"] + "}")
    out.append(r"\par\vspace{2pt}{\RaggedRight\color{mute}\fontsize{6.4}{7.8}\selectfont")
    for n in e.get("notes", []):
        out.append(tx(n) + r"\par")
    out.append(tx(e.get("source", "")) + r"\par}")
    out.append(r"\end{minipage}")
    return "\n".join(out)


# ----------------------------------------------------------------- cover
def _kp(doc: dict) -> str:
    return "\n".join(rf"\item {{\sffamily\fontsize{{9}}{{11.5}}\selectfont\bfseries\color{{accent}}{tx(k['label'])}\ }}"
                     rf"{{\fontsize{{10}}{{12.6}}\selectfont {tx(k['text'])}}}" for k in doc["key_points"])


def _summary(doc: dict) -> str:
    return "\n\n\\vspace{5pt}\n".join(_para(p) for p in doc["summary"])


def _sources(doc: dict) -> str:
    cites = [c for c in doc["citations"] if c.get("cover")]
    if not cites:
        return ""
    groups: dict[str, list[int]] = {}
    for c in cites:
        groups.setdefault(c["group"], []).append(c["n"])
    parts = [rf"\textbf{{{tx(format_refs(ns))}}}\,{tx(g)}" for g, ns in groups.items()]
    return (r"{\RaggedRight\sffamily\fontsize{6.6}{8.2}\selectfont\color{mute}"
            r"\textbf{Sources.} " + "; ".join(parts) + r".\par}")


def _status(doc: dict) -> str:
    st = doc["status"]
    ex = st["exceptions"]
    if ex:
        n = len(ex)
        body = (rf"\textbf{{{tx(st['label'])}}}: {n} open finding{'s' if n != 1 else ''}, "
                + "; ".join(rf"\textbf{{{tx(e['id'])}}} ({tx(e['severity'])}): {tx(e['short'])}" for e in ex) + ".")
    else:
        body = rf"\textbf{{{tx(st['label'])}}}"
    return (r"{\color{hair}\fboxrule 0.4pt\fboxsep 5pt\fbox{{\color{ink}\parbox{\dimexpr\linewidth-10pt-0.8pt\relax}"
            r"{\RaggedRight\sffamily\fontsize{7.8}{9.8}\selectfont " + body + r"}}}}")


def _sidebar(doc: dict) -> str:
    r, out = doc["rating"], []
    out.append(r"{\sffamily\fontsize{7.5}{9}\selectfont\bfseries\color{accent}\textls[90]{RATING}}\par")
    out.append(rf"{{\sffamily\fontsize{{25}}{{27}}\selectfont\bfseries\color{{ink}}{tx(r['rating'] or 'n/a')}}}\par")
    out.append(r"\vspace{2pt}{\color{accent}\rule{\linewidth}{0.6pt}}\par\vspace{3pt}")
    out.append(rf"\kv{{Price target}}{{{tx(r['price_target'])}}}{{{tx(r['basis'])}}}")
    out.append(rf"\kv{{Price}}{{{tx(r['price'])}}}{{close, {tx(r['price_as_of'])}}}")
    out.append(rf"\kv{{Implied return}}{{{tx(r['implied_return'])}}}{{}}")
    out.append(r"\sech{Key data}")
    for k in doc["key_data"]:
        out.append(rf"\kv{{{tx(k['label'])}}}{{{tx(k['value'])}}}{{{tx(k['note']) if k['note'] else ''}}}")
    if doc.get("valuation_methods"):
        out.append(r"\sech{DCF value per share}")
        for k in doc["valuation_methods"]:
            out.append(rf"\kv{{{tx(k['label'])}}}{{{tx(k['value'])}}}{{{tx(k['note'])}}}")
    fs = doc["financial_summary"]
    if not fs["columns"] or not fs["rows"]:
        return "\n".join(out)
    out.append(r"\sech{Financial summary}")
    out.append(r"{\sffamily\fontsize{6.5}{8}\selectfont\color{mute}USD mm unless stated; EPS in USD; fiscal year\par}\vspace{2pt}")
    n = len(fs["columns"])
    spec = r"@{}l" + "".join(r"@{\extracolsep{\fill}}r" for _ in range(n)) + r"@{}"
    head = " & ".join(rf"\textbf{{{c['label'].replace('FY20', 'FY')}{c['kind']}}}" for c in fs["columns"])
    rule = r"\arrayrulecolor{%s}\specialrule{%s}{0pt}{0pt}"
    out += [r"{\sffamily\fontsize{7}{9}\selectfont\setlength{\tabcolsep}{0pt}\renewcommand{\arraystretch}{1.28}",
            rf"\begin{{tabular*}}{{\linewidth}}{{{spec}}}",
            rf" & {head}\\" + rule % ("accent", "0.6pt")]
    for r_ in fs["rows"]:
        vals = " & ".join(tx(v) if v is not None else "n.a." for v in r_["values"])
        out.append(rf"{tx(r_['label'])} & {vals}\\" + rule % ("hair", "0.3pt"))
    out += [r"\end{tabular*}}", r"\par\vspace{3pt}"]
    for note in fs["notes"]:
        out.append(rf"{{\sffamily\fontsize{{6.3}}{{7.6}}\selectfont\color{{mute}}{tx(note)}\par}}")
    return "\n".join(out)


def _cover_figure(doc: dict, figs: dict) -> str:
    c = doc.get("chart")
    if not c or "segments" not in figs:
        return ""
    return (r"\vspace{9pt}\figtitle{" + tx(c["id"]) + ": " + tx(c["title"]) + " (" + tx(c["unit"]) + ")}"
            r"\noindent\includegraphics[width=\linewidth]{" + figs["segments"] + r"}\par\vspace{1pt}"
            r"{\RaggedRight\sffamily\fontsize{6.3}{7.6}\selectfont\color{mute}" + tx(c["source"]) + r"\par}")


# ------------------------------------------------------------------ body
def _body(doc: dict, figs: dict) -> str:
    out: list[str] = []
    buf: list[dict] = []

    def flush():
        if buf:
            out.append(_prose(buf))
            buf.clear()

    for b in doc["body"]:
        t = b["type"]
        if t == "prose":
            buf.append(b)
            continue
        flush()
        if t == "h2":
            out.append(r"\sectitle{" + tx(b["text"]) + "}")
        elif t == "exhibit":
            out.append(r"\par\vspace{9pt}\noindent" + _exhibit(b, b["width"]) + r"\par")
        elif t == "exhibit_row":
            a, c = b["exhibits"]
            out.append(r"\par\vspace{9pt}\noindent" + _exhibit(a, "half") + r"\hfill" + _exhibit(c, "half") + r"\par")
        elif t == "football" and figs.get("football"):
            out.append(r"\par\vspace{9pt}\noindent" + _exhibit({**b, "_file": figs["football"]}, "full") + r"\par")
    flush()
    return "\n".join(out)


# -------------------------------------------------------------- controls
def _ptable(header: list[str], rows: list[list[str]], widths: list) -> str:
    spec = "".join(rf">{{\RaggedRight\arraybackslash}}p{{{w}mm}}" if w else r">{\RaggedRight\arraybackslash}X" for w in widths)
    rule = r"\arrayrulecolor{%s}\specialrule{%s}{0pt}{0pt}"
    lines = [r"{\setlength{\tabcolsep}{2pt}\renewcommand{\arraystretch}{1.2}\sffamily\fontsize{7}{8.6}\selectfont",
             rf"\begin{{tabularx}}{{\linewidth}}{{@{{}}{spec}@{{}}}}",
             " & ".join(rf"\textbf{{{tx(h)}}}" for h in header) + r"\\" + rule % ("accent", "0.6pt")]
    for r in rows:
        lines.append(" & ".join(tx(c) for c in r) + r"\\" + rule % ("hair", "0.3pt"))
    lines.append(r"\end{tabularx}}")
    return "\n".join(lines)


def _controls(doc: dict) -> str:
    c = doc["controls"]
    out = [r"\clearpage", r"\sectitle{Sources and controls}"]

    def sub(title):
        return rf"\par\vspace{{7pt}}{{\sffamily\fontsize{{7.5}}{{9}}\selectfont\bfseries\color{{accent}}\textls[90]{{{tx(title.upper())}}}\par}}\vspace{{2pt}}"

    if c["inputs"]:
        out.append(sub("Input documents"))
        out.append(_ptable(["Document", "Form", "Period ended", "Filed", "SHA-256 (first 12)"],
                           [[d["doc_id"], d["form"], d["period"], d["filed"], d["sha"]] for d in c["inputs"]],
                           [None, 14, 24, 22, 30]))
    if c["evidence"]:
        out.append(sub("Evidence references"))
        out.append(_ptable(["Ref.", "Source", "Statement", "Items cited"],
                           [[g["refs_text"], g["source"], g["statement"], g["items_text"]] for g in c["evidence"]],
                           [14, 52, 40, None]))
    if c["decisions"]:
        out.append(sub("Decision record"))
        out.append(_ptable(["Record", "Decision", "By", "Date", "Reasoning"],
                           [[d["id"], d["decision"], d["by"], d["date"], d["reasoning"]] for d in c["decisions"]],
                           [17, 44, 34, 20, None]))
    if c["exceptions"]:
        out.append(sub("Open findings at publication"))
        out.append(_ptable(["Finding", "Severity", "Summary"],
                           [[e["id"], e["severity"], e["summary"]] for e in c["exceptions"]], [17, 16, None]))
    out.append(sub("Provenance"))
    pv = [[p["label"], p["value"]] for p in c["provenance"]]
    half = (len(pv) + 1) // 2
    prow = [pv[i] + (pv[i + half] if i + half < len(pv) else ["", ""]) for i in range(half)]
    out.append(_ptable(["Item", "Value", "Item", "Value"], prow, [30, None, 30, None]))
    out.append(r"\par\vspace{10pt}{\RaggedRight\sffamily\fontsize{6.8}{8.4}\selectfont\color{mute}" + tx(doc["disclaimer"]) + r"\par}")
    return "\n".join(out)


def render_note_tex(doc: dict, figs: dict | None = None, cover_only: bool = False) -> str:
    """Fill the template. ``figs`` maps chart name ('segments', 'football') to a PDF path."""
    figs = figs or {}
    m = doc["meta"]
    body = ""
    if not cover_only:
        body = r"\clearpage" + "\n" + _body(doc, figs)
        if doc.get("controls"):
            body += "\n" + _controls(doc)
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
        "FIGURE": _cover_figure(doc, figs),
        "SOURCES": _sources(doc),
        "STATUS_BOX": _status(doc),
        "SIDEBAR": _sidebar(doc),
        "DISCLAIMER": tx(doc["disclaimer"]),
        "BODY": body,
    }
    t = TEMPLATE.read_text(encoding="utf-8")
    for k, v in toks.items():
        t = t.replace("{{" + k + "}}", v)
    return t


render_cover_tex = render_note_tex  # legacy name
