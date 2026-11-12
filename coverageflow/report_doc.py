"""Structured "report_doc" for the redesigned equity-research note.

``build_report_doc(evidence)`` turns :func:`export_dossier.collect_case`
output (verified artifacts of one case) into a renderer-neutral dict.  Both
renderers (``render_note_tex`` -> PDF and ``render_note_html`` -> HTML) read
only this dict, so the two outputs cannot drift apart.

Every figure is read from an artifact (model.json / valuation.json /
mandate.json / approval record / report.md); nothing is typed in.  The
``figure_sources`` entry documents which artifact key each cover figure came
from.  Strings are plain Unicode (real minus sign U+2212, no markup); the
renderers do their own escaping.

Page-1 only for now (``--cover-only``); the remaining sections of the note
are built later from the same dict.

CLI:
    python -m coverageflow.report_doc --case <case-dir> --out <dir> --cover-only
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from .export_dossier import collect_case, evidence_index_from_facts
from .render_note import strip_process_sections

MINUS = "−"
EN = "–"
EV = re.compile(r"\s*\[EV:([^\]]+)\]")

COMPANY_NAMES = {"AMD": "Advanced Micro Devices, Inc.", "INTC": "Intel Corporation"}

TARGET_BASIS = {
    "gordon_only_terminal_values_disagree": "Gordon DCF; terminal methods disagree",
    "gordon": "Gordon-growth DCF",
    "exit_multiple": "Exit-multiple DCF",
    "blend": "Blend of DCF terminal methods",
}


# ------------------------------------------------------------- formatting
def _num(x: Any) -> float | None:
    return None if x is None else float(x)


def f_mm(x: Any) -> str:
    x = _num(x)
    if x is None:
        return "n/a"
    s = f"{abs(x):,.0f}"
    return (MINUS + s) if x < 0 and round(x) != 0 else s


def f_usd(x: Any, d: int = 2) -> str:
    x = _num(x)
    if x is None:
        return "n/a"
    s = f"{abs(x):,.{d}f}"
    return (MINUS + s) if x < 0 else s


def f_pct(x: Any, d: int = 1, sign: bool = False) -> str:
    x = _num(x)
    if x is None:
        return "n/a"
    s = f"{abs(x) * 100:.{d}f}%"
    if x < 0:
        return MINUS + s
    return ("+" + s) if sign else s


def f_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    return f"{d.day} {d.strftime('%b %Y')}"


def format_refs(nums: list[int]) -> str:
    """[1,2,3,4,7] -> '1–4, 7' (runs of three or more become ranges)."""
    nums = sorted(set(nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{nums[i]}{EN}{nums[j]}")
        else:
            out.extend(str(n) for n in nums[i:j + 1])
        i = j + 1
    return ", ".join(out)


# ---------------------------------------------------------- data access
def _series(model: dict, field: str) -> dict:
    return (model.get("series") or {}).get(field) or {}


def _val(model: dict, field: str, period: str) -> float | None:
    return _num(_series(model, field).get(period))


def _yoy(model: dict, field: str, period: str) -> float | None:
    return _num((_series(model, field).get("yoy") or {}).get(period))


def _latest_quarter(model: dict) -> str:
    qs = {p for s in (model.get("series") or {}).values() for p in s
          if re.fullmatch(r"\d{4}-Q\d", p)}
    if not qs:
        raise ValueError("model.json has no quarterly period")
    return max(qs)


def _prior_year(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}"


def _quarter_label(period: str) -> str:
    return f"{period[5:]} FY{period[:4]}"


def _segments(model: dict) -> list[str]:
    return list(model.get("segment_slugs") or [])


def _seg_name(slug: str) -> str:
    return slug.replace("_", " ").title().replace(" And ", " and ")


# ----------------------------------------------------------- headline
def build_headline(rating: str | None, upside: float | None, lead_name: str | None,
                   ) -> str:
    """Deterministic headline: lead segment + valuation-vs-price clause."""
    left = f"{lead_name} carries the quarter" if lead_name else "Quarter in review"
    if upside is None or not rating:
        return left
    pct = f"{abs(upside) * 100:.0f}%"
    r = rating.lower()
    if r == "sell":
        right = f"DCF value is {pct} below the price"
    elif r == "buy":
        right = f"DCF value is {pct} above the price"
    else:
        right = "DCF value sits close to the price"
    return f"{left}; {right}"


# ------------------------------------------------------------ citations
class _Cites:
    """Numbers [EV:key] markers by first appearance and resolves sources."""

    def __init__(self, evidence: dict):
        self.index: dict[str, dict] = {}
        self.facts = (evidence.get("facts") or {}).get("facts") or []
        self.docs = {d["doc_id"]: d for d in
                     (evidence.get("manifest") or {}).get("documents", [])}
        self.model, self.val = evidence.get("model") or {}, evidence.get("valuation") or {}
        self.ticker = (evidence.get("mandate") or {}).get("ticker", "")
        self.nums: dict[str, int] = {}

    def number(self, key: str) -> int:
        return self.nums.setdefault(key, len(self.nums) + 1)

    def doc_label(self, doc_id: str) -> str:
        d = self.docs.get(doc_id)
        if not d:
            return doc_id
        return (f"{self.ticker} Form {d.get('form')}, period ended "
                f"{f_date(d.get('period'))}")

    def resolve(self, key: str) -> dict:
        cands = [f for f in self.facts if f.get("field") == key and f.get("citation")]
        if cands:
            # prefer the most recent period the fact set carries
            pe = self.model.get("period_ends") or {}
            cands.sort(key=lambda f: (str(pe.get(f.get("period"), "")),
                                      str(f.get("period"))), reverse=True)
            f = cands[0]
            c = f["citation"]
            return {"kind": "filing", "group": self.doc_label(c.get("doc_id", "")),
                    "source": self.doc_label(c.get("doc_id", "")),
                    "locator": ", ".join(x for x in (c.get("anchor"), c.get("locator")) if x)}
        if key.startswith(("dcf.", "comps.", "assumptions.", "cross_check", "sensitivity.")):
            return {"kind": "engine", "group": "CoverageFlow valuation engine",
                    "source": f"CoverageFlow valuation engine ({self.val.get('engine', '')})",
                    "locator": key}
        return {"kind": "engine", "group": "CoverageFlow model engine",
                "source": f"CoverageFlow model engine ({self.model.get('engine', '')}), "
                          "derived from filings", "locator": key}


def _summary_paragraphs(report_md: str, cites: _Cites) -> list[list[dict]]:
    """Summary section -> paragraphs of sentences {text, refs}; one marker
    cluster per sentence, placed before the sentence's closing punctuation."""
    body = strip_process_sections(report_md)
    m = re.search(r"^##\s+Summary\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    if not m:
        return []
    paras = []
    for para in re.split(r"\n\s*\n", m.group(1).strip()):
        para = " ".join(para.split()).replace("**", "")
        sents = _split_sentences(para)
        out = []
        for s in sents:
            keys = [k.strip() for mm in EV.finditer(s)
                    for k in re.split(r"[;,]", mm.group(1)) if k.strip()]
            text = EV.sub("", s).strip()
            text = re.sub(r"\s+([.;,])", r"\1", text)
            refs = []
            for k in keys:
                n = cites.number(k)
                if n not in refs:
                    refs.append(n)
            out.append({"text": text, "refs": sorted(refs)})
        paras.append(out)
    return paras


def _split_sentences(para: str) -> list[str]:
    """Split on '.' / '!' / '?' + optional markers, then whitespace + capital."""
    parts, start = [], 0
    pat = re.compile(r"([.!?])((?:\s*\[EV:[^\]]+\])*)(\s+)(?=[A-Z])")
    for mt in pat.finditer(para):
        end = mt.end(2)
        chunk = para[start:end]
        parts.append(chunk)
        start = mt.end()
    parts.append(para[start:])
    return [p for p in parts if p.strip()]


# --------------------------------------------------------- main builder
def build_report_doc(evidence: dict, force_draft: bool = False) -> dict:
    mandate, model, val = evidence["mandate"], evidence["model"], evidence["valuation"]
    result, approval = evidence.get("result") or {}, evidence.get("approval") or {}
    dcf = val.get("dcf") or {}
    ticker = str(mandate.get("ticker", ""))
    cutoff = str(mandate.get("cutoff_date", ""))
    cur = "USD"
    src: dict[str, str] = {}

    # ---- status
    terminal = result.get("terminal") or approval.get("decision") or "DRAFT"
    decision = approval.get("decision")
    published = decision in ("APPROVED", "PUBLISHED_WITH_EXCEPTIONS") and not force_draft
    status_label = {"PUBLISHED_WITH_EXCEPTIONS": "Published with exceptions",
                    "APPROVED": "Approved", "PUBLISHED": "Approved"}.get(
        decision if published else "", "Draft for review " + EN + " not approved")
    exceptions = []
    for e in (approval.get("exceptions") or
              (result.get("reason", {}).get("detail", {}).get("exceptions") or [])):
        s = str(e.get("summary", "")).strip()
        short = s.split(":")[0].strip() if ":" in s else re.split(r"(?<=\.)\s", s)[0]
        exceptions.append({"id": e.get("finding_id"), "severity": e.get("severity"),
                           "summary": s, "short": short.rstrip(".")})
    approver = approval.get("by") if published else None
    pub_date = (approval.get("ts") or "")[:10] if published else ""

    # ---- latest quarter
    q = _latest_quarter(model)
    pq = _prior_year(q)
    rev, rev_p = _val(model, "income_statement.revenue", q), _val(model, "income_statement.revenue", pq)
    rev_yoy = _yoy(model, "income_statement.revenue", q)
    gm, gm_p = _val(model, "derived.gross_margin", q), _val(model, "derived.gross_margin", pq)
    oi, oi_yoy = _val(model, "income_statement.operating_income", q), _yoy(model, "income_statement.operating_income", q)
    om = _val(model, "derived.operating_margin", q)
    eps, eps_p = _val(model, "income_statement.diluted_eps", q), _val(model, "income_statement.diluted_eps", pq)

    # ---- segments: lead = largest absolute y/y revenue gain
    seg_rows = []
    for slug in _segments(model):
        r, rp = _val(model, f"segments.{slug}.revenue", q), _val(model, f"segments.{slug}.revenue", pq)
        if r is not None and rp is not None:
            seg_rows.append({"slug": slug, "name": _seg_name(slug), "rev": r, "delta": r - rp,
                             "yoy": _yoy(model, f"segments.{slug}.revenue", q),
                             "share": _val(model, f"segments.{slug}.revenue_share_of_total", q)})
    lead = max(seg_rows, key=lambda s: s["delta"]) if seg_rows else None
    lag = min(seg_rows, key=lambda s: s["delta"]) if seg_rows else None

    # ---- valuation
    rating = dcf.get("rating")
    pt = dcf.get("target_price")
    price = dcf.get("market_price")
    upside = dcf.get("upside")
    shares = dcf.get("diluted_shares")
    net_debt = dcf.get("net_debt")
    mcap = price * shares if price is not None and shares is not None else None
    basis = TARGET_BASIS.get(dcf.get("target_basis"),
                             str(dcf.get("target_basis", "")).replace("_", " ").capitalize())

    headline = build_headline(rating, upside, lead["name"] if lead else None)

    # ---- key points
    kp = []
    if rev is not None:
        kp.append({"label": "Revenue", "text":
                   f"{cur} {f_mm(rev)}mm in {_quarter_label(q)}, {f_pct(rev_yoy, 1, True)} y/y "
                   f"(prior-year quarter {cur} {f_mm(rev_p)}mm)."})
    if gm is not None:
        kp.append({"label": "Margins", "text":
                   f"Gross margin {f_pct(gm)} versus {f_pct(gm_p)} a year earlier; operating income "
                   f"{cur} {f_mm(oi)}mm ({f_pct(oi_yoy, 1, True)} y/y), operating margin {f_pct(om)}."})
    if eps is not None:
        kp.append({"label": "Earnings", "text":
                   f"Diluted EPS {cur} {f_usd(eps)} versus {cur} {f_usd(eps_p)} in the prior-year quarter."})
    if lead:
        t = (f"{lead['name']} revenue {cur} {f_mm(lead['rev'])}mm, {f_pct(lead['yoy'], 1, True)} y/y"
             + (f", {f_pct(lead['share'])} of group" if lead.get("share") is not None else ""))
        if lag and lag["delta"] < 0:
            t += f"; {lag['name']} {f_pct(lag['yoy'], 1, True)} y/y"
        kp.append({"label": "Segments", "text": t + "."})

    # ---- key data
    kd = []
    if mcap is not None:
        kd.append({"label": "Market cap", "value": f"{cur} {f_mm(mcap)}mm",
                   "note": "computed: price × diluted shares"})
    if net_debt is not None:
        kd.append({"label": "Net cash" if net_debt < 0 else "Net debt",
                   "value": f"{cur} {f_mm(abs(net_debt))}mm",
                   "note": _quarter_label(dcf.get("net_debt_period") or q)})
    if shares is not None:
        kd.append({"label": "Diluted shares", "value": f"{f_mm(shares)}mm",
                   "note": _quarter_label(dcf.get("shares_period") or q)})
    kd.append({"label": "WACC", "value": f_pct(dcf.get("wacc"), 1), "note": ""})
    kd.append({"label": "Terminal growth",
               "value": f_pct((dcf.get("terminal", {}).get("gordon") or {}).get("terminal_growth"), 1),
               "note": ""})

    # ---- DCF value per share by terminal method
    pds = dcf.get("per_diluted_share") or {}
    vm = []
    if pds.get("gordon") is not None:
        vm.append({"label": "Gordon growth", "value": f"{cur} {f_usd(pds['gordon'])}",
                   "note": ("target basis; " if pt == pds["gordon"] else "")
                           + f"g = {f_pct((dcf.get('terminal', {}).get('gordon') or {}).get('terminal_growth'), 1)}"})
    if pds.get("exit_multiple") is not None:
        mult = (dcf.get("terminal", {}).get("exit_multiple") or {}).get("multiple")
        vm.append({"label": "Exit multiple", "value": f"{cur} {f_usd(pds['exit_multiple'])}",
                   "note": (f"{mult:.1f}x terminal EBITDA" if mult else "")})

    # ---- financial summary
    years = dcf.get("years") or []
    base_fy = int(re.sub(r"\D", "", (dcf.get("base") or {}).get("window", {}).get("fy") or "0") or 0)
    act = sorted(p for p in _series(model, "income_statement.revenue") if re.fullmatch(r"FY\d{4}", p))[-2:]
    cols = [{"label": p, "kind": "A", "key": p} for p in act]
    n_fc = min(3, len(years))
    cols += [{"label": f"FY{base_fy + i + 1}", "kind": "E", "key": i} for i in range(n_fc)]

    def row(label, unit, actf, fcf, fmt):
        vals = []
        for c in cols:
            if c["kind"] == "A":
                v = actf(c["key"])
            else:
                v = fcf(years[c["key"]]) if fcf else None
            vals.append(None if v is None else fmt(v))
        return {"label": label, "unit": unit, "values": vals}

    fin_rows = [
        row("Revenue", "USD mm", lambda p: _val(model, "income_statement.revenue", p),
            lambda y: y.get("revenue"), f_mm),
        row("Growth y/y", "%", lambda p: _yoy(model, "income_statement.revenue", p),
            lambda y: y.get("growth"), lambda v: f_pct(v, 1)),
        row("EBITDA margin", "%", lambda p: _val(model, "derived.ebitda_margin", p),
            lambda y: y.get("ebitda_margin"), lambda v: f_pct(v, 1)),
        row("Diluted EPS", "USD", lambda p: _val(model, "income_statement.diluted_eps", p),
            None, lambda v: f_usd(v)),
        row("Free cash flow", "USD mm", lambda p: _val(model, "derived.fcf", p),
            lambda y: y.get("fcf"), f_mm),
    ]
    fin_rows = [r for r in fin_rows if any(v is not None for v in r["values"])]
    margin_note = ""
    if (dcf.get("cash_flow_build") or {}).get("ebit_margin_source") == "ltm_flat":
        margin_note = "Forecast margins held at the base-year level."
    fin_notes = [n for n in ("A = reported, E = CoverageFlow DCF base case. " + margin_note,
                             "EPS is not forecast by the model; forecast FCF is unlevered."
                             if any(r["label"] == "Diluted EPS" for r in fin_rows) else "") if n]

    # ---- summary prose with citations
    cites = _Cites(evidence)
    paras = _summary_paragraphs(evidence.get("report_md") or "", cites)
    citations = []
    for key, n in sorted(cites.nums.items(), key=lambda kv: kv[1]):
        r = cites.resolve(key)
        citations.append({"n": n, "key": key, **r})

    # ---- chart: segment revenue, latest quarter vs prior-year quarter
    chart = None
    if seg_rows:
        chart = {"title": f"Revenue by segment, {_quarter_label(q)} vs {_quarter_label(pq)}",
                 "unit": "USD mm",
                 "categories": [{"name": s_["name"], "note": f_pct(s_["yoy"], 1, True) + " y/y"}
                                for s_ in seg_rows],
                 "series": [{"label": _quarter_label(pq),
                             "values": [_val(model, f"segments.{s_['slug']}.revenue", pq) for s_ in seg_rows]},
                            {"label": _quarter_label(q), "values": [s_["rev"] for s_ in seg_rows]}],
                 "source": "Source: company filings (Form 10-Q), CoverageFlow model engine."}
        src["chart"] = f"model.series segments.<slug>.revenue [{q}, {pq}] and .yoy[{q}]"

    # ---- provenance of cover figures
    src.update({
        "headline": "dcf.rating, dcf.upside, segments.<slug>.revenue (largest y/y gain)",
        "revenue / y/y": f"model.series income_statement.revenue [{q}], .yoy[{q}]",
        "gross margin": f"model.series derived.gross_margin [{q}, {pq}]",
        "operating income": f"model.series income_statement.operating_income [{q}], .yoy[{q}]",
        "operating margin": f"model.series derived.operating_margin [{q}]",
        "diluted EPS": f"model.series income_statement.diluted_eps [{q}, {pq}]",
        "lead segment": f"model.series segments.<slug>.revenue / .yoy / revenue_share_of_total [{q}]",
        "rating": "valuation.dcf.rating", "price target": "valuation.dcf.target_price",
        "DCF by terminal method": "valuation.dcf.per_diluted_share.{gordon,exit_multiple}, terminal.exit_multiple.multiple",
        "price target basis": "valuation.dcf.target_basis",
        "price / as of": "valuation.dcf.market_price, market_price_as_of",
        "implied return": "valuation.dcf.upside",
        "market cap": "computed: valuation.dcf.market_price x valuation.dcf.diluted_shares",
        "net cash/debt": "valuation.dcf.net_debt, net_debt_period",
        "diluted shares": "valuation.dcf.diluted_shares, shares_period",
        "WACC": "valuation.dcf.wacc",
        "terminal growth": "valuation.dcf.terminal.gordon.terminal_growth",
        "fin. summary actuals": "model.series income_statement.revenue (+yoy), derived.ebitda_margin, "
                                "income_statement.diluted_eps, derived.fcf [FY columns]",
        "fin. summary forecasts": "valuation.dcf.years[].revenue/growth/ebitda_margin/fcf",
        "status / exceptions": "approvals/apr-*.json (decision, by, ts, exceptions); result.json",
        "summary prose": "report/<report_version>.md, section Summary",
        "citation sources": "facts.json citation (doc_id, anchor, locator) + inputs/manifest.json",
    })

    return {
        "meta": {
            "ticker": ticker,
            "company": COMPANY_NAMES.get(ticker.upper(), ticker),
            "kicker": f"{ticker} · {_quarter_label(q)} {str(mandate.get('report_policy', {}).get('note_type', 'update')).replace('quarterly_', '')}",
            "doc_type": "Equity Research",
            "date": f_date(pub_date or cutoff),
            "cutoff": f_date(cutoff),
            "cutoff_iso": cutoff,
            "latest_period": q,
            "draft": not published,
            "terminal": terminal,
            "candidate_id": (evidence.get("state", {}).get("candidate") or {}).get("candidate_id")
                            or result.get("candidate_id") or "",
            "report_version": result.get("report_version") or "draft",
            "case_id": evidence.get("case_id"),
        },
        "headline": headline,
        "byline": {"author": "CoverageFlow (automated coverage)",
                   "approver": approver, "approved_on": f_date(pub_date) if pub_date else "",
                   "decision": decision if published else None,
                   "text": ("CoverageFlow (automated coverage) · "
                            + (f"{'Published with exceptions' if decision == 'PUBLISHED_WITH_EXCEPTIONS' else 'Approved'} by {approver}, {f_date(pub_date)}"
                               if published and approver else "Not yet approved"))},
        "rating": {"rating": rating, "price_target": f"{cur} {f_usd(pt)}", "basis": basis,
                   "price": f"{cur} {f_usd(price)}", "price_as_of": f_date(dcf.get("market_price_as_of")),
                   "implied_return": f_pct(upside, 1, True)},
        "key_data": kd,
        "valuation_methods": vm,
        "financial_summary": {"columns": [{"label": c["label"], "kind": c["kind"]} for c in cols],
                              "rows": fin_rows, "notes": fin_notes, "currency_unit": "USD mm"},
        "key_points": kp,
        "summary": paras,
        "chart": chart,
        "status": {"label": status_label, "published": published,
                   "exceptions": exceptions, "n_open": len(exceptions)},
        "citations": citations,
        "disclaimer": (f"Generated by CoverageFlow from SEC filings and market data; information "
                       f"cutoff {f_date(cutoff)}. Automated analysis for a course project; "
                       "not investment advice."),
        "figure_sources": src,
    }


# ------------------------------------------------------------------ CLI
def _compile_pdf(tex: Path) -> tuple[bool, list[str]]:
    for _ in range(2):
        p = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                            "-output-directory", str(tex.parent), str(tex)],
                           capture_output=True, text=True, timeout=180, cwd=str(tex.parent))
    log = tex.with_suffix(".log").read_text(errors="replace") if tex.with_suffix(".log").exists() else ""
    warn = [l for l in log.splitlines() if re.search(r"Overfull|Underfull \\vbox|Error|Missing", l)]
    return p.returncode == 0 and tex.with_suffix(".pdf").exists(), warn


def build_cover(case_dir: Path, out_dir: Path, draft_variant: bool = True,
                force_draft: bool = False) -> dict:
    from .render_note_html import render_cover_html
    from .render_note_tex import render_cover_tex

    from .note_charts import segment_chart

    out_dir.mkdir(parents=True, exist_ok=True)
    ev = collect_case(case_dir)
    res: dict[str, Any] = {}
    variants = [("cover", force_draft)] + ([("cover-draft", True)] if draft_variant and not force_draft else [])
    for name, draft in variants:
        doc = build_report_doc(ev, force_draft=draft)
        if name == "cover":
            (out_dir / "report_doc.json").write_text(
                json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            svg = None
            if doc.get("chart"):
                svg, pdfb = segment_chart(doc["chart"])
                (out_dir / "chart-segments.pdf").write_bytes(pdfb)
                (out_dir / "chart-segments.svg").write_text(svg, encoding="utf-8")
            (out_dir / "cover.html").write_text(render_cover_html(doc, chart_svg=svg), encoding="utf-8")
            res["html"] = str(out_dir / "cover.html")
        tex = out_dir / f"{name}.tex"
        tex.write_text(render_cover_tex(doc, "chart-segments.pdf" if doc.get("chart") else None), encoding="utf-8")
        ok, warn = (False, ["pdflatex not found"]) if not shutil.which("pdflatex") else _compile_pdf(tex)
        res[name] = {"tex": str(tex), "pdf": str(tex.with_suffix(".pdf")) if ok else None,
                     "log_warnings": warn}
        if ok and shutil.which("pdftoppm"):
            subprocess.run(["pdftoppm", "-r", "110", "-png", "-singlefile",
                            str(tex.with_suffix(".pdf")), str(out_dir / name)], check=False)
            res[name]["png"] = str(out_dir / f"{name}.png")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", required=True, help="case directory")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--cover-only", action="store_true", help="build page 1 only (currently the only mode)")
    ap.add_argument("--draft", action="store_true", help="force DRAFT mode on the main cover")
    ap.add_argument("--no-draft-variant", action="store_true", help="skip cover-draft.pdf")
    args = ap.parse_args(argv)
    res = build_cover(Path(args.case), Path(args.out), draft_variant=not args.no_draft_variant,
                      force_draft=args.draft)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
