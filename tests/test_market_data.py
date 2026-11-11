"""Market-data providers against recorded fixtures (no network)."""

from __future__ import annotations

import hashlib
import math
from datetime import date

import pytest

from coverageflow import market_data as md
from tests.market_fixtures import (CUTOFF, Recorded, cf_bytes, companyfacts, fred_csv,
                                   stooq_csv, weekly_rows, yahoo_json)

END = date.fromisoformat(CUTOFF)


def _urls(ticker, interval="d", yahoo="1d", lookback=14, years=None):
    from datetime import timedelta
    d1 = END - timedelta(days=(365 * years + 7) if years else lookback)
    return md._stooq_url(ticker, d1, END, interval), md._yahoo_url(ticker, d1, END, yahoo)


ROWS = [("2026-10-27", 20.0), ("2026-10-28", 21.0), ("2026-10-29", 22.5),
        ("2026-10-30", 23.0), ("2026-11-02", 99.0)]   # last row post-dates the cutoff


class TestDailyClose:
    def test_stooq_primary_never_after_cutoff(self):
        s, _ = _urls("INTC")
        raw = stooq_csv(ROWS)
        r = md.daily_close("INTC", CUTOFF, Recorded({s: raw}))
        assert (r["price"], r["date"], r["source"]) == (23.0, "2026-10-30", "stooq")
        assert r["sha256"] == hashlib.sha256(raw).hexdigest()
        assert r["url"] == s and r["fetched_at"]

    def test_weekend_cutoff_uses_prior_friday(self):
        cutoff = "2026-10-31"
        d1 = date(2026, 10, 31) - __import__("datetime").timedelta(days=14)
        url = md._stooq_url("INTC", d1, date(2026, 10, 31), "d")
        r = md.daily_close("INTC", cutoff, Recorded({url: stooq_csv(ROWS)}))
        assert r["date"] == "2026-10-30"

    def test_only_post_cutoff_rows_is_a_failure(self):
        s, y = _urls("INTC")
        f = Recorded({s: stooq_csv([("2026-11-02", 5.0)]),
                      y: yahoo_json([("2026-11-02", 5.0)])})
        with pytest.raises(md.MarketDataError):
            md.daily_close("INTC", CUTOFF, f)

    def test_yahoo_fallback_when_stooq_has_no_data(self):
        s, y = _urls("INTC")
        f = Recorded({s: b"No data", y: yahoo_json(ROWS)})
        r = md.daily_close("INTC", CUTOFF, f)
        assert (r["price"], r["date"], r["source"]) == (23.0, "2026-10-30", "yahoo")
        assert r["failures"] and "stooq" in r["failures"][0]

    def test_yahoo_fallback_when_stooq_errors(self):
        s, y = _urls("INTC")
        f = Recorded({s: md.MarketDataError("boom"), y: yahoo_json(ROWS)})
        assert md.daily_close("INTC", CUTOFF, f)["source"] == "yahoo"

    def test_all_providers_fail(self):
        with pytest.raises(md.MarketDataError, match="all price providers failed"):
            md.daily_close("INTC", CUTOFF, Recorded())


def test_weekly_closes_and_two_year_window():
    s, _ = _urls("SPY", "w", "1wk", years=2)
    rows = weekly_rows("2024-11-01", 105, lambda i: 100 + i) + [("2026-11-06", 1.0)]
    r = md.weekly_closes("SPY", CUTOFF, Recorded({s: stooq_csv(rows)}))
    assert len(r["series"]) == 105 and r["series"][-1][0] == "2026-10-30"
    assert all(d <= CUTOFF for d, _ in r["series"])


class TestBeta:
    def _bench(self, n=60):
        # deterministic zig-zag returns with variance
        px, out = 100.0, []
        for i, (d, _) in enumerate(weekly_rows("2025-01-03", n, lambda i: 0)):
            px *= 1 + (0.02 if i % 3 else -0.015) + 0.001 * (i % 5)
            out.append((d, px))
        return out

    def test_recovers_known_beta(self):
        bench = self._bench()
        rets = [bench[i + 1][1] / bench[i][1] - 1 for i in range(len(bench) - 1)]
        px, stock = 50.0, [(bench[0][0], 50.0)]
        for (d, _), r in zip(bench[1:], rets):
            px *= 1 + 1.7 * r
            stock.append((d, px))
        r = md.regress_beta(stock, bench)
        assert math.isclose(r["beta"], 1.7, rel_tol=1e-9)
        assert r["n_obs"] == len(bench) - 1 and r["r_squared"] > 0.999

    def test_aligns_by_iso_week_when_providers_label_differently(self):
        bench = self._bench()
        # shift the stock's labels from Friday to Monday of the same ISO week
        from datetime import timedelta
        stock = [((date.fromisoformat(d) - timedelta(days=4)).isoformat(), p)
                 for d, p in bench]
        assert math.isclose(md.regress_beta(stock, bench)["beta"], 1.0)

    def test_too_few_points(self):
        with pytest.raises(md.MarketDataError):
            md.regress_beta(self._bench(10), self._bench(10))


class TestRiskFree:
    def test_last_value_on_or_before_cutoff_skips_dots_and_later(self):
        raw = fred_csv([("2026-10-28", "4.10"), ("2026-10-29", "4.20"),
                        ("2026-10-30", "."), ("2026-11-02", "9.99")])
        url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
               "&cosd=2026-09-30&coed=2026-10-30")
        r = md.risk_free(CUTOFF, Recorded({url: raw}))
        assert (r["rate"], r["date"]) == (0.042, "2026-10-29")
        assert r["source"] == "fred:DGS10" and r["sha256"]

    def test_legacy_header_and_no_data(self):
        url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
               "&cosd=2026-09-30&coed=2026-10-30")
        assert md.risk_free(CUTOFF, Recorded({url: fred_csv([("2026-10-20", "4.0")], "DATE,DGS10")}))["rate"] == 0.04
        with pytest.raises(md.MarketDataError):
            md.risk_free(CUTOFF, Recorded({url: fred_csv([("2026-11-02", "4.0")])}))


class TestFundamentals:
    def _f(self, cutoff, **kw):
        url = md.companyfacts_url("0000050863")
        return md.fundamentals("50863", cutoff, Recorded({url: cf_bytes(**kw)}))

    def test_ttm_is_fy_plus_ytd_minus_prior_ytd(self):
        m = self._f("2026-08-01")["metrics"]
        # FY25 100 + H1'26 70 - H1'25 40
        assert m["revenue"]["value"] == 130 and m["revenue"]["as_of"] == "2026-06-30"
        assert m["revenue"]["tag"] == "us-gaap:Revenues"
        assert m["operating_income"]["value"] == pytest.approx(13)
        assert m["da"]["value"] == pytest.approx(6.5)
        assert m["net_income"]["value"] == pytest.approx(8 + 5 - 3)

    def test_only_facts_filed_on_or_before_cutoff(self):
        # restated H1'26 (filed 2026-10-01) is invisible before then
        assert self._f("2026-09-30")["metrics"]["revenue"]["value"] == 130
        assert self._f("2026-10-30")["metrics"]["revenue"]["value"] == 135
        # between the FY25 10-K (2026-02-01) and the Q1 10-Q (2026-04-30): FY only
        r = self._f("2026-03-01")["metrics"]["revenue"]
        assert r["value"] == 100 and "no later 10-Q" in r["basis"]
        # before FY25 was filed: FY24 + Q1'25 - Q1'24 would need Q1'25 (filed 2025-04-30)
        r = self._f("2026-01-15")["metrics"]["revenue"]
        assert r["value"] == pytest.approx(90 + 20 - 18)

    def test_instants_and_shares(self):
        m = self._f("2026-08-01", debt=200, cash=300, shares=1000)["metrics"]
        assert m["total_debt"]["value"] == 200 and m["total_debt"]["as_of"] == "2026-06-30"
        assert m["cash"]["value"] == 300 and "+" in m["cash"]["tag"]
        assert m["diluted_shares"]["value"] == 1000   # shortest span at latest end
        # nothing filed yet -> missing, not invented
        early = self._f("2025-01-01")
        assert early["metrics"] == {} and "revenue" in early["missing"]

    def test_history_and_effective_tax(self):
        h = self._f("2026-08-01")["history"]
        assert h["gross_margin"][-1][1] == pytest.approx(0.55)
        assert h["revenue_growth"][-1][1] == pytest.approx(100 / 90 - 1)
        assert h["effective_tax_rate"]["value"] == pytest.approx(0.2)

    def test_provenance_fields_and_bad_payload(self):
        f = self._f("2026-08-01")
        assert f["sha256"] and f["url"].endswith("CIK0000050863.json")
        url = md.companyfacts_url("50863")
        with pytest.raises(md.MarketDataError):
            md.fundamentals("50863", CUTOFF, Recorded({url: b"{}"}))


def test_default_fetch_refuses_nothing_but_uses_generic_agent():
    f = md.default_fetch()
    assert callable(f)
    from coverageflow import bundle
    assert "@example.com" in bundle.USER_AGENT
