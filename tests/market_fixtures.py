"""Hand-made recorded responses for market-data and prefill tests (no network)."""

from __future__ import annotations

import json
from datetime import date, timedelta

from coverageflow import market_data as md

CUTOFF = "2026-10-30"


def stooq_csv(rows):
    return ("Date,Open,High,Low,Close,Volume\n" + "\n".join(
        f"{d},{c},{c},{c},{c},1000" for d, c in rows) + "\n").encode()


def yahoo_json(rows, gmtoffset=-14400):
    ts = [int((date.fromisoformat(d) - date(1970, 1, 1)).total_seconds()) + 13 * 3600 + 30 * 60
          for d, _ in rows]
    return json.dumps({"chart": {"result": [{
        "meta": {"gmtoffset": gmtoffset}, "timestamp": ts,
        "indicators": {"quote": [{"close": [c for _, c in rows]}]}}]}}).encode()


def fred_csv(rows, header="observation_date,DGS10"):
    return (header + "\n" + "\n".join(f"{d},{v}" for d, v in rows) + "\n").encode()


def weekly_rows(start, n, fn):
    d = date.fromisoformat(start)
    return [((d + timedelta(days=7 * i)).isoformat(), fn(i)) for i in range(n)]


def fact(start, end, val, filed, form="10-Q"):
    e = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        e["start"] = start
    return e


def companyfacts(scale=1.0, shares=1000.0, debt=200.0, cash=300.0):
    """FY2024, FY2025 + Q1/Q2 2026 YTD with comparatives, restated Q2 filed late."""
    def dur(vals):
        return {"units": {"USD": [
            fact("2024-01-01", "2024-12-31", vals[0] * scale, "2025-02-01", "10-K"),
            fact("2025-01-01", "2025-12-31", vals[1] * scale, "2026-02-01", "10-K"),
            fact("2024-01-01", "2024-03-31", vals[2] * scale, "2025-04-30"),
            fact("2025-01-01", "2025-03-31", vals[2] * scale, "2026-04-30"),
            fact("2025-01-01", "2025-03-31", vals[3] * scale, "2025-04-30"),
            fact("2026-01-01", "2026-03-31", vals[4] * scale, "2026-04-30"),
            fact("2025-01-01", "2025-06-30", vals[5] * scale, "2026-07-30"),
            fact("2026-01-01", "2026-06-30", vals[6] * scale, "2026-07-30"),
            fact("2026-01-01", "2026-06-30", vals[6] * scale + 5, "2026-10-01"),  # restated later
        ]}}
    # vals: FY24, FY25, Q1'24, Q1'25, Q1'26, H1'25, H1'26
    # (Q1'25 reported twice: 2025 filing and 2026 comparative; the 2026 one is vals[2]?)
    return {
        "entityName": "Test Co",
        "facts": {
            "us-gaap": {
                "Revenues": dur((90, 100, 18, 20, 30, 40, 70)),
                "OperatingIncomeLoss": dur((9, 10, 1.8, 2, 3, 4, 7)),
                "DepreciationDepletionAndAmortization": dur((4.5, 5, 0.9, 1, 1.5, 2, 3.5)),
                "NetIncomeLoss": dur((6, 8, 1, 1.5, 2, 3, 5)),
                "GrossProfit": {"units": {"USD": [
                    fact("2024-01-01", "2024-12-31", 45 * scale, "2025-02-01", "10-K"),
                    fact("2025-01-01", "2025-12-31", 55 * scale, "2026-02-01", "10-K")]}},
                "IncomeTaxExpenseBenefit": {"units": {"USD": [
                    fact("2025-01-01", "2025-12-31", 2 * scale, "2026-02-01", "10-K")]}},
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {"units": {"USD": [
                    fact("2025-01-01", "2025-12-31", 10 * scale, "2026-02-01", "10-K")]}},
                "LongTermDebt": {"units": {"USD": [
                    {"end": "2025-12-31", "val": debt - 50, "filed": "2026-02-01"},
                    {"end": "2026-06-30", "val": debt, "filed": "2026-07-30"}]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                    {"end": "2026-06-30", "val": cash - 100, "filed": "2026-07-30"}]}},
                "ShortTermInvestments": {"units": {"USD": [
                    {"end": "2026-06-30", "val": 100, "filed": "2026-07-30"}]}},
                "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
                    fact("2026-04-01", "2026-06-30", shares, "2026-07-30"),
                    fact("2026-01-01", "2026-06-30", shares + 10, "2026-07-30")]}},
            }}}


def cf_bytes(**kw):
    return json.dumps(companyfacts(**kw)).encode()


class Recorded:
    """fetch(url) -> bytes from a dict; unknown URLs raise (no network)."""

    def __init__(self, mapping=None):
        self.map = dict(mapping or {})
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.map:
            raise md.MarketDataError(f"no recorded response for {url}")
        v = self.map[url]
        if isinstance(v, Exception):
            raise v
        return v


# ------------------------------------------------------------ a tiny "world"

import json as _json  # noqa: E402
from datetime import timedelta as _td  # noqa: E402

CIKS = {"INTC": 50863, "AMD": 2488, "NVDA": 1045810, "ZZZ": 999999}


def tickers_json():
    return _json.dumps({str(i): {"cik_str": c, "ticker": t, "title": t}
                        for i, (t, c) in enumerate(CIKS.items())}).encode()


def bench_weekly(n=105):
    px, out = 100.0, []
    for i, (d, _) in enumerate(weekly_rows("2024-11-01", n, lambda i: 0)):
        px *= 1 + (0.02 if i % 3 else -0.015) + 0.001 * (i % 5)
        out.append((d, px))
    return out


def levered(bench, k, start=50.0):
    px, out = start, [bench[0]]
    out = [(bench[0][0], start)]
    for (d0, p0), (d1, p1) in zip(bench, bench[1:]):
        px *= 1 + k * (p1 / p0 - 1)
        out.append((d1, px))
    return out


def world(price=23.0, beta=1.7, rf="4.20", spy_ok=True, peers=("AMD", "NVDA"),
          shares=2.0, debt=20.0, cash=30.0, extra=None):
    """Recorded responses for INTC + peers + SPY + FRED + SEC (cutoff 2026-10-30)."""
    from datetime import date
    end = date.fromisoformat(CUTOFF)
    m = {"https://www.sec.gov/files/company_tickers.json": tickers_json()}
    bench = bench_weekly()

    def px_urls(tk, daily_price, weekly):
        d = md._stooq_url(tk, end - _td(days=14), end, "d")
        w = md._stooq_url(tk, end - _td(days=365 * 2 + 7), end, "w")
        m[d] = stooq_csv([("2026-10-29", daily_price - 1), ("2026-10-30", daily_price),
                          ("2026-11-02", 999.0)])
        m[w] = stooq_csv(weekly)

    px_urls("INTC", price, levered(bench, beta))
    if spy_ok:
        px_urls("SPY", 500.0, bench)
    for i, tk in enumerate(peers):
        px_urls(tk, 40.0 + 10 * i, levered(bench, 1.0 + i))
    for tk in ("INTC", *peers):
        m[md.companyfacts_url(str(CIKS[tk]))] = cf_bytes(
            scale=1.0 + (tk != "INTC") * (1 + (tk == "NVDA")), shares=shares, debt=debt, cash=cash)
    m["https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd=2026-09-30&coed=2026-10-30"] = fred_csv(
        [("2026-10-29", rf), ("2026-10-30", "."), ("2026-11-02", "9")])
    m.update(extra or {})
    return Recorded(m)
