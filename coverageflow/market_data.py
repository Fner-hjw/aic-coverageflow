"""Market-data providers for the mandate prefill (stdlib only).

Every network read goes through ONE injectable ``fetch(url) -> bytes`` so tests
use recorded fixtures and never touch the network. ``default_fetch()`` builds
the production fetch: SEC hosts (www.sec.gov, data.sec.gov) go through
bundle.EdgarFetcher (throttled, EDGAR User-Agent), everything else through
urllib with the same generic project User-Agent. No personal name or e-mail is
ever sent; the contact in the User-Agent is bundle.py's generic placeholder.

Providers
---------
* Daily close on or before a date: Yahoo chart API, fallback Stooq CSV.
* Weekly closes (2 years, for beta): same providers, interval weekly.
* Risk-free rate: US Treasury daily par yield curve (10Y), fallback FRED DGS10;
  last value on or before the cutoff.
* Fundamentals: SEC XBRL companyfacts, using only facts with ``filed`` <= cutoff.

Every record carries ``source``, ``url``, ``sha256`` of the raw response and
``fetched_at``. A date after the cutoff is never returned.

us-gaap tags tried (first match by latest period end; ties keep list order)
--------------------------------------------------------------------------
revenue            Revenues, RevenueFromContractWithCustomerExcludingAssessedTax,
                   RevenueFromContractWithCustomerIncludingAssessedTax, SalesRevenueNet
operating income   OperatingIncomeLoss
D&A                DepreciationDepletionAndAmortization,
                   DepreciationAmortizationAndAccretionNet, DepreciationAndAmortization,
                   DepreciationAmortizationAndOther
net income         NetIncomeLoss (attributable to parent), fallback ProfitLoss
diluted shares     WeightedAverageNumberOfDilutedSharesOutstanding (unit shares),
                   fallback dei:EntityCommonStockSharesOutstanding
total debt         DebtLongtermAndShorttermCombinedAmount; else LongTermDebt
                   (+ ShortTermBorrowings); else LongTermDebtNoncurrent +
                   (DebtCurrent | LongTermDebtCurrent) + ShortTermBorrowings
cash & ST inv.     CashCashEquivalentsAndShortTermInvestments; else
                   CashAndCashEquivalentsAtCarryingValue + first of ShortTermInvestments,
                   MarketableSecuritiesCurrent, AvailableForSaleSecuritiesDebtSecuritiesCurrent
gross profit       GrossProfit (history only)
income tax / pretax IncomeTaxExpenseBenefit / IncomeLossFromContinuingOperations
                   BeforeIncomeTaxes... (effective rate, latest fiscal year)

TTM = latest fiscal year + current YTD - prior-year YTD (matched on period
dates); with no later 10-Q the fiscal year stands alone, and the basis says so.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import statistics
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import bundle as bundle_mod

Fetch = Callable[[str], bytes]

BENCHMARK = "SPY"
SEC_HOSTS = bundle_mod.SEC_HOSTS


MARKET_USER_AGENT = "Mozilla/5.0 (compatible; CoverageFlow/1.0)"
MARKET_MIN_INTERVAL = 1.0  # seconds between non-SEC requests


class MarketDataError(Exception):
    """A provider chain could not produce the requested data."""


# ------------------------------------------------------------------ plumbing

FETCH_CACHE_TTL = 6 * 3600.0          # seconds; a past cutoff's data does not change
FETCH_CACHE_MAX_BYTES = 256 * 1024 * 1024
_FETCH_CACHE: dict[str, tuple[float, bytes]] = {}
_FETCH_CACHE_LOCK = threading.Lock()


def clear_fetch_cache() -> None:
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()


def _cache_get(url: str, ttl: float) -> bytes | None:
    import time
    with _FETCH_CACHE_LOCK:
        hit = _FETCH_CACHE.get(url)
        if hit and time.monotonic() - hit[0] <= ttl:
            return hit[1]
        _FETCH_CACHE.pop(url, None)
    return None


def _cache_put(url: str, raw: bytes) -> None:
    import time
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE[url] = (time.monotonic(), raw)
        total = sum(len(v[1]) for v in _FETCH_CACHE.values())
        for k in list(_FETCH_CACHE):          # oldest first (insertion order)
            if total <= FETCH_CACHE_MAX_BYTES or len(_FETCH_CACHE) <= 1:
                break
            total -= len(_FETCH_CACHE.pop(k)[1])


def default_fetch(min_interval: float | None = None, *, cache: bool = True,
                  cache_ttl: float | None = None) -> Fetch:
    """Production fetch: throttled EDGAR client for SEC hosts, urllib otherwise.

    Responses are kept in a process-wide cache keyed by URL (default 6 hours),
    so a retry or another ticker reuses the ticker index, companyfacts and
    price downloads; the bytes (hence url and sha256 in provenance) are
    identical on a hit. ``cache=False`` bypasses it (tests, forced refresh).
    """
    ttl = FETCH_CACHE_TTL if cache_ttl is None else cache_ttl
    inner = _network_fetch(min_interval)
    if not cache:
        return inner

    def cached(url: str) -> bytes:
        raw = _cache_get(url, ttl)
        if raw is None:
            raw = inner(url)
            _cache_put(url, raw)
        return raw

    return cached


def _network_fetch(min_interval: float | None = None) -> Fetch:
    import urllib.request
    edgar = bundle_mod.EdgarFetcher(
        **({"min_interval": min_interval} if min_interval is not None else {}))
    import time
    last = {"t": 0.0}

    def fetch(url: str) -> bytes:
        host = url.split("/", 3)[2] if url.count("/") >= 2 else ""
        if host in SEC_HOSTS:
            return edgar.get(url)
        # Yahoo answers 429 to the default urllib agent; a generic
        # browser-style agent (no personal contact) is accepted. Calls are
        # spaced out and retried with backoff: rate limits and dropped TLS
        # connections are transient on these endpoints.
        req = urllib.request.Request(url, headers={"User-Agent": MARKET_USER_AGENT})
        error: Exception | None = None
        for attempt in range(4):
            wait = last["t"] + MARKET_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    last["t"] = time.monotonic()
                    return resp.read()
            except Exception as exc:
                last["t"] = time.monotonic()
                error = exc
                if getattr(exc, "code", None) in (400, 401, 403, 404):
                    break
                time.sleep(2.0 * (attempt + 1))
        raise MarketDataError(f"GET {url} failed: {error}") from error

    return fetch


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _d(text: str) -> date:
    return date.fromisoformat(str(text)[:10])


def _record(url: str, raw: bytes, source: str, **extra) -> dict:
    return {"source": source, "url": url, "sha256": _sha(raw),
            "fetched_at": _now(), **extra}


# ------------------------------------------------------------ price providers

def _stooq_symbol(ticker: str) -> str:
    return ticker.lower().replace(".", "-") + ".us"


def _stooq_url(ticker: str, d1: date, d2: date, interval: str) -> str:
    return (f"https://stooq.com/q/d/l/?s={_stooq_symbol(ticker)}"
            f"&d1={d1:%Y%m%d}&d2={d2:%Y%m%d}&i={interval}")


def _parse_stooq(raw: bytes) -> list[tuple[str, float]]:
    text = raw.decode("utf-8", "replace").strip()
    if not text or text.lower().startswith(("no data", "<", "exceeded")):
        raise MarketDataError(f"stooq returned no usable data: {text[:60]!r}")
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for r in rows:
        try:
            out.append((r["Date"][:10], float(r["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise MarketDataError("stooq CSV has no Date/Close rows")
    return sorted(out)


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _yahoo_url(ticker: str, d1: date, d2: date, interval: str) -> str:
    return (f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker.upper().replace('.', '-')}?period1={_epoch(d1)}"
            f"&period2={_epoch(d2 + timedelta(days=1))}&interval={interval}")


def _parse_yahoo(raw: bytes) -> list[tuple[str, float]]:
    try:
        obj = json.loads(raw.decode("utf-8"))
        res = obj["chart"]["result"][0]
        stamps = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        off = int((res.get("meta") or {}).get("gmtoffset") or 0)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise MarketDataError(f"yahoo response unusable: {exc}") from exc
    out = []
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(ts + off, tz=timezone.utc).date()
        out.append((day.isoformat(), float(c)))
    if not out:
        raise MarketDataError("yahoo chart has no closes")
    return sorted(out)


def _series(ticker: str, d1: date, d2: date, stooq_i: str, yahoo_i: str,
            fetch: Fetch, cutoff: str) -> dict:
    """First provider that yields at least one point on/before the cutoff."""
    failures: list[str] = []
    # Yahoo first: Stooq's CSV endpoint now answers scripted clients with a
    # JavaScript browser check, so it is kept only as a fallback.
    providers = (("yahoo", _yahoo_url(ticker, d1, d2, yahoo_i), _parse_yahoo),
                 ("stooq", _stooq_url(ticker, d1, d2, stooq_i), _parse_stooq))
    for name, url, parse in providers:
        try:
            raw = fetch(url)
            pts = [(dt, px) for dt, px in parse(raw) if dt <= cutoff and px > 0]
            if not pts:
                raise MarketDataError("no rows on or before the cutoff")
            return {**_record(url, raw, name), "points": pts, "failures": failures}
        except Exception as exc:  # provider chain: try the next one
            failures.append(f"{name}: {exc}")
    raise MarketDataError(f"{ticker}: all price providers failed ({'; '.join(failures)})")


def daily_close(ticker: str, cutoff: str, fetch: Fetch, lookback_days: int = 14) -> dict:
    """Last daily close on or before ``cutoff`` (never later).

    Returns {price, date, source, url, sha256, fetched_at, failures}.
    """
    end = _d(cutoff)
    s = _series(ticker, end - timedelta(days=lookback_days), end, "d", "1d", fetch, cutoff)
    dt, px = s["points"][-1]
    assert dt <= cutoff
    return {"price": px, "date": dt, "source": s["source"], "url": s["url"],
            "sha256": s["sha256"], "fetched_at": s["fetched_at"],
            "failures": s["failures"]}


def weekly_closes(ticker: str, cutoff: str, fetch: Fetch, years: int = 2) -> dict:
    """Weekly closes for the ``years`` before the cutoff, dates <= cutoff."""
    end = _d(cutoff)
    s = _series(ticker, end - timedelta(days=365 * years + 7), end, "w", "1wk",
                fetch, cutoff)
    s["series"] = s.pop("points")
    return s


# -------------------------------------------------------------------- beta

def _week_key(day: str) -> tuple[int, int]:
    iso = _d(day).isocalendar()
    return (iso[0], iso[1])


def regress_beta(stock: list[tuple[str, float]], bench: list[tuple[str, float]],
                 min_obs: int = 26) -> dict:
    """OLS slope of stock weekly returns on benchmark weekly returns.

    Closes are aligned by ISO week (providers label weekly bars differently).
    Returns {beta, n_obs, r_squared}; raises MarketDataError if too few points.
    """
    s = {_week_key(d): p for d, p in stock}
    b = {_week_key(d): p for d, p in bench}
    keys = sorted(set(s) & set(b))
    rs, rb = [], []
    for k0, k1 in zip(keys, keys[1:]):
        rs.append(s[k1] / s[k0] - 1.0)
        rb.append(b[k1] / b[k0] - 1.0)
    if len(rs) < min_obs:
        raise MarketDataError(f"only {len(rs)} aligned weekly returns (need {min_obs})")
    mb, ms = statistics.fmean(rb), statistics.fmean(rs)
    var = sum((x - mb) ** 2 for x in rb)
    if var == 0:
        raise MarketDataError("benchmark returns have zero variance")
    cov = sum((x - mb) * (y - ms) for x, y in zip(rb, rs))
    beta = cov / var
    ss_tot = sum((y - ms) ** 2 for y in rs)
    r2 = (cov * cov / (var * ss_tot)) if ss_tot else 0.0
    return {"beta": beta, "n_obs": len(rs), "r_squared": r2}


# -------------------------------------------------------------- risk-free

def treasury_url(year: int) -> str:
    return ("https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/{year}/all"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}"
            "&page&_format=csv")


def fred_url(cutoff: str) -> str:
    end = _d(cutoff)
    return ("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
            f"&cosd={(end - timedelta(days=30)).isoformat()}&coed={end.isoformat()}")


def _treasury_10y(cutoff: str, fetch: Fetch) -> dict:
    """US Treasury daily par yield curve, 10-year column, last day <= cutoff.

    The CSV is published per calendar year, so an early-January cutoff falls
    back to the previous year's file.
    """
    end = _d(cutoff)
    for year in (end.year, end.year - 1):
        url = treasury_url(year)
        raw = fetch(url)
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace"))))
        best = None
        for r in rows:
            try:
                m, d, y = (int(x) for x in str(r.get("Date", "")).split("/"))
                day = date(y, m, d).isoformat()
                val = float(r.get("10 Yr") or "")
            except (ValueError, TypeError):
                continue
            if day <= cutoff and (best is None or day > best[0]):
                best = (day, val)
        if best is not None:
            return {"rate": best[1] / 100.0, "date": best[0],
                    **_record(url, raw, "treasury:par_yield_10y")}
    raise MarketDataError("Treasury 10Y: no observation on or before the cutoff")


def _fred_dgs10(cutoff: str, fetch: Fetch) -> dict:
    url = fred_url(cutoff)
    raw = fetch(url)
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8", "replace"))))
    best = None
    for r in rows[1:]:
        if len(r) < 2:
            continue
        try:
            day, val = r[0][:10], float(r[1])
            _d(day)
        except ValueError:
            continue  # "." = no observation, or a header artefact
        if day <= cutoff and (best is None or day > best[0]):
            best = (day, val)
    if best is None:
        raise MarketDataError("FRED DGS10: no observation on or before the cutoff")
    return {"rate": best[1] / 100.0, "date": best[0],
            **_record(url, raw, "fred:DGS10")}


def risk_free(cutoff: str, fetch: Fetch) -> dict:
    """10-year Treasury yield, last value on or before the cutoff.

    Primary: the US Treasury's daily par yield curve (official source, no
    key). Fallback: FRED DGS10. Returned ``rate`` is a fraction
    (4.25 percent -> 0.0425).
    """
    failures: list[str] = []
    for name, fn in (("treasury", _treasury_10y), ("fred", _fred_dgs10)):
        try:
            out = fn(cutoff, fetch)
            out["failures"] = failures
            return out
        except Exception as exc:
            failures.append(f"{name}: {exc}")
    raise MarketDataError(f"risk-free: all providers failed ({'; '.join(failures)})")


# ------------------------------------------------------ SEC companyfacts

REVENUE_TAGS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet")
OPINC_TAGS = ("OperatingIncomeLoss",)
DA_TAGS = ("DepreciationDepletionAndAmortization",
           "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization",
           "DepreciationAmortizationAndOther")
NI_TAGS = ("NetIncomeLoss", "ProfitLoss")
SHARES_TAGS = ("WeightedAverageNumberOfDilutedSharesOutstanding",)
GROSS_TAGS = ("GrossProfit",)
TAX_TAGS = ("IncomeTaxExpenseBenefit",)
PRETAX_TAGS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic")


def companyfacts_url(cik: str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"


def _entries(facts: dict, tag: str, unit: str = "USD", ns: str = "us-gaap") -> list[dict]:
    return list((((facts.get(ns) or {}).get(tag) or {}).get("units") or {}).get(unit) or [])


def _visible(entries: list[dict], cutoff: str) -> list[dict]:
    """Facts filed on or before the cutoff; latest filing wins per (start, end)."""
    best: dict[tuple, dict] = {}
    for e in entries:
        if not e.get("end") or not e.get("filed") or str(e["filed"])[:10] > cutoff:
            continue
        k = (e.get("start"), e["end"])
        if k not in best or e["filed"] > best[k]["filed"]:
            best[k] = e
    return list(best.values())


def _days(e: dict) -> int | None:
    if not e.get("start"):
        return None
    return (_d(e["end"]) - _d(e["start"])).days


def _near(a: str, b: date, tol: int) -> bool:
    return abs((_d(a) - b).days) <= tol


def ttm_from_entries(entries: list[dict], cutoff: str) -> dict | None:
    """Trailing twelve months from duration facts filed <= cutoff.

    TTM = latest fiscal year + current YTD - prior-year YTD. Returns
    {value, as_of, filed, basis} or None when no fiscal year is visible.
    """
    vis = _visible(entries, cutoff)
    annual = [e for e in vis if (_days(e) or 0) >= 350 and (_days(e) or 0) <= 380]
    if not annual:
        return None
    fy = max(annual, key=lambda e: e["end"])
    fy_end = _d(fy["end"])
    ytd = [e for e in vis if e.get("start") and _d(e["end"]) > fy_end
           and _near(e["start"], fy_end + timedelta(days=1), 7)
           and (_days(e) or 0) < 350]
    base = {"as_of": fy["end"], "filed": fy["filed"]}
    if not ytd:
        return {**base, "value": fy["val"],
                "basis": f"fiscal year ended {fy['end']} (no later 10-Q filed on or before cutoff)"}
    cur = max(ytd, key=lambda e: (e["end"], _days(e) or 0))
    cs, ce = _d(cur["start"]), _d(cur["end"])
    prior = [e for e in vis if e.get("start")
             and _near(e["start"], cs - timedelta(days=364), 10)
             and _near(e["end"], ce - timedelta(days=364), 10)]
    if not prior:
        return {**base, "value": fy["val"],
                "basis": f"fiscal year ended {fy['end']} (prior-year YTD comparative missing, TTM not formed)"}
    pr = max(prior, key=lambda e: e["filed"])
    return {"value": fy["val"] + cur["val"] - pr["val"], "as_of": cur["end"],
            "filed": max(cur["filed"], fy["filed"]),
            "basis": (f"TTM to {cur['end']} = FY{fy['end']} {fy['val']:.6g} + YTD "
                      f"{cur['start']}..{cur['end']} {cur['val']:.6g} - prior YTD {pr['val']:.6g}")}


def latest_instant(entries: list[dict], cutoff: str) -> dict | None:
    vis = [e for e in _visible(entries, cutoff)]
    if not vis:
        return None
    e = max(vis, key=lambda e: (e["end"], e["filed"]))
    return {"value": e["val"], "as_of": e["end"], "filed": e["filed"]}


def latest_period(entries: list[dict], cutoff: str) -> dict | None:
    """Latest reported value of a duration fact (shortest span at the latest end)."""
    vis = _visible(entries, cutoff)
    if not vis:
        return None
    e = max(vis, key=lambda e: (e["end"], -(_days(e) or 10_000)))
    return {"value": e["val"], "as_of": e["end"], "filed": e["filed"]}


def annual_series(entries: list[dict], cutoff: str) -> list[tuple[str, float]]:
    vis = _visible(entries, cutoff)
    return sorted((e["end"], e["val"]) for e in vis
                  if 350 <= (_days(e) or 0) <= 380)


def _pick(facts: dict, tags, resolver, cutoff: str, unit: str = "USD", ns: str = "us-gaap"):
    """Resolve every tag; keep the one with the latest period end (ties: list order)."""
    best = None
    for order, tag in enumerate(tags):
        res = resolver(_entries(facts, tag, unit, ns), cutoff)
        if res is None:
            continue
        key = (res["as_of"], -order)
        if best is None or key > best[0]:
            best = (key, {**res, "tag": f"{ns}:{tag}"})
    return best[1] if best else None


def _sum_at(parts: list[dict]) -> dict:
    return {"value": sum(p["value"] for p in parts),
            "as_of": max(p["as_of"] for p in parts),
            "filed": max(p["filed"] for p in parts),
            "tag": " + ".join(p["tag"] for p in parts)}


def _total_debt(facts: dict, cutoff: str) -> dict | None:
    inst = latest_instant
    comb = _pick(facts, ("DebtLongtermAndShorttermCombinedAmount",), inst, cutoff)
    if comb:
        return comb
    lt = _pick(facts, ("LongTermDebt",), inst, cutoff)
    stb = _pick(facts, ("ShortTermBorrowings",), inst, cutoff)
    if lt:
        return _sum_at([lt] + ([stb] if stb and stb["as_of"] == lt["as_of"] else []))
    parts = [p for p in (
        _pick(facts, ("LongTermDebtNoncurrent",), inst, cutoff),
        _pick(facts, ("DebtCurrent", "LongTermDebtCurrent"), inst, cutoff), stb) if p]
    return _sum_at(parts) if parts else None


def _cash(facts: dict, cutoff: str) -> dict | None:
    inst = latest_instant
    one = _pick(facts, ("CashCashEquivalentsAndShortTermInvestments",), inst, cutoff)
    if one:
        return one
    cash = _pick(facts, ("CashAndCashEquivalentsAtCarryingValue",), inst, cutoff)
    sti = _pick(facts, ("ShortTermInvestments", "MarketableSecuritiesCurrent",
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent"), inst, cutoff)
    parts = [p for p in (cash, sti if sti and cash and sti["as_of"] == cash["as_of"] else None)
             if p]
    return _sum_at(parts) if parts else cash


def fundamentals(cik: str, cutoff: str, fetch: Fetch) -> dict:
    """Fundamentals from SEC companyfacts using only facts filed <= cutoff.

    Returns {cik, entity, url, sha256, fetched_at, metrics, history, missing}.
    ``metrics`` maps name -> {value, tag, as_of, filed, basis}; names: revenue,
    operating_income, da, net_income, diluted_shares, total_debt, cash.
    """
    url = companyfacts_url(cik)
    raw = fetch(url)
    try:
        doc = json.loads(raw.decode("utf-8"))
        facts = doc["facts"]
    except (ValueError, KeyError, TypeError) as exc:
        raise MarketDataError(f"companyfacts for CIK {cik} unusable: {exc}") from exc
    m: dict[str, dict | None] = {}
    for name, tags in (("revenue", REVENUE_TAGS), ("operating_income", OPINC_TAGS),
                       ("da", DA_TAGS), ("net_income", NI_TAGS)):
        m[name] = _pick(facts, tags, ttm_from_entries, cutoff)
    sh = _pick(facts, SHARES_TAGS, latest_period, cutoff, unit="shares")
    if sh:
        sh["basis"] = f"weighted-average diluted shares, period ended {sh['as_of']}"
    else:
        sh = _pick(facts, ("EntityCommonStockSharesOutstanding",), latest_instant,
                   cutoff, unit="shares", ns="dei")
        if sh:
            sh["basis"] = "basic shares on cover page (diluted not available)"
    m["diluted_shares"] = sh
    for name, fn, what in (("total_debt", _total_debt, "debt at"), ("cash", _cash, "cash and short-term investments at")):
        m[name] = fn(facts, cutoff)
        if m[name]:
            m[name]["basis"] = f"{what} {m[name]['as_of']}"
    # history for bounds / growth / tax
    rev_best = _pick(facts, REVENUE_TAGS, ttm_from_entries, cutoff)
    rev_tag = rev_best["tag"].split(":", 1)[1] if rev_best else None
    rev_ann = annual_series(_entries(facts, rev_tag), cutoff) if rev_tag else []
    gp_ann = dict(annual_series(_entries(facts, "GrossProfit"), cutoff))
    gm = [(d, gp_ann[d] / v) for d, v in rev_ann if d in gp_ann and v]
    growth = [(b[0], b[1] / a[1] - 1.0) for a, b in zip(rev_ann, rev_ann[1:]) if a[1]]
    tax = dict(annual_series(_entries(facts, "IncomeTaxExpenseBenefit"), cutoff))
    pre = {}
    for t in PRETAX_TAGS:
        pre.update(dict(annual_series(_entries(facts, t), cutoff)))
    etr = None
    if tax and pre:
        d = max(set(tax) & set(pre), default=None)
        if d and pre[d]:
            etr = {"value": tax[d] / pre[d], "as_of": d,
                   "basis": f"income tax {tax[d]:.6g} / pre-tax income {pre[d]:.6g}, fiscal year ended {d}"}
    return {"cik": f"{int(cik):010d}", "entity": doc.get("entityName"),
            **_record(url, raw, "sec:companyfacts"),
            "metrics": {k: v for k, v in m.items() if v},
            "missing": [k for k, v in m.items() if not v],
            "history": {"gross_margin": gm, "revenue_growth": growth,
                        "effective_tax_rate": etr}}
