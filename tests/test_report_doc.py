"""report_doc / render_note_tex / render_note_html: the redesigned research note.

Builds a small but complete case (rating, price, segments, DCF years,
sensitivity, comps, decision record), then checks that the cover and exhibits
carry the artifact values, that the PDF/HTML renderers agree, that no em dash
reaches an output, that the case status drives the headline status and the
DRAFT watermark, and that the HTML is self-contained.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from coverageflow import note_build, note_charts
from coverageflow.export_dossier import build_dossier, collect_case
from coverageflow.render_note_html import render_note_html
from coverageflow.render_note_tex import render_note_tex
from coverageflow.report_doc import (MINUS, build_report_doc, clean_dashes, format_refs)
from tests.test_export_dossier import CUT, _write, make_mini_case

EM = "—"

REPORT = (
    "# INTC update\n\n## Summary\n"
    "Revenue rose to USD 13,500mm, up 8.0% year over year " + EM + " a strong quarter "
    + EM + " with margins up [EV:income_statement.revenue] [EV:income_statement.gross_profit] "
    "[EV:derived.revenue_yoy] [EV:derived.gross_margin]. The DCF gives USD 4.11 per share "
    "[EV:dcf.per_diluted_share.gordon].\n\n"
    "## Results\nGross profit was USD 5,175mm [EV:income_statement.gross_profit]. Net income was "
    "USD 700mm.\n\n## Segment detail\nCCG leads the recovery [EV:segments.ccg.revenue].\n\n"
    "## Outlook\nFoundry ramp supports the guide.\n\n"
    "## Valuation\nDCF at USD 4.11 [EV:dcf.per_diluted_share.gordon] versus USD 14.40 on the exit multiple.\n\n"
    "## Risks\nExecution risk on 18A.\n\n"
    "## Open findings\n\nharness text zzz\n")


def _model() -> dict:
    s = {
        "income_statement.revenue": {"2026-Q3": 13500.0, "2025-Q3": 12500.0, "2026-Q3-YTD": 39000.0,
                                     "2025-Q3-YTD": 36000.0, "FY2024": 50000.0, "FY2025": 52000.0,
                                     "yoy": {"2026-Q3": 0.08, "FY2025": 0.04}},
        "income_statement.gross_profit": {"2026-Q3": 5175.0, "2025-Q3": 4375.0, "2026-Q3-YTD": 14000.0,
                                          "2025-Q3-YTD": 12000.0},
        "income_statement.operating_expenses": {"2026-Q3": 4350.0, "2025-Q3": 4200.0},
        "income_statement.operating_income": {"2026-Q3": 825.0, "2025-Q3": 175.0, "yoy": {"2026-Q3": 3.714}},
        "income_statement.net_income": {"2026-Q3": 700.0, "2025-Q3": 150.0},
        "income_statement.diluted_eps": {"2026-Q3": 0.16, "2025-Q3": 0.03, "FY2024": 0.4, "FY2025": 0.5},
        "derived.gross_margin": {"2026-Q3": 0.3833, "2025-Q3": 0.35},
        "derived.operating_margin": {"2026-Q3": 0.0611, "2025-Q3": 0.014},
        "derived.ebitda_margin": {"FY2024": 0.2, "FY2025": 0.22},
        "derived.fcf": {"FY2024": 900.0, "FY2025": 1200.0},
        "segments.data_center.revenue": {"2026-Q3": 6000.0, "2025-Q3": 4000.0, "yoy": {"2026-Q3": 0.5}},
        "segments.data_center.operating_income": {"2026-Q3": 1500.0},
        "segments.data_center.revenue_share_of_total": {"2026-Q3": 0.444},
        "segments.client.revenue": {"2026-Q3": 7500.0, "2025-Q3": 8500.0, "yoy": {"2026-Q3": -0.1176}},
        "segments.client.operating_income": {"2026-Q3": 600.0},
        "segments.client.revenue_share_of_total": {"2026-Q3": 0.556},
    }
    return {"artifact": "model", "engine": "model_engine@1.0", "cutoff": "2026-10-30",
            "series": s, "segment_slugs": ["data_center", "client"],
            "period_ends": {"2026-Q3": "2026-09-26", "2025-Q3": "2025-09-27"}}


def _years(n=5):
    return [{"year": i + 1, "revenue": 54000.0 * (1.05 ** i), "growth": 0.05, "ebitda": 11000.0 + 100 * i,
             "ebitda_margin": 0.2, "ebit": 9000.0, "capex": 1300.0, "fcf": 1400.0 + 100 * i,
             "discount_factor": 0.9 - 0.05 * i, "pv": 1268.0 + 10 * i} for i in range(n)]


def _valuation() -> dict:
    return {"artifact": "valuation", "engine": "valuation_engine@2.0", "cutoff": "2026-10-30",
            "dcf": {"wacc": 0.104, "rating": "Sell", "target_price": 4.11, "target_basis": "gordon",
                    "market_price": 40.0, "market_price_as_of": "2026-10-30", "upside": -0.8973,
                    "diluted_shares": 4375.0, "shares_period": "2026-Q3", "net_debt": 27000.0,
                    "net_debt_period": "2026-Q3",
                    "base": {"revenue": 52000.0, "ebitda": 11000.0, "ebitda_margin": 0.21, "fcf": 1200.0,
                             "window": {"fy": "FY2025"}},
                    "years": _years(), "pv_sum": 5000.0,
                    "terminal": {"gordon": {"terminal_growth": 0.025, "terminal_value": 20000.0, "pv": 13000.0},
                                 "exit_multiple": {"multiple": 14.0, "terminal_value": 90000.0, "pv": 60000.0},
                                 "implied_exit_multiple_from_gordon": 5.0, "implied_growth_from_exit": 0.07},
                    "ev": {"gordon": 18000.0, "exit_multiple": 65000.0},
                    "equity_value": {"gordon": 18000.0, "exit_multiple": 63000.0},
                    "per_diluted_share": {"gordon": 4.11, "exit_multiple": 14.4},
                    "bridge": {"net_debt": 27000.0, "nci": 0.0, "nci_source": "absent"},
                    "discount_convention": "year_end",
                    "cash_flow_build": {"ebit_margin_source": "ltm_flat"}},
            "comps": {"as_of": "2026-10-30",
                      "peers": [{"ticker": "AAA", "ev_ebitda": 10.0, "p_e_ttm": 15.0, "p_s": 2.0,
                                 "rev_growth_yoy": 0.05}],
                      "applied": {"ev_ebitda": {"peer_median": 10.0, "own_metric": 11000.0,
                                                "implied_ev": 110000.0, "implied_price": 19.0},
                                  "p_s": {"peer_median": 2.0, "own_metric": 52000.0, "implied_price": 12.0}},
                      "own_band": {"ev_ebitda": {"band": [8.0, 12.0], "implied_price": [15.0, 23.0]},
                                   "p_s": {"band": [1.5, 2.5], "implied_price": [9.0, 15.0]}},
                      "comps_price": 15.5, "comps_upside": -0.61},
            "sensitivity": {"basis": "gordon", "wacc_points": [0.094, 0.104, 0.114],
                            "terminal_growth_points": [0.015, 0.025, 0.035],
                            "grid": [[4.5, 5.0, 5.6], [3.7, 4.11, 4.6], [3.1, 3.4, 3.8]]},
            "cross_check_note": "DCF and relative valuation disagree beyond tolerance.", "within_tolerance": False}


def make_rich_case(root: Path, *, terminal="APPROVED", decision="APPROVED", status="CLOSED",
                   exceptions=None) -> Path:
    case = make_mini_case(root)
    gen = case / "rounds" / "round-001" / "generator"
    _write(gen / "model.json", _model())
    _write(gen / "valuation.json", _valuation())
    _write(gen / "facts.json", {"facts": [
        {"field": "income_statement.revenue", "period": "2026-Q3", "value": 13500.0,
         "citation": {"doc_id": "intc-10q-2026q3", "anchor": "Condensed Consolidated Statements of Operations",
                      "locator": "Net revenue"}},
        {"field": "income_statement.gross_profit", "period": "2026-Q3", "value": 5175.0,
         "citation": {"doc_id": "intc-10q-2026q3", "anchor": "Condensed Consolidated Statements of Operations",
                      "locator": "Gross profit"}}]})
    man = json.loads((case / "inputs" / "manifest.json").read_text())
    man["documents"][0].update({"period": "2026-09-26"})
    _write(case / "inputs" / "manifest.json", man)
    (case / "report" / "v1-cand-012.md").write_text(REPORT, encoding="utf-8")
    st = json.loads((case / "state.json").read_text())
    st["status"] = status
    _write(case / "state.json", st)
    res = json.loads((case / "result.json").read_text())
    res["terminal"] = terminal
    if exceptions:
        res["reason"] = {"code": terminal, "detail": {"exceptions": exceptions}}
    _write(case / "result.json", res)
    ap = {"decision_id": "apr-0001", "case_id": CUT, "decision": decision, "by": "Gaetano Marco Chiantia",
          "ts": "2026-11-09T17:00:00Z", "reasoning": "verified"}
    if exceptions:
        ap["exceptions"] = exceptions
    _write(case / "approvals" / "apr-0001.json", ap)
    return case


@pytest.fixture()
def rich(tmp_path):
    return make_rich_case(tmp_path)


def _all_text(doc) -> str:
    return json.dumps(doc, ensure_ascii=False)


# ------------------------------------------------------------ cover figures
def test_cover_figures_equal_artifacts(rich):
    ev = collect_case(rich)
    doc = build_report_doc(ev)
    val, mod = _valuation()["dcf"], _model()["series"]
    assert doc["rating"]["rating"] == val["rating"]
    assert doc["rating"]["price_target"] == f"USD {val['target_price']:.2f}"
    assert doc["rating"]["price"] == f"USD {val['market_price']:.2f}"
    assert doc["rating"]["implied_return"] == MINUS + f"{abs(val['upside']) * 100:.1f}%"
    kd = {k["label"]: k["value"] for k in doc["key_data"]}
    assert kd["Market cap"] == f"USD {val['market_price'] * val['diluted_shares']:,.0f}mm"
    assert kd["Net debt"] == f"USD {val['net_debt']:,.0f}mm"
    assert kd["WACC"] == "10.4%"
    revenue_kp = doc["key_points"][0]["text"]
    assert "13,500" in revenue_kp and "+8.0%" in revenue_kp and "12,500" in revenue_kp
    # headline is built from rating, upside and the segment with the largest gain
    assert doc["headline"] == "Data Center carries the quarter; DCF value is 90% below the price"
    fs = doc["financial_summary"]
    assert [c["label"] for c in fs["columns"]] == ["FY2024", "FY2025", "FY2026", "FY2027", "FY2028"]
    rows = {r["label"]: r["values"] for r in fs["rows"]}
    assert rows["Revenue"][:2] == ["50,000", "52,000"]
    assert rows["Revenue"][2] == f"{_years()[0]['revenue']:,.0f}"
    assert rows["Diluted EPS"] == ["0.40", "0.50", None, None, None]   # no EPS forecast in the model


# ----------------------------------------------------------- exhibit tracing
_NUM = re.compile(r"[-−+]?\d[\d,]*(?:\.\d+)?")


def _floats(x):
    if isinstance(x, bool):
        return
    if isinstance(x, (int, float)):
        yield float(x)
    elif isinstance(x, dict):
        for v in x.values():
            yield from _floats(v)
    elif isinstance(x, list):
        for v in x:
            yield from _floats(v)


def test_every_exhibit_number_traces_to_the_artifacts(rich):
    doc = build_report_doc(collect_case(rich))
    art = set(_floats(_model())) | set(_floats(_valuation()))
    # market cap style products are not table cells; sums of two artifact values are (the bridge)
    checked = 0
    exhibits = []
    for b in doc["body"]:
        if b["type"] == "exhibit":
            exhibits.append(b)
        elif b["type"] == "exhibit_row":
            exhibits.extend(b["exhibits"])
    assert len(exhibits) >= 6
    for e in exhibits:
        header = e["table"]["header"]
        for r in e["table"]["rows"]:
            for col, cell in enumerate(r["cells"][1:], start=1):
                if cell in ("n.a.", "") or header[col] in ("y/y", "% of group") or cell.endswith("pp"):
                    continue
                m = _NUM.search(cell.replace("†", ""))
                assert m, (e["title"], cell)
                txt = m.group(0).replace(MINUS, "").replace(",", "").lstrip("+-")
                dec = len(txt.split(".")[1]) if "." in txt else 0
                val = float(txt)
                pct = "%" in cell
                ok = any(round(abs(a) * (100 if pct else 1), dec) == val for a in art) \
                    or any(round(abs(a), dec) == val for a in art)
                assert ok, (e["title"], r["cells"][0], cell)
                checked += 1
    assert checked > 60


def test_exhibits_are_numbered_and_sourced(rich):
    doc = build_report_doc(collect_case(rich))
    ids = [doc["chart"]["id"]]
    for b in doc["body"]:
        if b["type"] in ("exhibit", "football"):
            ids.append(b["id"])
            assert b["source"].startswith("Source:")
        elif b["type"] == "exhibit_row":
            for e in b["exhibits"]:
                ids.append(e["id"])
                assert e["source"].startswith("Source:")
    assert ids == [f"Exhibit {i}" for i in range(1, len(ids) + 1)]
    heads = [b["text"] for b in doc["body"] if b["type"] == "h2"]
    assert heads == ["Results", "Segment detail", "Outlook", "Valuation", "Risks"]


def test_sensitivity_base_case_is_boxed(rich):
    doc = build_report_doc(collect_case(rich))
    sens = next(e for b in doc["body"] if b["type"] == "exhibit_row" for e in b["exhibits"]
                if e["title"].startswith("Gordon DCF value"))
    boxed = [(r["cells"][0], r["cells"][c]) for r in sens["table"]["rows"] for c in r.get("box", [])]
    assert boxed == [("10.4%", "4.11")]


# ------------------------------------------------------------------ prose
def test_process_sections_are_not_rendered_and_citations_collapse(rich):
    doc = build_report_doc(collect_case(rich))
    assert "zzz" not in _all_text(doc)
    first = doc["summary"][0][0]
    assert first["refs"] == [1, 2, 3, 4]                 # one cluster per sentence
    assert format_refs([1, 2, 3, 4, 7]) == "1–4, 7"
    assert format_refs([5, 6]) == "5, 6"
    assert "[EV:" not in _all_text(doc)
    # citations resolve to the filing statement or to an engine
    by_key = {c["key"]: c for c in doc["citations"]}
    assert by_key["income_statement.revenue"]["kind"] == "filing"
    assert by_key["income_statement.revenue"]["statement"].startswith("Condensed")
    assert by_key["dcf.per_diluted_share.gordon"]["group"] == "CoverageFlow valuation engine"


# ------------------------------------------------------------- em dashes
def test_clean_dashes_helper():
    assert clean_dashes(f"a {EM} b {EM} c") == "a, b, c"
    assert clean_dashes(f"a{EM}b") == "a, b"
    assert clean_dashes(f"up {EM}.") == "up."
    assert clean_dashes("1–4 and −8") == "1–4 and −8"        # en dash and minus survive


def test_no_em_dash_in_any_output(tmp_path):
    exc = [{"finding_id": "fnd-0007", "severity": "major",
            "summary": f"The note cites values {EM} USD 6,697mm FCF {EM} that are not supported."}]
    case = make_rich_case(tmp_path, terminal="PUBLISHED_WITH_EXCEPTIONS", decision="PUBLISHED_WITH_EXCEPTIONS",
                          exceptions=exc)
    doc = build_report_doc(collect_case(case))
    tex, html = render_note_tex(doc, {}), render_note_html(doc, {})
    assert EM not in tex and EM not in html and "---" not in tex
    assert EM not in _all_text(doc)
    built = note_build.build_note(collect_case(case), tmp_path / "o", html_name="n.html", compile_pdf=False)
    assert EM not in Path(built["tex"]).read_text() and EM not in Path(built["html"]).read_text()
    bad = tmp_path / "bad.html"
    bad.write_text(f"x {EM} y", encoding="utf-8")
    with pytest.raises(AssertionError):
        note_build.assert_no_em_dash(bad)


# ----------------------------------------------------------------- status
def test_published_with_exceptions_status(tmp_path):
    exc = [{"finding_id": "fnd-0007", "severity": "major",
            "summary": "Cited values do not match: USD 6,697mm FCF is not supported by the cited capex."}]
    case = make_rich_case(tmp_path, terminal="PUBLISHED_WITH_EXCEPTIONS", decision="PUBLISHED_WITH_EXCEPTIONS",
                          exceptions=exc)
    doc = build_report_doc(collect_case(case))
    assert doc["status"]["label"] == "Published with exceptions" and doc["status"]["n_open"] == 1
    assert doc["meta"]["draft"] is False
    assert "Published with exceptions by Gaetano Marco Chiantia" in doc["byline"]["text"]
    tex, html = render_note_tex(doc, {}), render_note_html(doc, {})
    for out in (tex, html):
        assert "Published with exceptions" in out and "fnd-0007" in out
        assert "Draft for review" not in out
    assert r"\draftfalse" in tex and 'class="draft"' not in html
    # the dagger marks the figure quoted by the finding (FCF 1,200 is not quoted; the check is by number)
    flat = _all_text(doc)
    assert "†" not in flat or "fnd-0007" in flat
    assert doc["controls"]["exceptions"][0]["id"] == "fnd-0007"


@pytest.mark.parametrize("status", ["PENDING_APPROVAL", "ESCALATED", "EXHAUSTED"])
def test_unfinished_case_is_a_draft_with_watermark_on_every_page(tmp_path, status):
    case = make_rich_case(tmp_path, status=status)
    doc = build_report_doc(collect_case(case))
    assert doc["meta"]["draft"] is True and doc["status"]["label"].startswith("Draft for review")
    tex = render_note_tex(doc, {})
    assert r"\drafttrue" in tex
    template = Path("references/note-template.tex").read_text()
    assert r"\AddToShipoutPictureFG{" in template      # not the starred (single-page) form
    html = render_note_html(doc, {})
    assert 'class="draft"' in html and "position:fixed" in html


def test_service_status_override_and_force_draft(rich):
    ev = collect_case(rich)
    assert build_report_doc(ev)["meta"]["draft"] is False
    assert build_report_doc(ev, force_draft=True)["meta"]["draft"] is True
    assert build_report_doc(ev, status="PENDING_APPROVAL")["meta"]["draft"] is True
    assert build_report_doc(ev, status="PUBLISHED_WITH_EXCEPTIONS")["meta"]["draft"] is False


def test_decision_record_lists_every_approval(tmp_path):
    case = make_rich_case(tmp_path)
    _write(case / "approvals" / "apr-0000.json", {
        "decision_id": "apr-0000", "case_id": CUT, "decision": "REVISION_REQUESTED", "by": "R. Reviewer",
        "ts": "2026-11-08T10:00:00Z", "reasoning": "tighten the outlook"})
    doc = build_report_doc(collect_case(case))
    got = [(d["id"], d["decision"]) for d in doc["controls"]["decisions"]]
    assert got == [("apr-0000", "REVISION_REQUESTED"), ("apr-0001", "APPROVED")]
    assert doc["controls"]["inputs"][0]["sha"] == "a" * 12


# ------------------------------------------------------------------- HTML
def test_html_is_self_contained_and_matches_pdf_content(rich):
    doc = build_report_doc(collect_case(rich))
    charts = note_charts.make_charts(doc)
    html = render_note_html(doc, {k: v[0] for k, v in charts.items()})
    assert not re.search(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', html)
    assert not re.search(r"https?://", html)
    assert "<script" not in html and "@import" not in html and not re.search(r"url\((?!#)", html)
    tex = render_note_tex(doc, {})
    for needle in ("USD 4.11", "13,500", "Sources and controls", "Exhibit 4"):
        assert needle.replace(" ", "~") in tex or needle in tex
        assert needle in html
    assert html.count("<svg") == len(charts)
    for c in doc["citations"]:
        assert f'id="n{c["n"]}"' in html                       # every superscript has a target


def test_note_renders_without_matplotlib(rich, tmp_path, monkeypatch):
    monkeypatch.setattr(note_charts, "matplotlib", None)
    assert note_charts.make_charts({"chart": {"x": 1}}) == {}
    built = note_build.build_note(collect_case(rich), tmp_path / "o", html_name="n.html", compile_pdf=False)
    assert Path(built["tex"]).exists() and "<svg" not in Path(built["html"]).read_text()


def test_sparse_case_renders_with_n_a(tmp_path):
    """run1-clean style data: no rating, no price, no segments: n.a., never a crash."""
    case = make_mini_case(tmp_path)
    doc = build_report_doc(collect_case(case))
    assert doc["rating"]["price"] == "USD n.a."
    tex, html = render_note_tex(doc, {}), render_note_html(doc, {})
    assert "n.a." in tex and "n.a." in html


# -------------------------------------------------------------------- PDF
def test_full_pdf_compiles_without_overfull_boxes(rich, tmp_path):
    if not shutil.which("pdflatex"):
        pytest.skip("pdflatex not installed")
    built = note_build.build_note(collect_case(rich), tmp_path / "o", html_name=None)
    assert built["compiled"] is True, built
    assert not [w for w in built["warnings"] if "Overfull" in w or w.startswith("! ")], built["warnings"]
    pdf = Path(built["pdf"]).read_bytes()
    assert pdf[:5] == b"%PDF-"
    if shutil.which("pdfinfo"):
        import subprocess
        info = subprocess.run(["pdfinfo", built["pdf"]], capture_output=True, text=True).stdout
        pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
        assert 3 <= pages <= 7
    out = build_dossier(rich)
    assert out["compiled"] is True and Path(out["pdf"]).exists()
    assert Path(out["pdf"]).name.startswith(rich.name + "-")
    assert "-dossier" in Path(out["pdf"]).name
