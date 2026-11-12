"""Structured "report_doc" for the redesigned equity-research note.

``build_report_doc(evidence)`` turns :func:`export_dossier.collect_case`
output (the verified artifacts of one case) into a renderer-neutral dict.
Both renderers (``render_note_tex`` -> PDF, ``render_note_html`` -> HTML) read
only this dict, so the two outputs cannot drift apart.

Every figure is read from an artifact (model.json / valuation.json /
mandate.json / approval records / report.md); nothing is typed in.  The
``figure_sources`` entry documents where each cover figure comes from.
Strings are plain Unicode (true minus U+2212, no markup); the renderers do
their own escaping.  Missing values are ``"n.a."``; em dashes never appear.

Document layout (``doc["body"]`` is an ordered list of blocks):
    page 1  cover: headline, key points, summary, chart (Exhibit 1), sidebar
    body    the writer's remaining sections interleaved with numbered
            exhibits, then the valuation exhibits
    last    "Sources and controls" (``doc["controls"]``)

CLI:
    python -m coverageflow.report_doc --case <case-dir> --out <dir> [--cover-only]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from . import report_exhibits as ex
from .export_dossier import collect_case
from .render_note import strip_process_sections

MINUS = "−"
EN = "–"
EV = re.compile(r"\s*\[EV:([^\]]+)\]")
NA = "n.a."

COMPANY_NAMES = {"AMD": "Advanced Micro Devices, Inc.", "INTC": "Intel Corporation"}

TARGET_BASIS = {
    "gordon_only_terminal_values_disagree": "Gordon DCF; terminal methods disagree",
    "gordon": "Gordon-growth DCF",
    "exit_multiple": "Exit-multiple DCF",
    "blend": "Blend of DCF terminal methods",
}

PUBLISHED_DECISIONS = ("APPROVED", "PUBLISHED_WITH_EXCEPTIONS")
FINAL_STATUSES = ("CLOSED", "APPROVED", "PUBLISHED_WITH_EXCEPTIONS", "PUBLISHED")
# Sections that carry exhibits, in canonical note order
_EXHIBIT_SECTIONS = ("results", "segment detail", "valuation")


# ------------------------------------------------------------- sanitising
_EM = "—―"


def clean_dashes(text: str) -> str:
    """Remove em dashes from note text (single place, deterministic).

    A pair around an aside becomes commas; a single one becomes a comma.
    En dashes are left alone (numeric ranges only) and so are minus signs.
    """
    if not isinstance(text, str) or not any(c in text for c in _EM):
        return text
    t = re.sub(rf"\s*[{_EM}]\s*([^{_EM}]+?)\s*[{_EM}]\s*", r", \1, ", text)
    t = re.sub(rf"\s*[{_EM}]\s*", ", ", t)
    return re.sub(r",\s*([.,;:])", r"\1", t)


def _clean_tree(x):
    if isinstance(x, str):
        return clean_dashes(x)
    if isinstance(x, list):
        return [_clean_tree(i) for i in x]
    if isinstance(x, dict):
        return {k: _clean_tree(v) for k, v in x.items()}
    return x


# ------------------------------------------------------------- formatting
def _num(x: Any) -> float | None:
    return None if x is None else float(x)


def f_mm(x: Any) -> str:
    x = _num(x)
    if x is None:
        return NA
    s = f"{abs(x):,.0f}"
    return (MINUS + s) if x < 0 and round(x) != 0 else s


def f_usd(x: Any, d: int = 2) -> str:
    x = _num(x)
    if x is None:
        return NA
    s = f"{abs(x):,.{d}f}"
    return (MINUS + s) if x < 0 else s


def f_pct(x: Any, d: int = 1, sign: bool = False) -> str:
    x = _num(x)
    if x is None:
        return NA
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


def _val(model: dict, field: str, period: str | None) -> float | None:
    return _num(_series(model, field).get(period)) if period else None


def _yoy(model: dict, field: str, period: str | None) -> float | None:
    y = _series(model, field).get("yoy")
    return _num(y.get(period)) if isinstance(y, dict) and period else None


def _latest_quarter(model: dict) -> str | None:
    qs = {p for s in (model.get("series") or {}).values() for p in s
          if re.fullmatch(r"\d{4}-Q\d", p)}
    return max(qs) if qs else None


def _prior_year(period: str | None) -> str | None:
    return f"{int(period[:4]) - 1}{period[4:]}" if period else None


def _quarter_label(period: str | None) -> str:
    return f"{period[5:]} FY{period[:4]}" if period else "latest quarter"


def _ytd_label(period: str | None) -> str:
    return f"{3 * int(period[6])}M FY{period[:4]}" if period else "YTD"


def _seg_name(slug: str) -> str:
    return slug.replace("_", " ").title().replace(" And ", " and ")


# ----------------------------------------------------------- headline
def build_headline(rating: str | None, upside: float | None, lead_name: str | None) -> str:
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
        self.facts = (evidence.get("facts") or {}).get("facts") or []
        self.docs = {d["doc_id"]: d for d in
                     (evidence.get("manifest") or {}).get("documents", []) if d.get("doc_id")}
        self.model, self.val = evidence.get("model") or {}, evidence.get("valuation") or {}
        self.ticker = (evidence.get("mandate") or {}).get("ticker", "")
        self.nums: dict[str, int] = {}

    def number(self, key: str) -> int:
        return self.nums.setdefault(key, len(self.nums) + 1)

    def doc_label(self, doc_id: str) -> str:
        d = self.docs.get(doc_id)
        if not d:
            return doc_id or "filing"
        return f"{self.ticker} Form {d.get('form')}, period ended {f_date(d.get('period') or d.get('filed'))}"

    def resolve(self, key: str) -> dict:
        cands = [f for f in self.facts if f.get("field") == key and f.get("citation")]
        if cands:
            pe = self.model.get("period_ends") or {}
            cands.sort(key=lambda f: (str(pe.get(f.get("period"), "")), str(f.get("period"))),
                       reverse=True)
            c = cands[0]["citation"]
            label = self.doc_label(c.get("doc_id", ""))
            return {"kind": "filing", "group": label, "source": label,
                    "doc_id": c.get("doc_id", ""), "statement": c.get("anchor") or "",
                    "item": c.get("locator") or "",
                    "locator": ", ".join(x for x in (c.get("anchor"), c.get("locator")) if x)}
        if key.startswith(("dcf.", "comps.", "assumptions.", "cross_check", "sensitivity.")):
            eng = f"CoverageFlow valuation engine ({self.val.get('engine', '')})"
            return {"kind": "engine", "group": "CoverageFlow valuation engine",
                    "source": eng, "doc_id": "", "statement": "Valuation outputs",
                    "item": key, "locator": key}
        return {"kind": "engine", "group": "CoverageFlow model engine",
                "source": f"CoverageFlow model engine ({self.model.get('engine', '')}), derived from filings",
                "doc_id": "", "statement": "Model series", "item": key, "locator": key}


# ----------------------------------------------------- note text -> blocks
def _split_sentences(para: str) -> list[str]:
    """Split on '.' / '!' / '?' + optional markers, then whitespace + capital."""
    parts, start = [], 0
    pat = re.compile(r"([.!?])((?:\s*\[EV:[^\]]+\])*)(\s+)(?=[A-Z])")
    for mt in pat.finditer(para):
        parts.append(para[start:mt.end(2)])
        start = mt.end()
    parts.append(para[start:])
    return [p for p in parts if p.strip()]


def _sentences(para: str, cites: _Cites) -> list[dict]:
    """Paragraph -> [{text, refs}]: one marker cluster per sentence."""
    para = " ".join(para.split())
    para = re.sub(r"\*\*(.+?)\*\*", r"\1", para)
    para = re.sub(r"(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)", r"\1", para)
    out = []
    for s in _split_sentences(para):
        keys = [k.strip() for mm in EV.finditer(s)
                for k in re.split(r"[;,]", mm.group(1)) if k.strip()]
        text = EV.sub("", s).strip()
        text = re.sub(r"\s+([.;,])", r"\1", text)
        refs: list[int] = []
        for k in keys:
            n = cites.number(k)
            if n not in refs:
                refs.append(n)
        out.append({"text": text, "refs": sorted(refs)})
    return out


def _parse_sections(report_md: str) -> list[tuple[str, str]]:
    """Reader sections [(heading, body)] of the note; process sections dropped."""
    body = strip_process_sections(report_md or "")
    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.M)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def _blocks(text: str, cites: _Cites) -> list[dict]:
    """Section body -> prose blocks (p, ul, h3, table)."""
    blocks: list[dict] = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.lstrip().startswith("|") for ln in lines):
            rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines
                    if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", ln)]
            if rows:
                blocks.append({"type": "table", "header": rows[0], "rows": rows[1:]})
            continue
        if lines[0].lstrip().startswith("###"):
            blocks.append({"type": "h3", "text": lines[0].lstrip("# ").strip()})
            lines = lines[1:]
            if not lines:
                continue
        if all(re.match(r"\s*[-*]\s+", ln) for ln in lines):
            blocks.append({"type": "ul", "items": [_sentences(re.sub(r"^\s*[-*]\s+", "", ln), cites)
                                                    for ln in lines]})
        else:
            blocks.append({"type": "p", "sentences": _sentences(" ".join(lines), cites)})
    return blocks


# ------------------------------------------------------------ status
def _approvals(case_dir: Path | None, given: list | None = None) -> list[dict]:
    if given is not None:
        return list(given)
    out = []
    if case_dir and (Path(case_dir) / "approvals").exists():
        for p in sorted((Path(case_dir) / "approvals").glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def _status(evidence: dict, force_draft: bool, status: str | None) -> dict:
    approval, result = evidence.get("approval") or {}, evidence.get("result") or {}
    state_status = status or (evidence.get("state") or {}).get("status")
    decision = approval.get("decision")
    terminal = result.get("terminal")
    final = decision if decision in PUBLISHED_DECISIONS else (
        terminal if terminal in PUBLISHED_DECISIONS else None)
    if final is None and terminal == "PUBLISHED":
        final = "APPROVED"
    ok_state = state_status is None or state_status in FINAL_STATUSES
    published = bool(final) and ok_state and not force_draft
    label = {"PUBLISHED_WITH_EXCEPTIONS": "Published with exceptions",
             "APPROVED": "Approved"}.get(final if published else "", "Draft for review, not approved")
    exceptions = []
    raw = approval.get("exceptions") or (
        ((result.get("reason") or {}).get("detail") or {}).get("exceptions") or [])
    if published:
        for e in raw:
            s = str(e.get("summary", "")).strip()
            short = s.split(":")[0].strip() if ":" in s else re.split(r"(?<=\.)\s", s)[0]
            exceptions.append({"id": e.get("finding_id"), "severity": e.get("severity"),
                               "summary": s, "short": short.rstrip(".")})
    return {"label": label, "published": published, "final": final if published else None,
            "exceptions": exceptions, "n_open": len(exceptions),
            "approver": approval.get("by") if published else None,
            "date": (approval.get("ts") or "")[:10] if published else "",
            "case_status": state_status}


# --------------------------------------------------------- main builder
def _compact_items(keys: list[str]) -> str:
    """Engine field keys grouped by their first path component: 'derived: a, b; segments: c.d'."""
    groups: dict[str, list[str]] = {}
    for k in keys:
        head, _, rest = k.partition(".")
        groups.setdefault(head, []).append(rest or k)
    return "; ".join(f"{h}: {', '.join(v)}" for h, v in groups.items())


def build_report_doc(evidence: dict, force_draft: bool = False, status: str | None = None) -> dict:
    mandate, model, val = evidence.get("mandate") or {}, evidence.get("model") or {}, evidence.get("valuation") or {}
    result = evidence.get("result") or {}
    dcf = val.get("dcf") or {}
    ticker = str(mandate.get("ticker", ""))
    cutoff = str(mandate.get("cutoff_date", ""))
    cur = "USD"
    src: dict[str, str] = {}
    st = _status(evidence, force_draft, status)
    published = st["published"]
    decision = st["final"]

    q = _latest_quarter(model)
    pq = _prior_year(q)
    ytd, pytd = (f"{q}-YTD" if q else None), (f"{pq}-YTD" if pq else None)
    rev, rev_p = _val(model, "income_statement.revenue", q), _val(model, "income_statement.revenue", pq)
    rev_yoy = _yoy(model, "income_statement.revenue", q)
    gm, gm_p = _val(model, "derived.gross_margin", q), _val(model, "derived.gross_margin", pq)
    oi, oi_yoy = _val(model, "income_statement.operating_income", q), _yoy(model, "income_statement.operating_income", q)
    om = _val(model, "derived.operating_margin", q)
    eps, eps_p = _val(model, "income_statement.diluted_eps", q), _val(model, "income_statement.diluted_eps", pq)

    # segments: lead = largest absolute y/y revenue gain
    seg_rows = []
    for slug in model.get("segment_slugs") or []:
        r, rp = _val(model, f"segments.{slug}.revenue", q), _val(model, f"segments.{slug}.revenue", pq)
        if r is not None and rp is not None:
            seg_rows.append({"slug": slug, "name": _seg_name(slug), "rev": r, "delta": r - rp,
                             "yoy": _yoy(model, f"segments.{slug}.revenue", q),
                             "share": _val(model, f"segments.{slug}.revenue_share_of_total", q)})
    lead = max(seg_rows, key=lambda s: s["delta"]) if seg_rows else None
    lag = min(seg_rows, key=lambda s: s["delta"]) if seg_rows else None

    rating, pt, price = dcf.get("rating"), dcf.get("target_price"), dcf.get("market_price")
    upside, shares, net_debt = dcf.get("upside"), dcf.get("diluted_shares"), dcf.get("net_debt")
    mcap = price * shares if price is not None and shares is not None else None
    basis = TARGET_BASIS.get(dcf.get("target_basis"),
                             str(dcf.get("target_basis") or "").replace("_", " ").capitalize())
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
               "value": f_pct((dcf.get("terminal", {}).get("gordon") or {}).get("terminal_growth"), 1), "note": ""})

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

    # ---- financial summary (cover sidebar)
    years = dcf.get("years") or []
    base_fy = int(re.sub(r"\D", "", (dcf.get("base") or {}).get("window", {}).get("fy") or "0") or 0)
    act = sorted(p for p in _series(model, "income_statement.revenue") if re.fullmatch(r"FY\d{4}", p))[-2:]
    cols = [{"label": p, "kind": "A", "key": p} for p in act]
    n_fc = min(3, len(years)) if base_fy else 0
    cols += [{"label": f"FY{base_fy + i + 1}", "kind": "E", "key": i} for i in range(n_fc)]

    def row(label, unit, actf, fcf, fmt):
        vals = []
        for c in cols:
            v = actf(c["key"]) if c["kind"] == "A" else (fcf(years[c["key"]]) if fcf else None)
            vals.append(None if v is None else fmt(v))
        return {"label": label, "unit": unit, "values": vals}

    fin_rows = [
        row("Revenue", "USD mm", lambda p: _val(model, "income_statement.revenue", p),
            lambda y: y.get("revenue"), f_mm),
        row("Growth y/y", "%", lambda p: _yoy(model, "income_statement.revenue", p),
            lambda y: y.get("growth"), lambda v: f_pct(v, 1)),
        row("EBITDA margin", "%", lambda p: _val(model, "derived.ebitda_margin", p),
            lambda y: y.get("ebitda_margin"), lambda v: f_pct(v, 1)),
        row("Diluted EPS", "USD", lambda p: _val(model, "income_statement.diluted_eps", p), None, f_usd),
        row("Free cash flow", "USD mm", lambda p: _val(model, "derived.fcf", p), lambda y: y.get("fcf"), f_mm),
    ]
    fin_rows = [r for r in fin_rows if any(v is not None for v in r["values"])]
    margin_note = ("Forecast margins held at the base-year level."
                   if (dcf.get("cash_flow_build") or {}).get("ebit_margin_source") == "ltm_flat" else "")
    fin_notes = [n for n in ("A = reported, E = CoverageFlow DCF base case. " + margin_note,
                             "EPS is not forecast by the model; forecast FCF is unlevered."
                             if any(r["label"] == "Diluted EPS" for r in fin_rows) else "") if n]

    # ---- prose (cover Summary + body sections) with citations
    cites = _Cites(evidence)
    sections = _parse_sections(evidence.get("report_md") or "")
    summary: list[list[dict]] = []
    body_sections: list[tuple[str, list[dict]]] = []
    for head, text in sections:
        blocks = _blocks(text, cites)
        if head.lower() == "summary":
            summary = [b["sentences"] for b in blocks if b["type"] == "p"]
            if not summary:  # list-style summary: flatten
                summary = [it for b in blocks if b["type"] == "ul" for it in b["items"]]
        else:
            body_sections.append((head, blocks))
    cover_max = max((n for p in summary for s_ in p for n in s_["refs"]), default=0)

    # ---- exhibits
    tokens = ex.exception_tokens(st["exceptions"])
    dagger_hit: set[str] = set()
    ex_by_section: dict[str, list[dict]] = {"results": [], "segment detail": [], "valuation": []}
    ql, pql = _quarter_label(q), _quarter_label(pq)
    inc = ex.income_exhibit(model, q or "", pq or "", ytd or "", pytd or "", ql, pql,
                            _ytd_label(q), _ytd_label(pq))
    if inc:
        ex_by_section["results"].append(inc)
    seg = ex.segment_exhibit(model, q or "", pq or "", ytd or "", ql, _ytd_label(q))
    if seg:
        ex_by_section["segment detail"].append(seg)
    dx = ex.dcf_exhibit(model, val, base_fy)
    pair1 = [e for e in (ex.bridge_exhibit(val), ex.sensitivity_exhibit(val)) if e]
    pair2 = [e for e in (ex.peers_exhibit(val), ex.implied_exhibit(val, ticker, base_fy)) if e]
    fb = ex.football_data(val)
    ids = ", ".join(str(x["id"]) for x in st["exceptions"])
    for e in [dx, *pair1, *pair2]:
        if e and tokens and ex.mark_daggers(e["table"], tokens):
            dagger_hit.add(e["title"])
            e["notes"].append(f"\u2020 Figure is quoted in open finding {ids}.")
    for r in fin_rows:
        for i, v in enumerate(r["values"]):
            if v and any(m.replace(",", "") in tokens for m in ex._NUM.findall(v)):
                r["values"][i] = v + "†"
                dagger_hit.add("financial summary")
    if "financial summary" in dagger_hit:
        fin_notes.append(f"† Figure is quoted in open finding {ids}.")

    chart = None
    if seg_rows:
        chart = {"title": f"Revenue by segment, {ql} vs {pql}", "unit": "USD mm",
                 "categories": [{"name": s_["name"], "note": f_pct(s_["yoy"], 1, True) + " y/y"} for s_ in seg_rows],
                 "series": [{"label": pql, "values": [_val(model, f"segments.{s_['slug']}.revenue", pq) for s_ in seg_rows]},
                            {"label": ql, "values": [s_["rev"] for s_ in seg_rows]}],
                 "source": "Source: company filings (Form 10-Q), CoverageFlow model engine.", "id": ""}

    # ---- body blocks in note order, with exhibits attached to their sections
    names = [h for h, _ in body_sections]
    lower = [h.lower() for h in names]
    order: list[tuple[str, list[dict]]] = list(body_sections)
    have_ex = {"results": bool(ex_by_section["results"]), "segment detail": bool(ex_by_section["segment detail"]),
               "valuation": bool(dx or pair1 or pair2 or fb)}
    canon = ["results", "segment detail", "valuation"]
    for k in canon:
        if have_ex[k] and k not in lower:
            # insert before the first later-canonical or "risks"/"outlook" section, else append
            idx = next((i for i, h in enumerate(lower)
                        if (k == "results" and h in ("segment detail", "outlook", "valuation", "risks"))
                        or (k == "segment detail" and h in ("outlook", "valuation", "risks"))
                        or (k == "valuation" and h == "risks")), len(order))
            order.insert(idx, (k.title() if k != "segment detail" else "Segment detail", []))
            lower.insert(idx, k)
    body: list[dict] = []
    counter = [1 if chart else 0]

    def nxt() -> str:
        counter[0] += 1
        return f"Exhibit {counter[0]}"

    if chart:
        chart["id"] = "Exhibit 1"
    for (head, blocks), low in zip(order, lower):
        body.append({"type": "h2", "text": head})
        body.extend({**b, "type": "prose", "kind": b["type"]} for b in blocks)
        if low == "results":
            for e in ex_by_section["results"]:
                e["id"] = nxt()
                body.append({"type": "exhibit", **e})
        elif low == "segment detail":
            for e in ex_by_section["segment detail"]:
                e["id"] = nxt()
                body.append({"type": "exhibit", **e})
        elif low == "valuation":
            if dx:
                dx["id"] = nxt()
                body.append({"type": "exhibit", **dx})
            for pair in (pair1, pair2):
                for e in pair:
                    e["id"] = nxt()
                if len(pair) == 2:
                    body.append({"type": "exhibit_row", "exhibits": pair})
                else:
                    body.extend({"type": "exhibit", **e} for e in pair)
            if fb:
                fb["id"] = nxt()
                body.append({"type": "football", **fb})

    # ---- citations (global numbering; cover subset flagged)
    citations = []
    for key, n in sorted(cites.nums.items(), key=lambda kv: kv[1]):
        citations.append({"n": n, "key": key, "cover": n <= cover_max, **cites.resolve(key)})

    # ---- controls page
    manifest = evidence.get("manifest") or {}
    docs = [{"doc_id": d.get("doc_id", ""), "form": d.get("form", ""), "period": f_date(d.get("period")),
             "filed": f_date(d.get("filed")), "sha": str(d.get("sha256", ""))[:12]}
            for d in manifest.get("documents", [])]
    evid_rows: list[dict] = []
    groups: dict[tuple, dict] = {}
    for c in citations:
        gk = (c["group"], c["statement"])
        g = groups.get(gk)
        if g is None:
            g = groups[gk] = {"refs": [], "source": c["group"], "statement": c["statement"],
                              "items": [], "kind": c["kind"]}
            evid_rows.append(g)
        g["refs"].append(c["n"])
        it = c["item"]
        if it and it not in g["items"]:
            g["items"].append(it)
    evid_rows.sort(key=lambda g: (g["kind"] != "filing", min(g["refs"])))
    for g in evid_rows:
        g["refs_text"] = format_refs(g["refs"])
        g["items_text"] = _compact_items(g["items"]) if g["kind"] == "engine" else "; ".join(g["items"])
    decisions = [{"id": a.get("decision_id", ""), "decision": a.get("decision", ""), "by": a.get("by", ""),
                  "date": f_date((a.get("ts") or "")[:10]), "reasoning": a.get("reasoning", "")}
                 for a in _approvals(evidence.get("case_dir"), evidence.get("approvals"))]
    cand = (evidence.get("state", {}).get("candidate") or {}).get("candidate_id") or result.get("candidate_id") or ""
    prov = [("Information cutoff", f_date(cutoff)), ("Case", str(evidence.get("case_id") or "")),
            ("Candidate", cand), ("Report version", str(result.get("report_version") or "draft")),
            ("Evidence bundle", f"{manifest.get('bundle_id', NA)} (sha256 {str(manifest.get('bundle_sha256') or '')[:12] or NA})"),
            ("Model engine", str(model.get("engine") or NA)), ("Valuation engine", str(val.get("engine") or NA)),
            ("Fact extraction prompt", str((evidence.get("facts") or {}).get("prompt_version") or NA))]
    controls = {"inputs": docs, "evidence": evid_rows, "decisions": decisions,
                "exceptions": st["exceptions"], "provenance": [{"label": a, "value": b} for a, b in prov]}

    disclaimer = (f"Generated by CoverageFlow from SEC filings and market data; information cutoff "
                  f"{f_date(cutoff)}. Automated analysis for a course project; not investment advice.")

    src.update({
        "headline": "dcf.rating, dcf.upside, segments.<slug>.revenue (largest y/y gain)",
        "key points": f"model.series income_statement.*, derived.*, segments.* [{q}, {pq}] and yoy",
        "rating / price target / price / implied return": "valuation.dcf.rating, target_price, market_price, upside",
        "key data": "valuation.dcf.net_debt, diluted_shares, wacc, terminal.gordon.terminal_growth (market cap computed)",
        "financial summary": "model.series (FY actuals) and valuation.dcf.years (forecast)",
        "exhibits": "model.series; valuation.dcf / sensitivity / comps",
        "summary and sections": "report/<report_version>.md",
        "status": "approvals/apr-*.json, result.json, state.json"})

    doc = {
        "meta": {"ticker": ticker, "company": COMPANY_NAMES.get(ticker.upper(), ticker),
                 "kicker": f"{ticker} · {_quarter_label(q)} {str(mandate.get('report_policy', {}).get('note_type', 'update')).replace('quarterly_', '')}",
                 "doc_type": "Equity Research", "date": f_date(st["date"] or cutoff), "cutoff": f_date(cutoff),
                 "cutoff_iso": cutoff, "latest_period": q, "draft": not published, "terminal": result.get("terminal") or "DRAFT",
                 "candidate_id": cand, "report_version": result.get("report_version") or "draft",
                 "case_id": evidence.get("case_id")},
        "headline": headline,
        "byline": {"author": "CoverageFlow (automated coverage)", "approver": st["approver"],
                   "approved_on": f_date(st["date"]) if st["date"] else "", "decision": decision,
                   "text": "CoverageFlow (automated coverage) · "
                           + (f"{'Published with exceptions' if decision == 'PUBLISHED_WITH_EXCEPTIONS' else 'Approved'} by {st['approver']}, {f_date(st['date'])}"
                              if published and st["approver"] else "Not yet approved")},
        "rating": {"rating": rating, "price_target": f"{cur} {f_usd(pt)}", "basis": basis,
                   "price": f"{cur} {f_usd(price)}", "price_as_of": f_date(dcf.get("market_price_as_of")),
                   "implied_return": f_pct(upside, 1, True)},
        "key_data": kd, "valuation_methods": vm,
        "financial_summary": {"columns": [{"label": c["label"], "kind": c["kind"]} for c in cols],
                              "rows": fin_rows, "notes": fin_notes, "currency_unit": "USD mm"},
        "key_points": kp, "summary": summary, "chart": chart, "football": fb,
        "body": body,
        "status": {k: st[k] for k in ("label", "published", "exceptions", "n_open", "case_status")},
        "citations": citations, "controls": controls, "disclaimer": disclaimer, "figure_sources": src,
    }
    return _clean_tree(doc)


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    from .note_build import build_note

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", required=True, help="case directory")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--cover-only", action="store_true", help="build the cover page only (cover.pdf/html)")
    ap.add_argument("--draft", action="store_true", help="force DRAFT mode")
    ap.add_argument("--no-draft-variant", action="store_true", help="skip the forced-draft variant")
    args = ap.parse_args(argv)
    ev = collect_case(Path(args.case))
    out = Path(args.out)
    res = {}
    stem = "cover" if args.cover_only else "note"
    res[stem] = build_note(ev, out, stem=stem, html_name=f"{stem}.html", force_draft=args.draft,
                           cover_only=args.cover_only)
    res[stem].pop("doc")
    if not args.no_draft_variant and not args.draft:
        res[f"{stem}-draft"] = build_note(ev, out, stem=f"{stem}-draft", html_name=f"{stem}-draft.html",
                                          force_draft=True, cover_only=args.cover_only)
        res[f"{stem}-draft"].pop("doc")
    (out / "report_doc.json").write_text(
        json.dumps(build_report_doc(ev, force_draft=args.draft), indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
