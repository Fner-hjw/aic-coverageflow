"""Self-contained HTML renderer for the page-1 cover of the redesigned note.

Reads only a ``report_doc`` dict (``report_doc.build_report_doc``).  The page
is a single file: inline CSS, no scripts, no external requests, no web fonts
(Charter / Georgia for prose, system sans for data).  ``@media print`` matches
the A4 PDF; below 760px the two columns collapse into one.
"""

from __future__ import annotations

from html import escape

from .report_doc import format_refs

CSS = """
:root{--navy:#1F3A5F;--ink:#1b1b1b;--mute:#5e6670;--hair:#b9bec4;--bg:#fff}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#eceeef;color:var(--ink);
  font-family:Charter,'Bitstream Charter','Sitka Text',Georgia,serif;font-size:16px;line-height:1.5}
.sans{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;
  font-variant-numeric:lining-nums tabular-nums}
.page{position:relative;max-width:210mm;margin:0 auto;background:var(--bg);padding:0 14mm 12mm;min-height:297mm}
.draft::before{content:'DRAFT';position:absolute;left:50%;top:46%;transform:translate(-50%,-50%) rotate(-54deg);
  font:700 190px/1 -apple-system,'Helvetica Neue',Arial,sans-serif;color:rgba(0,0,0,.06);pointer-events:none}
.run{display:flex;justify-content:space-between;align-items:baseline;padding:14mm 0 6px;
  border-bottom:.5px solid var(--hair);font-size:11px;color:var(--mute)}
.run b{color:var(--navy);font-size:12px}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 292px;column-gap:0;margin-top:8mm}
.main{padding-right:5.5mm;border-right:.5px solid var(--hair)}
.side{padding-left:5.5mm}
.kicker{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--navy)}
h1{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-weight:700;
  font-size:27px;line-height:1.16;margin:4px 0 8px;letter-spacing:-.005em}
.byline{font-size:11px;color:var(--mute);padding-bottom:6px;border-bottom:.5px solid var(--hair)}
ul.kp{list-style:none;margin:12px 0 0;padding:0}
ul.kp li{position:relative;padding-left:13px;margin:0 0 5px;font-size:14.5px;line-height:1.4}
ul.kp li::before{content:'';position:absolute;left:0;top:.52em;width:4.5px;height:4.5px;background:var(--navy)}
ul.kp b{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;font-size:13px;color:var(--navy);margin-right:.3em}
h2{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;font-size:10.5px;font-weight:700;
  letter-spacing:.09em;text-transform:uppercase;color:var(--navy);margin:16px 0 6px;
  padding-bottom:2px;border-bottom:1px solid var(--navy)}
.summary p{margin:0 0 7px;hyphens:auto;-webkit-hyphens:auto;text-align:left}
sup.c{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;font-size:.62em;line-height:0;vertical-align:super}
sup.c a{color:var(--navy);text-decoration:none}
sup.c a:hover{text-decoration:underline}
ol.notes{margin:4px 0 0;padding:0;list-style:none;font-size:10.5px;line-height:1.45;color:var(--mute)}
ol.notes li{display:grid;grid-template-columns:2.2em 1fr;column-gap:2px;padding:1px 0}
ol.notes li:target{color:var(--ink);background:#f3f5f7}
ol.notes .n{color:var(--navy);font-weight:700;text-align:right;padding-right:.5em}
ol.notes code{font-family:ui-monospace,Menlo,monospace;font-size:.95em}
figure.fig{margin:16px 0 8px}
figure.fig figcaption{font-size:11px;font-weight:700;color:var(--navy);margin-bottom:2px}
figure.fig svg{width:100%;height:auto;display:block}
figure.fig .src{font-size:10px;color:var(--mute);margin:2px 0 0}
.status{margin-top:16px;border:.5px solid var(--hair);padding:6px 9px;font-size:12px;line-height:1.4}
.rating{font-size:38px;font-weight:700;color:var(--navy);line-height:1.05;margin:0 0 6px;
  padding-bottom:5px;border-bottom:1px solid var(--navy)}
.lab{font-size:10.5px;font-weight:700;letter-spacing:.09em;color:var(--navy)}
dl.kv{margin:0}
dl.kv .r{display:flex;justify-content:space-between;align-items:baseline;border-bottom:.5px solid var(--hair);
  padding:4px 0 3px;font-size:12.5px;flex-wrap:wrap}
dl.kv dt{color:var(--mute)}
dl.kv dd{margin:0;font-weight:700;text-align:right}
dl.kv .nt{flex-basis:100%;text-align:right;font-size:10px;color:var(--mute);line-height:1.25}
table.fs{border-collapse:collapse;width:100%;font-size:10.5px}
table.fs th{font-weight:700;text-align:right;padding:0 0 3px 6px;white-space:nowrap;border-bottom:1px solid var(--navy)}
table.fs td{text-align:right;padding:2.5px 0 2.5px 6px;white-space:nowrap;border-bottom:.5px solid var(--hair)}
table.fs th:first-child,table.fs td:first-child{text-align:left;font-weight:400;padding-left:0}
table.fs td .u{font-size:9px;color:var(--mute)}
.fsn{font-size:9.5px;color:var(--mute);margin:4px 0 0;line-height:1.35}
.srcline{display:none}
.sub{font-size:10px;color:var(--mute);margin:0 0 3px}
.disc{margin-top:10mm;font-size:9.5px;color:var(--mute);line-height:1.4}
.foot{display:flex;justify-content:space-between;font-size:10px;color:var(--mute);margin-top:6px;
  border-top:.5px solid var(--hair);padding-top:4px}
@media (max-width:760px){
  .page{padding:0 16px 24px}.run{padding-top:14px}
  .cols{grid-template-columns:1fr;margin-top:18px}
  .main{padding-right:0;border-right:0}
  .side{padding-left:0;margin-top:20px;border-top:.5px solid var(--hair);padding-top:14px}
  h1{font-size:23px}
}
@media print{
  @page{size:A4;margin:0}
  .cols{grid-template-columns:minmax(0,1fr) 60mm}
  table.fs th,table.fs td{padding-left:4px}
  table.fs{font-size:6.8pt}
  ol.notes{display:none}.srcline{display:block!important;font-size:6.6pt;line-height:1.25;color:var(--mute);margin:3px 0 0}
  body{background:#fff;font-size:10pt;line-height:1.3}
  .page{max-width:none;margin:0;padding:0 18mm;height:297mm;min-height:0;overflow:hidden}
  .run{padding-top:14mm;font-size:7.5pt}.run b{font-size:8pt}
  h1{font-size:19pt}.kicker{font-size:8pt}.byline{font-size:7.5pt}
  ul.kp li{font-size:9.5pt}ul.kp b{font-size:9pt}
  h2{font-size:7.5pt}.lab{font-size:7.5pt}
  .rating{font-size:25pt}dl.kv .r{font-size:8.5pt}dl.kv .nt{font-size:6.5pt}
  table.fs{font-size:7pt}.fsn,.sub{font-size:6.3pt}
  figure.fig figcaption{font-size:7.5pt}figure.fig .src{font-size:6.3pt}
  ol.notes{font-size:6.6pt}.status{font-size:7.8pt}.disc{font-size:6.3pt}.foot{font-size:7pt}
  .page a{color:inherit}
}
"""


def e(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def _sentence(s: dict) -> str:
    text = e(s["text"])
    if s["refs"]:
        links = []
        # one cluster; each number links to its own footnote
        nums = sorted(s["refs"])
        label = format_refs(nums)
        links = _cluster(nums, label)
        text = (text[:-1] + links + text[-1]) if text and text[-1] in ".;" else text + links
    return text


def _cluster(nums: list[int], label: str) -> str:
    """Render '1–4, 7' with every number inside a range linking to its note."""
    parts = []
    for chunk in label.split(", "):
        if "–" in chunk:
            a, b = (int(x) for x in chunk.split("–"))
            parts.append(f'<a href="#n{a}">{a}</a>–<a href="#n{b}">{b}</a>')
        else:
            parts.append(f'<a href="#n{chunk}">{chunk}</a>')
    return f'<sup class="c">{", ".join(parts)}</sup>'


def render_cover_html(doc: dict, chart_svg: str | None = None) -> str:
    m, r = doc["meta"], doc["rating"]
    fs, st = doc["financial_summary"], doc["status"]

    kp = "".join(f"<li><b>{e(k['label'])}</b>{e(k['text'])}</li>" for k in doc["key_points"])
    summary = "".join("<p>" + " ".join(_sentence(s) for s in p) + "</p>" for p in doc["summary"])
    notes = "".join(
        f'<li id="n{c["n"]}" title="{e(c["source"] + (" - " + c["locator"] if c["locator"] else ""))}">'
        f'<span class="n">{c["n"]}</span><span>{e(c["source"])}'
        f'{" - " + e(c["locator"]) if c["locator"] and c["kind"] == "filing" else ""}'
        f'{"<code> " + e(c["key"]) + "</code>" if c["kind"] != "filing" else ""}</span></li>'
        for c in doc["citations"])
    # sup-link hover title: show source on the link itself
    for c in doc["citations"]:
        title = e(c["source"] + (", " + c["locator"] if c["locator"] else ""))
        summary = summary.replace(f'<a href="#n{c["n"]}">', f'<a href="#n{c["n"]}" title="{title}">')

    if st["exceptions"]:
        ex = "; ".join(f"<b>{e(x['id'])}</b> ({e(x['severity'])}) {e(x['short'])}" for x in st["exceptions"])
        n = st["n_open"]
        status = f"<b>{e(st['label'])}</b> — {n} open finding{'s' if n != 1 else ''}: {ex}."
    else:
        status = f"<b>{e(st['label'])}</b>"

    kd = "".join(
        f'<div class="r"><dt>{e(k["label"])}</dt><dd>{e(k["value"])}</dd>'
        + (f'<div class="nt">{e(k["note"])}</div>' if k["note"] else "") + "</div>"
        for k in doc["key_data"])
    vm = "".join(f'<div class="r"><dt>{e(k["label"])}</dt><dd>{e(k["value"])}</dd><div class="nt">{e(k["note"])}</div></div>'
                 for k in doc.get("valuation_methods", []))
    head = "".join(f"<th>{e(c['label'].replace('FY20', 'FY'))}{c['kind']}</th>" for c in fs["columns"])
    rows = "".join(
        f"<tr><td>{e(x['label'])}" + (f' <span class="u">({e(x["unit"])})</span>' if x["unit"] == "USD" else "")
        + "</td>" + "".join(f"<td>{e(v) if v is not None else '–'}</td>" for v in x["values"]) + "</tr>"
        for x in fs["rows"])
    fnotes = "".join(f"<p class='fsn'>{e(n)}</p>" for n in fs["notes"])

    fig = ""
    if doc.get("chart") and chart_svg:
        c = doc["chart"]
        svg = chart_svg[chart_svg.index("<svg"):]
        fig = (f'<figure class="fig sans"><figcaption>Figure 1: {e(c["title"])} ({e(c["unit"])})</figcaption>'
               f'<div role="img" aria-label="{e(c["title"])}">{svg}</div>'
               f'<p class="src">{e(c["source"])}</p></figure>')

    groups: dict[str, list[int]] = {}
    for c in doc["citations"]:
        groups.setdefault(c["group"], []).append(c["n"])
    srcline = ('<p class="srcline sans"><b>Sources.</b> ' + "; ".join(
        f"<b>{e(format_refs(ns))}</b> {e(g)}" for g, ns in groups.items()) + ".</p>") if groups else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(m['ticker'])} {e(m['kicker'].split('·')[-1].strip())} - CoverageFlow Research</title>
<style>{CSS}</style></head>
<body><div class="page{' draft' if m['draft'] else ''}">
<div class="run sans"><span>CoverageFlow Research &nbsp;|&nbsp; {e(m['doc_type'])} &nbsp;|&nbsp; {e(m['date'])}</span><b>{e(m['ticker'])}</b></div>
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
<ol class="notes sans" aria-label="Sources">{notes}</ol>
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
<h2>Financial summary</h2>
<p class="sub">USD mm unless stated; fiscal year</p>
<table class="fs"><thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table>
{fnotes}
</aside>
</div>
<p class="disc sans">{e(doc['disclaimer'])}</p>
<div class="foot sans"><span>{e(st['label'])} &middot; {e(m['report_version'])} &middot; information cutoff {e(m['cutoff'])}</span><span>1</span></div>
</div></body></html>
"""
