"""Prefill text: formatting helpers, basis sentences, progress callback, fetch cache."""

from __future__ import annotations

import re

import pytest

from coverageflow import mandate_prefill as mp
from coverageflow import market_data as md
from coverageflow import prefill_text as pt
from tests.market_fixtures import CUTOFF, world


def test_percent_multiple_money_and_date_helpers():
    assert pt.pct(0.049) == "4.90%" and pt.pct(0.136, 1) == "13.6%" and pt.pct(-0.022, 1) == "-2.2%"
    assert pt.pct_short(0.21) == "21%" and pt.pct_short(0.136) == "13.6%"
    assert pt.pct_short(0.10, 0) == "10%" and pt.pct_short(0.05, 0) == "5%"
    assert pt.mult(24.18) == "24.2x" and pt.mult(153.84) == "153.8x"
    assert pt.usd(1.03e8) == "USD 103m" and pt.usd(4.6e9) == "USD 4.6bn"
    assert pt.usd(2.5e12) == "USD 2.5tn" and pt.usd(12345) == "USD 12,345"
    assert pt.usd(-3e6) == "-USD 3m"
    for v in (1.03e8, 4.6e9, 1e-3, 9.99e11):
        assert "e+" not in pt.usd(v) and "e-" not in pt.usd(v)
    assert pt.day("2026-11-04") == "4 Nov 2026" and pt.day("2025-12-27T00:00") == "27 Dec 2025"
    assert pt.day(None) == "" and pt.year("2016-12-31") == "2016"
    assert pt.points(0.05) == "5 points" and pt.points(0.01) == "1 point"
    assert pt.number_list([153.84, 24.18]) == "153.8x, 24.2x"


def test_source_labels_and_caption():
    for raw, label in {"yahoo": "Yahoo Finance close", "treasury:par_yield_10y": "US Treasury",
                       "fred:DGS10": "FRED", "derived": "Computed", "default": "Default",
                       "sec:company_tickers": "SEC ticker index", "sec:companyfacts": "SEC filings",
                       "references/peer-sets.json": "Curated peer list", "mandate": "Mandate",
                       "sec:sic-3674": "SEC same-industry list"}.items():
        assert pt.source_label(raw) == label
    assert pt.caption({"source": "yahoo", "as_of": "2026-11-04", "basis": "Last close."}) \
        == "Yahoo Finance close, 4 Nov 2026. Last close."
    assert pt.caption({"source": "default", "as_of": None, "basis": "Long-run policy default."}) \
        == "Default. Long-run policy default."


@pytest.fixture(scope="module")
def prov():
    return mp.propose_mandate("INTC", CUTOFF, fetch=world())["provenance"]


def test_basis_strings_are_capitalised_sentences_without_raw_numbers(prov):
    for path, e in prov.items():
        b = e["basis"]
        assert b[0].isupper() and b.endswith((".", ")")), (path, b)
        assert not re.search(r"\d(e[+-]\d)", b)
        assert "—" not in b and ".." not in b, (path, b)


def test_specific_basis_sentences(prov):
    assert prov["assumptions.market.price"]["basis"] == "Last close on or before the cutoff."
    assert prov["assumptions.terminal_growth"]["basis"] == "Long-run policy default."
    assert prov["assumptions.dcf_comps_tolerance"]["basis"] == "Existing default, unchanged."
    w = prov["assumptions.wacc"]["basis"]
    assert re.fullmatch(r"CAPM\. Risk-free rate \d+\.\d\d% \+ \d\.\d\d beta × 5\.0% risk premium = "
                        r"\d+\.\d% cost of equity; cost of debt \d+\.\d\d% before \d+% tax; weighted "
                        r"by market value of equity and total debt\.( Beta clamped.*)?", w), w
    ex = prov["assumptions.exit_multiple_ebitda"]["basis"]
    assert re.fullmatch(r"Median peer EV/EBITDA of \d peers? \(\d+\.\dx(, \d+\.\dx)*\)\.", ex), ex
    gm = prov["assumptions.forecast_bounds.margin_floor_gross"]["basis"]
    assert gm == "Fiscal-year range 50.0%–55.0% (2024–2025), widened by 5 points." or \
        re.fullmatch(r"Fiscal-year range \d+\.\d%–\d+\.\d% \(\d{4}–\d{4}\), widened by 5 points\.", gm), gm


def test_tax_basis_outside_range_names_the_year_and_rate():
    m = mp.propose_mandate("INTC", CUTOFF, fetch=world())
    e = m["provenance"]["assumptions.tax_rate"]
    assert e["value"] in (0.21,) or e["source"] == "derived"
    assert e["basis"].startswith(("Statutory 21%.", "Effective rate for the year to "))


def test_progress_reports_each_step_in_order_with_a_stable_total():
    seen = []
    mp.propose_mandate("INTC", CUTOFF, fetch=world(), progress=lambda n, t, l: seen.append((n, t, l)))
    assert [n for n, _, _ in seen] == list(range(1, len(seen) + 1))
    labels = [l for _, _, l in seen]
    assert labels[:3] == ["Resolving INTC in the SEC ticker index", "Price for INTC",
                          "SEC filings for INTC"]
    assert "Price for AMD" in labels and any(l.startswith("SEC filings for AMD (1 of ") for l in labels)
    assert labels[-3] == "US Treasury 10-year yield"
    assert labels[-2].startswith("Two years of weekly prices for the SPY")
    assert labels[-1] == "Two years of weekly prices for INTC, for beta"
    assert all(1 <= n <= t for n, t, _ in seen)
    # skipped peers shrink the total, so the last step lands on it
    assert seen[-1][0] == seen[-1][1]


def test_progress_total_from_given_peers_is_exact():
    seen = []
    mp.propose_mandate("INTC", CUTOFF, fetch=world(), peers=["AMD", "NVDA"],
                       progress=lambda n, t, l: seen.append((n, t)))
    assert {t for _, t in seen} == {len(seen)} == {3 + 2 * 2 + 3}


# ------------------------------------------------------------------ fetch cache

class _Counting:
    def __init__(self):
        self.calls = []

    def install(self, monkeypatch):
        import urllib.request

        class Resp:
            def __init__(s, body): s.body = body
            def read(s): return s.body
            def __enter__(s): return s
            def __exit__(s, *a): return False

        def urlopen(req, timeout=0):
            self.calls.append(req.full_url)
            return Resp(b"payload:" + req.full_url.encode())

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(md, "MARKET_MIN_INTERVAL", 0.0)


def test_default_fetch_cache_hits_return_identical_bytes(monkeypatch):
    md.clear_fetch_cache()
    c = _Counting()
    c.install(monkeypatch)
    url = "https://example.test/a"
    f = md.default_fetch()
    a = f(url)
    b = md.default_fetch()(url)          # a new fetch shares the process cache
    assert a == b and md._sha(a) == md._sha(b) and c.calls == [url]
    md.default_fetch()("https://example.test/other")
    assert len(c.calls) == 2
    md.clear_fetch_cache()


def test_default_fetch_cache_can_be_bypassed_and_expires(monkeypatch):
    md.clear_fetch_cache()
    c = _Counting()
    c.install(monkeypatch)
    url = "https://example.test/b"
    md.default_fetch(cache=False)(url)
    md.default_fetch(cache=False)(url)
    assert len(c.calls) == 2
    md.clear_fetch_cache()
    md.default_fetch(cache_ttl=-1.0)(url)
    md.default_fetch(cache_ttl=-1.0)(url)
    assert len(c.calls) == 4
    md.clear_fetch_cache()
