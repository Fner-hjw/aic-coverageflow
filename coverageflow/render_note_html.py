"""Self-contained HTML renderer for the redesigned research note.

Reads only a ``report_doc`` dict (``report_doc.build_report_doc``).  One file: inline
CSS and inline SVG charts, no scripts, no external requests, no web fonts
(Charter / Georgia for prose, system sans for data).  ``@media print`` lays
out A4 pages like the PDF (running header and footer as page margin boxes);
below 760px the columns collapse.  The draft watermark is a fixed overlay, so
it repeats on every printed page.
"""

from __future__ import annotations

import re
from html import escape

from .report_doc import format_refs

CSS = """
:root{--accent:#1a8a4d;--ink:#1c2733;--mute:#51606f;--hair:#cfd8df;--bg:#fff}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#eceeef;color:var(--ink);
  font-family:Charter,'Bitstream Charter','Sitka Text',Georgia,serif;font-size:16px;line-height:1.5}
.sans{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
  font-variant-numeric:lining-nums tabular-nums}
.page{position:relative;max-width:210mm;margin:0 auto;background:var(--bg);padding:0 14mm 12mm;min-height:297mm}
.draft::before{content:'DRAFT';position:fixed;left:50%;top:50%;transform:translate(-50%,-50%) rotate(-54deg);
  font:700 190px/1 -apple-system,'Helvetica Neue',Arial,sans-serif;color:rgba(0,0,0,.07);pointer-events:none;z-index:1000}
.run{display:flex;justify-content:space-between;align-items:baseline;padding:14mm 0 6px;
  border-bottom:.5px solid var(--hair);font-size:11px;color:var(--mute)}
.run b{color:var(--accent);font-size:12px}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 292px;column-gap:0;margin-top:8mm}
.main{padding-right:5.5mm;border-right:.5px solid var(--hair)}
.side{padding-left:5.5mm}
.kicker{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--accent)}
h1{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-weight:700;
  font-size:27px;line-height:1.16;margin:4px 0 8px;letter-spacing:-.005em}
.byline{font-size:11px;color:var(--mute);padding-bottom:6px;border-bottom:.5px solid var(--hair)}
ul.kp{list-style:none;margin:12px 0 0;padding:0}
ul.kp li{position:relative;padding-left:13px;margin:0 0 5px;font-size:14.5px;line-height:1.4}
ul.kp li::before{content:'';position:absolute;left:0;top:.52em;width:4.5px;height:4.5px;background:var(--accent)}
ul.kp b{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;font-size:13px;color:var(--accent);margin-right:.3em}
h2{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-size:10.5px;font-weight:700;
  letter-spacing:.09em;text-transform:uppercase;color:var(--accent);margin:16px 0 6px;
  padding-bottom:2px;border-bottom:1px solid var(--accent)}
.summary p{margin:0 0 7px;hyphens:auto;-webkit-hyphens:auto;text-align:left}
sup.c{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;font-size:.62em;line-height:0;vertical-align:super}
sup.c a{color:var(--accent);text-decoration:none}
sup.c a:hover{text-decoration:underline}
figure.fig{margin:16px 0 8px}
figure.fig figcaption{font-size:11px;font-weight:700;color:var(--accent);margin-bottom:2px}
figure.fig svg{width:100%;height:auto;display:block}
figure.fig svg text{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif!important;font-variant-numeric:lining-nums tabular-nums}
figure.fig .src{font-size:10px;color:var(--mute);margin:2px 0 0}
.status{margin-top:16px;border:.5px solid var(--hair);padding:6px 9px;font-size:12px;line-height:1.4}
.rating{font-size:38px;font-weight:700;color:var(--ink);line-height:1.05;margin:0 0 6px;
  padding-bottom:5px;border-bottom:1px solid var(--accent)}
.lab{font-size:10.5px;font-weight:700;letter-spacing:.09em;color:var(--accent)}
dl.kv{margin:0}
dl.kv .r{display:flex;justify-content:space-between;align-items:baseline;border-bottom:.5px solid var(--hair);
  padding:4px 0 3px;font-size:12.5px;flex-wrap:wrap}
dl.kv dt{color:var(--mute)}
dl.kv dd{margin:0;font-weight:700;text-align:right}
dl.kv .nt{flex-basis:100%;text-align:right;font-size:10px;color:var(--mute);line-height:1.25}
div.fsw{overflow-x:auto}
table.fs{border-collapse:collapse;width:100%;table-layout:fixed;font-size:10px}
table.fs col.l{width:27%}
table.fs th{font-weight:700;text-align:right;padding:0 0 3px 2px;white-space:nowrap;border-bottom:1px solid var(--accent)}
table.fs td{text-align:right;padding:2.5px 0 2.5px 2px;white-space:nowrap;border-bottom:.5px solid var(--hair)}
table.fs th:first-child,table.fs td:first-child{text-align:left;font-weight:400;padding-left:0}
table.fs td .u{font-size:9px;color:var(--mute)}
.fsn{font-size:9.5px;color:var(--mute);margin:4px 0 0;line-height:1.35}
.srcline{font-size:10.5px;line-height:1.45;color:var(--mute);margin:4px 0 0}
.sub{font-size:10px;color:var(--mute);margin:0 0 3px}
.disc{margin-top:10mm;font-size:9.5px;color:var(--mute);line-height:1.4}
.foot{display:flex;justify-content:space-between;font-size:10px;color:var(--mute);margin-top:6px;
  border-top:.5px solid var(--hair);padding-top:4px}
@media (max-width:760px){
  .page{padding:0 16px 24px}.run{padding-top:14px}
  .cols{grid-template-columns:1fr;margin-top:18px}
  .main{padding-right:0;border-right:0}
  table.fs{min-width:300px}
  .side{padding-left:0;margin-top:20px;border-top:.5px solid var(--hair);padding-top:14px}
  h1{font-size:23px}
}

/* ---- body: sections, prose, exhibits, controls */
h2.sec{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-size:19px;font-weight:700;
  letter-spacing:0;text-transform:none;color:var(--ink);margin:30px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--accent);break-after:avoid}
h3.sub3{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;font-size:14px;margin:10px 0 2px;break-after:avoid}
.prose{column-count:2;column-gap:7mm;column-rule:.5px solid var(--hair);font-size:15px;line-height:1.45}
.prose p{margin:0 0 7px;hyphens:auto;-webkit-hyphens:auto}
.prose ul{margin:0 0 7px;padding-left:14px}
.prose li{margin:0 0 3px}
figure.ex,.controls,table.xt{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-variant-numeric:lining-nums tabular-nums}
figure.ex{margin:22px 0 0;break-inside:avoid}
figure.ex figcaption{font-size:12px;line-height:1.3;padding-bottom:3px;border-bottom:1px solid var(--accent);margin-bottom:2px}
figure.ex figcaption b{color:var(--accent);margin-right:.7em}
figure.ex figcaption b+span{font-weight:700}
figure.ex .unit{font-size:10.5px;color:var(--mute);display:block;font-weight:400}
figure.ex .exn,figure.ex .src{font-size:10.5px;color:var(--mute);margin:3px 0 0;line-height:1.35}
figure.ex svg{width:100%;height:auto;display:block}
figure.ex svg text{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif!important;font-variant-numeric:lining-nums tabular-nums}
.exrow>*{min-width:0}
.exrow{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);column-gap:7mm;align-items:start}
.tw{overflow-x:auto}
table.xt{border-collapse:collapse;width:100%;font-size:12px}
table.xt th{font-weight:700;text-align:right;padding:3px 0 3px 8px;vertical-align:bottom;border-bottom:1px solid var(--accent);line-height:1.2}
table.xt td{text-align:right;padding:3px 0 3px 8px;border-bottom:.5px solid var(--hair);white-space:nowrap}
table.xt th:first-child,table.xt td:first-child{text-align:left;padding-left:0;white-space:normal}
table.xt tr.total td{font-weight:700}
table.xt tr.subr td{color:var(--mute)}
table.xt td.box{outline:1px solid var(--accent);outline-offset:-1px}
.dg{font-size:.8em;vertical-align:super;line-height:0}
table.ct{border-collapse:collapse;width:100%;font-size:11.5px;line-height:1.35}
table.ct th{font-weight:700;text-align:left;padding:3px 8px 3px 0;border-bottom:1px solid var(--accent)}
table.ct td{text-align:left;padding:3px 8px 3px 0;border-bottom:.5px solid var(--hair);vertical-align:top}
h3.ch{font-size:10.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);margin:20px 0 4px}
.controls{margin-top:30px}
.controls td:target,.controls td:has(:target){background:#f1f6f3}

@media (max-width:760px){
  .prose{column-count:1}
  .exrow{grid-template-columns:minmax(0,1fr);row-gap:0}
  h2.sec{font-size:17px}
}

@media print{
  @page{size:A4;margin:16mm 18mm 18mm;
    @top-left{content:var(--hdr);font:7.5pt -apple-system,'Helvetica Neue',Arial,sans-serif;color:#51606f;border-bottom:.4pt solid #cfd8df;padding-bottom:2mm}
    @top-right{content:var(--tkr);font:700 8pt -apple-system,'Helvetica Neue',Arial,sans-serif;color:#1a8a4d;border-bottom:.4pt solid #cfd8df;padding-bottom:2mm}
    @bottom-left{content:var(--ftr);font:7pt -apple-system,'Helvetica Neue',Arial,sans-serif;color:#51606f}
    @bottom-right{content:counter(page) " / " counter(pages);font:7pt -apple-system,'Helvetica Neue',Arial,sans-serif;color:#51606f}}
  html,body{background:#fff}
  body{font-size:10pt;line-height:1.3}
  .page{max-width:none;margin:0;padding:0;min-height:0}
  .run,.foot{display:none}
  .cover{break-after:page}
  .cols{grid-template-columns:minmax(0,1fr) 68mm;margin-top:0}
  table.fs th,table.fs td{padding-left:1px}
  table.fs col.l{width:26%}
  .srcline{font-size:6.6pt;line-height:1.25}
  h1{font-size:19pt}.kicker{font-size:8pt}.byline{font-size:7.5pt}
  ul.kp li{font-size:10pt}ul.kp b{font-size:9pt}
  h2{font-size:7.5pt}.lab{font-size:7.5pt}
  .rating{font-size:25pt}dl.kv .r{font-size:8.5pt}dl.kv .nt{font-size:6.5pt}
  table.fs{font-size:6.2pt}.fsn,.sub{font-size:6.3pt}
  figure.fig figcaption{font-size:7.5pt}figure.fig .src{font-size:6.3pt}
  .status{font-size:7.8pt}.disc{font-size:6.3pt;margin-top:6mm}
  h2.sec{font-size:12pt;margin:12pt 0 6pt}
  h3.sub3{font-size:9pt}
  .prose{font-size:10pt;line-height:1.3}
  figure.ex{margin-top:9pt}
  figure.ex figcaption{font-size:7.5pt}figure.ex .unit{font-size:6.5pt}
  figure.ex .exn,figure.ex .src{font-size:6.4pt}
  table.xt{font-size:7.2pt}
  table.xt th,table.xt td{padding-top:1.5pt;padding-bottom:1.5pt}
  table.ct{font-size:7pt}h3.ch{font-size:7.5pt;margin-top:9pt}
  .controls{break-before:page;margin-top:0}
  .page a{color:inherit}
}
"""


def e(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def _inline_svg(svg: str) -> str:
    """The <svg> element only, without XML namespaces, metadata or URLs (self-contained HTML)."""
    t = svg[svg.index("<svg"):]
    t = re.sub(r"<metadata>.*?</metadata>", "", t, flags=re.S)
    t = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", t)
    return re.sub(r"xlink:href", "href", t)


def _css_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _cluster(label: str, titles: dict[int, str]) -> str:
    """Render '1–4, 7': each number links to its entry in the evidence table."""
    def link(n: int) -> str:
        return f'<a href="#n{n}" title="{e(titles.get(n, ""))}">{n}</a>'

    parts = []
    for chunk in label.split(", "):
        if "–" in chunk:
            a, b = (int(x) for x in chunk.split("–"))
            parts.append(f"{link(a)}–{link(b)}")
        else:
            parts.append(link(int(chunk)))
    return f'<sup class="c">{", ".join(parts)}</sup>'


def _sentence(s: dict, titles: dict[int, str]) -> str:
    text = e(s["text"])
    if s["refs"]:
        cl = _cluster(format_refs(s["refs"]), titles)
        text = (text[:-1] + cl + text[-1]) if text and text[-1] in ".;" else text + cl
    return text


def _para(sents: list[dict], titles: dict[int, str]) -> str:
    return " ".join(_sentence(s, titles) for s in sents)


def _cell(c: str) -> str:
    t = e(c)
    return t.replace("†", '<sup class="dg">†</sup>')


def _table(t: dict) -> str:
    head = "".join(f"<th>{e(h)}</th>" for h in t["header"])
    rows = []
    for r in t["rows"]:
        box = set(r.get("box") or [])
        tds = "".join(f'<td{" class=\"box\"" if i in box else ""}>{_cell(c)}</td>' for i, c in enumerate(r["cells"]))
        rows.append(f'<tr class="{"subr" if r.get("style") == "sub" else e(r.get("style", ""))}">{tds}</tr>')
    return f'<div class="tw"><table class="xt"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _exhibit(b: dict, charts: dict) -> str:
    cap = (f'<figcaption><b>{e(b["id"])}</b><span>{e(b["title"])}</span>'
           f'<span class="unit">{e(b.get("unit", ""))}</span></figcaption>')
    if b.get("table"):
        inner = _table(b["table"])
    else:
        svg = charts.get("football")
        inner = (f'<div role="img" aria-label="{e(b["title"])}">{_inline_svg(svg)}</div>' if svg else "")
    notes = "".join(f'<p class="exn">{e(n)}</p>' for n in b.get("notes", []))
    return f'<figure class="ex">{cap}{inner}{notes}<p class="src">{e(b.get("source", ""))}</p></figure>'


def _prose_blocks(blocks: list[dict], titles: dict[int, str]) -> str:
    out = []
    for b in blocks:
        k = b.get("kind")
        if k == "p":
            out.append(f"<p>{_para(b['sentences'], titles)}</p>")
        elif k == "ul":
            out.append("<ul>" + "".join(f"<li>{_para(it, titles)}</li>" for it in b["items"]) + "</ul>")
        elif k == "h3":
            out.append(f'<h3 class="sub3">{e(b["text"])}</h3>')
        elif k == "table":
            out.append(_table({"header": b["header"], "rows": [{"cells": r} for r in b["rows"]]}))
    return f'<div class="prose">{"".join(out)}</div>'


def _body(doc: dict, charts: dict, titles: dict[int, str]) -> str:
    out, buf = [], []

    def flush():
        if buf:
            out.append(_prose_blocks(buf, titles))
            buf.clear()

    for b in doc["body"]:
        t = b["type"]
        if t == "prose":
            buf.append(b)
            continue
        flush()
        if t == "h2":
            out.append(f'<h2 class="sec">{e(b["text"])}</h2>')
        elif t in ("exhibit", "football"):
            out.append(_exhibit(b, charts))
        elif t == "exhibit_row":
            out.append('<div class="exrow">' + "".join(_exhibit(x, charts) for x in b["exhibits"]) + "</div>")
    flush()
    return "".join(out)


def _ctable(header: list[str], rows: list[list[str]], raw_first: bool = False) -> str:
    th = "".join(f"<th>{e(h)}</th>" for h in header)
    tr = "".join("<tr>" + "".join(f"<td>{c if (raw_first and i == 0) else e(c)}</td>" for i, c in enumerate(r)) + "</tr>"
                 for r in rows)
    return f'<div class="tw"><table class="ct"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


def _controls(doc: dict) -> str:
    c = doc["controls"]
    out = ['<section class="controls"><h2 class="sec">Sources and controls</h2>']
    if c["inputs"]:
        out.append('<h3 class="ch">Input documents</h3>' + _ctable(
            ["Document", "Form", "Period ended", "Filed", "SHA-256 (first 12)"],
            [[d["doc_id"], d["form"], d["period"], d["filed"], d["sha"]] for d in c["inputs"]]))
    if c["evidence"]:
        rows = []
        for g in c["evidence"]:
            anchors = "".join(f'<span id="n{n}"></span>' for n in g["refs"])
            rows.append([anchors + e(g["refs_text"]), g["source"], g["statement"], g["items_text"]])
        out.append('<h3 class="ch">Evidence references</h3>' + _ctable(
            ["Ref.", "Source", "Statement", "Items cited"], rows, raw_first=True))
    if c["decisions"]:
        out.append('<h3 class="ch">Decision record</h3>' + _ctable(
            ["Record", "Decision", "By", "Date", "Reasoning"],
            [[d["id"], d["decision"], d["by"], d["date"], d["reasoning"]] for d in c["decisions"]]))
    if c["exceptions"]:
        out.append('<h3 class="ch">Open findings at publication</h3>' + _ctable(
            ["Finding", "Severity", "Summary"], [[x["id"], x["severity"], x["summary"]] for x in c["exceptions"]]))
    out.append('<h3 class="ch">Provenance</h3>' + _ctable(
        ["Item", "Value"], [[p["label"], p["value"]] for p in c["provenance"]]))
    out.append(f'<p class="disc sans">{e(doc["disclaimer"])}</p></section>')
    return "".join(out)


def render_note_html(doc: dict, charts: dict | None = None, cover_only: bool = False) -> str:
    """Render the whole note as one self-contained HTML page."""
    charts = charts or {}
    m, r = doc["meta"], doc["rating"]
    fs, st = doc["financial_summary"], doc["status"]
    titles = {c["n"]: c["source"] + (", " + c["locator"] if c["locator"] else "") for c in doc["citations"]}

    kp = "".join(f"<li><b>{e(k['label'])}</b>{e(k['text'])}</li>" for k in doc["key_points"])
    summary = "".join("<p>" + _para(p, titles) + "</p>" for p in doc["summary"])
    if st["exceptions"]:
        ex = "; ".join(f"<b>{e(x['id'])}</b> ({e(x['severity'])}): {e(x['short'])}" for x in st["exceptions"])
        n = st["n_open"]
        status = f"<b>{e(st['label'])}</b>: {n} open finding{'s' if n != 1 else ''}, {ex}."
    else:
        status = f"<b>{e(st['label'])}</b>"
    kd = "".join(
        f'<div class="r"><dt>{e(k["label"])}</dt><dd>{e(k["value"])}</dd>'
        + (f'<div class="nt">{e(k["note"])}</div>' if k["note"] else "") + "</div>" for k in doc["key_data"])
    vm = "".join(f'<div class="r"><dt>{e(k["label"])}</dt><dd>{e(k["value"])}</dd><div class="nt">{e(k["note"])}</div></div>'
                 for k in doc.get("valuation_methods", []))
    cols = "<col>" * len(fs["columns"])
    head = "".join(f"<th>{e(c['label'].replace('FY20', 'FY'))}{c['kind']}</th>" for c in fs["columns"])
    rows = "".join(
        f"<tr><td>{e(x['label'])}</td>" + "".join(f"<td>{_cell(v) if v is not None else 'n.a.'}</td>" for v in x["values"]) + "</tr>"
        for x in fs["rows"])
    fnotes = "".join(f"<p class='fsn'>{_cell(n)}</p>" for n in fs["notes"])
    fin_html = ""
    if fs["columns"] and fs["rows"]:
        fin_html = ('<h2>Financial summary</h2>\n<p class="sub">USD mm unless stated; EPS in USD; fiscal year</p>\n'
                    f'<div class="fsw"><table class="fs"><colgroup><col class="l">{cols}</colgroup>'
                    f'<thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table></div>\n{fnotes}')
    fig = ""
    if doc.get("chart") and charts.get("segments"):
        c = doc["chart"]
        svg = charts["segments"]
        fig = (f'<figure class="fig sans"><figcaption>{e(c["id"])}: {e(c["title"])} ({e(c["unit"])})</figcaption>'
               f'<div role="img" aria-label="{e(c["title"])}">{_inline_svg(svg)}</div>'
               f'<p class="src">{e(c["source"])}</p></figure>')
    groups: dict[str, list[int]] = {}
    for c in doc["citations"]:
        if c.get("cover"):
            groups.setdefault(c["group"], []).append(c["n"])
    srcline = ('<p class="srcline sans"><b>Sources.</b> ' + "; ".join(
        f"<b>{e(format_refs(ns))}</b> {e(g)}" for g, ns in groups.items()) + ".</p>") if groups else ""
    body = "" if cover_only else _body(doc, charts, titles)
    controls = "" if cover_only or not doc.get("controls") else _controls(doc)
    hdr = f"CoverageFlow Research  |  {m['doc_type']}  |  {m['date']}"
    ftr = f"{st['label']}  ·  {m['report_version']}  ·  information cutoff {m['cutoff']}"
    css = (CSS.replace("var(--hdr)", _css_str(hdr)).replace("var(--tkr)", _css_str(m["ticker"]))
           .replace("var(--ftr)", _css_str(ftr)))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(m['ticker'])} {e(m['kicker'].split('·')[-1].strip())} - CoverageFlow Research</title>
<style>{css}</style></head>
<body class="{'draft' if m['draft'] else ''}"><div class="page">
<div class="run sans"><span>CoverageFlow Research &nbsp;|&nbsp; {e(m['doc_type'])} &nbsp;|&nbsp; {e(m['date'])}</span><b>{e(m['ticker'])}</b></div>
<section class="cover">
<div class="cols">
<main class="main">
<div class="kicker sans">{e(m['kicker'])}</div>
<h1>{e(doc['headline'])}</h1>
<div class="byline sans">{e(doc['byline']['text'])}</div>
<ul class="kp sans-nums">{kp}</ul>
<h2>Summary</h2>
<div class="summary">{summary}</div>
{fig}
{srcline}
<div class="status sans">{status}</div>
</main>
<aside class="side sans">
<div class="lab">RATING</div>
<div class="rating">{e(r['rating'] or 'n/a')}</div>
<dl class="kv">
<div class="r"><dt>Price target</dt><dd>{e(r['price_target'])}</dd><div class="nt">{e(r['basis'])}</div></div>
<div class="r"><dt>Price</dt><dd>{e(r['price'])}</dd><div class="nt">close, {e(r['price_as_of'])}</div></div>
<div class="r"><dt>Implied return</dt><dd>{e(r['implied_return'])}</dd></div>
</dl>
<h2>Key data</h2>
<dl class="kv">{kd}</dl>
<h2>DCF value per share</h2>
<dl class="kv">{vm}</dl>
{fin_html}
</aside>
</div>
<p class="disc sans">{e(doc['disclaimer'])}</p>
</section>
{body}
{controls}
<div class="foot sans"><span>{e(st['label'])} &middot; {e(m['report_version'])} &middot; information cutoff {e(m['cutoff'])}</span></div>
</div></body></html>
"""


def render_cover_html(doc: dict, chart_svg: str | None = None) -> str:
    """Legacy name: the cover only."""
    return render_note_html(doc, {"segments": chart_svg} if chart_svg else {}, cover_only=True)
