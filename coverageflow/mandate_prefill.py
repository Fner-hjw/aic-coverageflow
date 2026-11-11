"""Deterministic mandate proposal builder (human reviews, edits, accepts).

``propose_mandate(ticker, cutoff, fetch=..., peers=None)`` returns a complete
mandate in the existing shape plus:

* ``status: "proposed"``: nothing may run on it until ``accept_mandate``;
* ``provenance``: {dotted json-path: {value, source, url, as_of, sha256, basis}}
  (derived values also carry ``inputs``); list indices are plain numbers, e.g.
  ``assumptions.peer_block.peers.0.ev_ebitda``;
* ``missing``: json-paths that could not be fetched (left null, basis
  "not available — enter manually") and ``warnings``.

Market data is fetched by code (coverageflow.market_data, one injectable
``fetch``); the mandate itself stays human-owned. Same inputs and same
recorded responses give the same proposal (apart from ``fetched_at``).

Peer suggestion (when neither --peers nor references/peer-sets.json names
peers): same SIC code per EDGAR. Limits: EDGAR's company browser lists
companies by CIK, not by size or business similarity, so the suggestion
keeps the first few SIC-mates that appear in the SEC ticker index (which is
roughly ordered by market capitalisation) and excludes the company itself.
SIC codes are coarse (one code covers very different chip businesses); treat
the suggestion as a starting point, or curate references/peer-sets.json.
"""

from __future__ import annotations

import copy
import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import bundle as bundle_mod
from . import market_data as md
from . import mandate_check

REFERENCES = Path(__file__).resolve().parent.parent / "references"
NOT_AVAILABLE = "not available — enter manually"

DEFAULT_ERP = 0.05
DEFAULT_DEBT_SPREAD = 0.015
DEFAULT_TAX = 0.21
DEFAULT_TERMINAL_GROWTH = 0.025
BETA_RANGE = (0.3, 2.5)
TAX_SANE = (0.05, 0.35)
MARGIN_WIDEN = 0.05
MAX_PEERS = 5

# Fallback segment growth bounds (the repo's existing defaults); companyfacts
# carries no segment-dimensioned history, so these are used and labelled as such.
KNOWN_SEGMENT_BOUNDS = {
    "INTC": {"CCG": [-0.10, 0.12], "DCAI": [-0.05, 0.25], "NEX": [-0.05, 0.20],
             "Foundry": [-0.05, 0.30], "Altera": [-0.10, 0.15],
             "Mobileye": [-0.05, 0.20]},
    "AMD": {"Data Center": [-0.05, 0.35], "Client": [-0.10, 0.20],
            "Embedded": [-0.20, 0.05], "Gaming": [-0.15, 0.10]},
}

Fetch = Callable[[str], bytes]


class PrefillError(Exception):
    """Prefill could not proceed at all (e.g. unknown ticker)."""


class _Cached:
    """Memoise fetch(url) for one proposal (the SEC ticker index is large)."""

    def __init__(self, fetch: Fetch):
        self._fetch, self._memo = fetch, {}

    def __call__(self, url: str) -> bytes:
        if url not in self._memo:
            self._memo[url] = self._fetch(url)
        return self._memo[url]


# ------------------------------------------------------------- small helpers

def _round(x: float, nd: int) -> float:
    return round(float(x), nd)


def _entry(value, source, basis, url=None, as_of=None, sha256=None, inputs=None) -> dict:
    e = {"value": value, "source": source, "url": url, "as_of": as_of,
         "sha256": sha256, "basis": basis}
    if inputs:
        e["inputs"] = inputs
    return e


def _src(rec: dict, **kw) -> dict:
    """Compact source block of a provider record, for the ``inputs`` map."""
    return {"source": rec.get("source"), "url": rec.get("url"),
            "sha256": rec.get("sha256"), "fetched_at": rec.get("fetched_at"), **kw}


def load_peer_sets(references_dir: Path | None = None) -> dict[str, list[str]]:
    path = (references_dir or REFERENCES) / "peer-sets.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {k.upper(): [str(t).upper() for t in v] for k, v in data.items()
            if not k.startswith("_") and isinstance(v, list)}


def _set_path(obj: dict, path: str, value: Any) -> None:
    node: Any = obj
    parts = path.split(".")
    for p in parts[:-1]:
        node = node[int(p)] if isinstance(node, list) else node.setdefault(p, {})
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def _get_path(obj: Any, path: str, default=None):
    node = obj
    for p in path.split("."):
        try:
            node = node[int(p)] if isinstance(node, list) else node[p]
        except (KeyError, IndexError, ValueError, TypeError):
            return default
    return node


# ------------------------------------------------------------------- peers

def suggest_peers_by_sic(cik: str, ticker: str, fetch: Fetch, n: int = MAX_PEERS) -> dict:
    """Same-SIC candidates from EDGAR; see the module docstring for limits."""
    sub_url = bundle_mod.submissions_url(cik)
    sub_raw = fetch(sub_url)
    sic = str(json.loads(sub_raw.decode("utf-8")).get("sic") or "")
    if not sic:
        raise PrefillError("EDGAR submissions carry no SIC code")
    list_url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&SIC={sic}&owner=include&match=&count=100&hidefilings=0")
    raw = fetch(list_url)
    ciks = []
    for m in re.finditer(r"CIK=(\d{10})", raw.decode("utf-8", "replace")):
        if m.group(1) not in ciks:
            ciks.append(m.group(1))
    idx = json.loads(fetch("https://www.sec.gov/files/company_tickers.json").decode("utf-8"))
    by_cik = {}
    for rank, e in enumerate(idx.values()):
        by_cik.setdefault(f"{int(e['cik_str']):010d}", (rank, str(e["ticker"]).upper()))
    own = f"{int(cik):010d}"
    ranked = sorted((by_cik[c] for c in ciks if c in by_cik and c != own))
    tickers = [t for _, t in ranked if t != ticker.upper()][:n]
    return {"tickers": tickers, "sic": sic, "url": list_url,
            "sha256": md._sha(raw)}


def _peer_row(tk: str, cutoff: str, fetch: Fetch) -> tuple[dict, dict]:
    """(row, provenance-by-field) for one peer, or raise PrefillError."""
    cik = bundle_mod.resolve_ticker(tk, fetch)
    if not cik:
        raise PrefillError("no CIK in the SEC ticker index")
    px = md.daily_close(tk, cutoff, fetch)
    f = md.fundamentals(cik, cutoff, fetch)
    m = f["metrics"]
    mult = multiples(px["price"], m)
    if all(mult[k] is None for k in ("ev_ebitda", "p_e_ttm", "p_s")):
        raise PrefillError("no multiple computable from filed data ("
                           + ", ".join(f["missing"] or ["non-positive earnings"]) + ")")
    growth = (f["history"]["revenue_growth"] or [(None, None)])[-1]
    row = {"ticker": tk, **{k: (None if v is None else _round(v, 2)) for k, v in mult.items()},
           "rev_growth_yoy": None if growth[1] is None else _round(growth[1], 4),
           "notional_note": f"computed {px['date']} close with SEC companyfacts filed <= {cutoff}"}
    inputs = {"price": _src(px, value=px["price"], as_of=px["date"]),
              "companyfacts": _src(f, as_of=max((v["as_of"] for v in m.values()), default=None))}
    for k, v in m.items():
        inputs[k] = {"value": v["value"], "tag": v["tag"], "as_of": v["as_of"],
                     "filed": v["filed"], "basis": v["basis"]}
    prov = {}
    basis = {"ev_ebitda": "(price x diluted shares + debt - cash) / (TTM operating income + TTM D&A)",
             "p_e_ttm": "price / (TTM net income attributable / diluted shares)",
             "p_s": "price x diluted shares / TTM revenue",
             "rev_growth_yoy": "latest fiscal year revenue / prior fiscal year - 1"}
    for k in ("ev_ebitda", "p_e_ttm", "p_s", "rev_growth_yoy"):
        if row[k] is None:
            prov[k] = _entry(None, "none", f"{NOT_AVAILABLE} (inputs missing or earnings not positive)")
        else:
            prov[k] = _entry(row[k], "derived", basis[k], url=f["url"], as_of=px["date"],
                             sha256=f["sha256"], inputs=inputs)
    return row, prov


def multiples(price: float, m: dict) -> dict:
    """ev_ebitda, p_e_ttm, p_s from a price and fundamentals metrics (None if not computable)."""
    sh = (m.get("diluted_shares") or {}).get("value")
    out = {"ev_ebitda": None, "p_e_ttm": None, "p_s": None}
    if not sh or sh <= 0:
        return out
    cap = price * sh
    oi, da = m.get("operating_income"), m.get("da")
    debt, cash = m.get("total_debt"), m.get("cash")
    if oi and da and debt and cash:
        ebitda = oi["value"] + da["value"]
        if ebitda > 0:
            out["ev_ebitda"] = (cap + debt["value"] - cash["value"]) / ebitda
    ni = m.get("net_income")
    if ni and ni["value"] > 0:
        out["p_e_ttm"] = price / (ni["value"] / sh)
    rev = m.get("revenue")
    if rev and rev["value"] > 0:
        out["p_s"] = cap / rev["value"]
    return out


# --------------------------------------------------------------- the builder

def propose_mandate(ticker: str, cutoff: str, fetch: Fetch | None = None,
                    peers: list[str] | None = None, *, erp: float = DEFAULT_ERP,
                    debt_spread: float = DEFAULT_DEBT_SPREAD, period: str = "fy2026q3",
                    references_dir: Path | None = None) -> dict:
    """Build a proposed mandate for ``ticker`` as of ``cutoff`` (YYYY-MM-DD)."""
    from . import cli
    from .orchestrator import new_case_id

    ticker = ticker.strip().upper()
    fetch = _Cached(fetch or md.default_fetch())
    case_id = new_case_id(ticker, period, cutoff)
    m = cli.default_mandate(case_id, ticker, cutoff)
    a = m["assumptions"]
    prov: dict[str, dict] = {}
    missing: list[str] = []
    warnings: list[str] = []

    def miss(path: str, why: str = "") -> None:
        missing.append(path)
        prov[path] = _entry(None, "none", NOT_AVAILABLE + (f" ({why})" if why else ""))

    cik = None
    try:
        cik = bundle_mod.resolve_ticker(ticker, fetch)
    except Exception as exc:
        warnings.append(f"CIK lookup failed: {exc}")
    if not cik:
        raise PrefillError(f"{ticker}: not found in the SEC ticker index (check the symbol)")
    m["cik"] = cik
    prov["cik"] = _entry(cik, "sec:company_tickers", "exact ticker match in the SEC ticker index",
                         url="https://www.sec.gov/files/company_tickers.json", as_of=cutoff)
    prov["assumptions.as_of"] = _entry(cutoff, "mandate", "the mandate cutoff date")

    # ---- price ----------------------------------------------------------
    price = None
    try:
        price = md.daily_close(ticker, cutoff, fetch)
        a["market"] = {"as_of": price["date"], "price": _round(price["price"], 4),
                       "note": f"closing price from {price['source']}; as-of <= cutoff"}
        prov["assumptions.market.price"] = _entry(
            _round(price["price"], 4), price["source"],
            f"last daily close on or before {cutoff}", url=price["url"],
            as_of=price["date"], sha256=price["sha256"])
        prov["assumptions.market.as_of"] = _entry(price["date"], price["source"],
                                                  "date of that close (never after the cutoff)",
                                                  url=price["url"], as_of=price["date"],
                                                  sha256=price["sha256"])
        for f in price["failures"]:
            warnings.append(f"price provider fallback: {f}")
    except Exception as exc:
        a["market"]["price"] = None
        miss("assumptions.market.price", str(exc))
        warnings.append(f"{ticker} price: {exc}")

    # ---- own fundamentals ------------------------------------------------
    own = None
    try:
        own = md.fundamentals(cik, cutoff, fetch)
    except Exception as exc:
        warnings.append(f"{ticker} fundamentals: {exc}")

    # ---- peers -------------------------------------------------------------
    sets = load_peer_sets(references_dir)
    peer_src: tuple[str, str, dict] | None = None
    if peers:
        names = [p.strip().upper() for p in peers if p.strip()]
        peer_src = ("user", "peers given on the command line / form", {})
    elif ticker in sets:
        names = sets[ticker]
        peer_src = ("references/peer-sets.json", f"curated peer list for {ticker}", {})
    else:
        try:
            s = suggest_peers_by_sic(cik, ticker, fetch)
            names = s["tickers"]
            peer_src = (f"sec:sic-{s['sic']}",
                        f"same-SIC ({s['sic']}) companies in EDGAR, first {len(names)} by "
                        "SEC ticker-index order; coarse, review before use",
                        {"url": s["url"], "sha256": s["sha256"]})
        except Exception as exc:
            names = []
            warnings.append(f"peer suggestion by SIC failed: {exc}")
    rows: list[dict] = []
    row_prov: list[dict] = []
    used: list[str] = []
    for tk in names:
        if tk == ticker:
            continue
        try:
            row, pv = _peer_row(tk, cutoff, fetch)
        except Exception as exc:
            warnings.append(f"peer {tk} skipped: {exc}")
            continue
        flags = [k for k in ("ev_ebitda", "p_e_ttm", "p_s") if row[k] is None]
        if flags:
            warnings.append(f"peer {tk}: incomplete, no {', '.join(flags)}")
        rows.append(row)
        row_prov.append(pv)
        used.append(tk)
    a["peer_block"]["peers"] = rows
    a["peer_block"]["note"] = "peer multiples computed from market data; human-reviewed"
    if rows:
        dates = [pv["ev_ebitda"]["as_of"] or pv["p_s"]["as_of"] or pv["p_e_ttm"]["as_of"]
                 for pv in row_prov]
        a["peer_block"]["as_of"] = max(d for d in dates if d) if any(dates) else cutoff
    for i, (row, pv) in enumerate(zip(rows, row_prov)):
        prov[f"assumptions.peer_block.peers.{i}.ticker"] = _entry(
            row["ticker"], peer_src[0], peer_src[1], **peer_src[2])
        for k, e in pv.items():
            prov[f"assumptions.peer_block.peers.{i}.{k}"] = e
    if used and peer_src:
        prov["assumptions.peer_block.peers"] = _entry(
            used, peer_src[0], peer_src[1], as_of=cutoff, **peer_src[2])
    else:
        miss("assumptions.peer_block.peers", "no usable peers")

    # ---- exit multiple, own band -----------------------------------------
    evs = [r["ev_ebitda"] for r in rows if r["ev_ebitda"] is not None]
    if evs:
        a["exit_multiple_ebitda"] = _round(statistics.median(evs), 1)
        prov["assumptions.exit_multiple_ebitda"] = _entry(
            a["exit_multiple_ebitda"], "derived",
            f"median peer EV/EBITDA ({len(evs)} peer(s): {', '.join(map(str, evs))})",
            as_of=cutoff)
    else:
        a["exit_multiple_ebitda"] = None
        miss("assumptions.exit_multiple_ebitda", "no peer EV/EBITDA")
    band = {"as_of": cutoff,
            "note": "peer min/max at the cutoff (own history not fetched); review"}
    for key, col in (("ev_ebitda", "ev_ebitda"), ("p_e", "p_e_ttm"), ("p_s", "p_s")):
        vals = [r[col] for r in rows if r[col] is not None]
        path = f"assumptions.own_band.{key}"
        if len(vals) >= 2:
            band[key] = [min(vals), max(vals)]
            prov[path] = _entry(band[key], "derived",
                                f"min and max of {len(vals)} peer {col} values", as_of=cutoff)
        else:
            miss(path, "needs at least 2 peer values")
    a["own_band"] = band

    # ---- tax, margins, bounds ---------------------------------------------
    a["tax_rate"] = DEFAULT_TAX
    prov["assumptions.tax_rate"] = _entry(DEFAULT_TAX, "default", "statutory US 21% default; "
                                          "effective rate not computable from filed data")
    hist = (own or {}).get("history") or {}
    etr = hist.get("effective_tax_rate")
    if etr and TAX_SANE[0] <= etr["value"] <= TAX_SANE[1]:
        a["tax_rate"] = _round(etr["value"], 3)
        prov["assumptions.tax_rate"] = _entry(
            a["tax_rate"], "derived", etr["basis"], url=own["url"], as_of=etr["as_of"],
            sha256=own["sha256"])
    elif etr:
        prov["assumptions.tax_rate"]["basis"] = (
            f"statutory 21% default; effective rate {etr['value']:.3f} ({etr['basis']}) "
            f"outside the sane range {TAX_SANE}")
    a["terminal_growth"] = DEFAULT_TERMINAL_GROWTH
    prov["assumptions.terminal_growth"] = _entry(
        DEFAULT_TERMINAL_GROWTH, "default", "long-run policy default (2.5%)")

    fb = a["forecast_bounds"]
    gm = hist.get("gross_margin") or []
    if len(gm) >= 2:
        vals = [g for _, g in gm]
        lo = max(0.0, _round(min(vals) - MARGIN_WIDEN, 2))
        hi = min(1.0, _round(max(vals) + MARGIN_WIDEN, 2))
        fb["margin_floor_gross"], fb["margin_ceiling_gross"] = lo, hi
        basis = (f"fiscal-year gross margin range {min(vals):.3f}..{max(vals):.3f} "
                 f"({gm[0][0]}..{gm[-1][0]}) widened by {MARGIN_WIDEN:.2f}")
        for k, v in (("margin_floor_gross", lo), ("margin_ceiling_gross", hi)):
            prov[f"assumptions.forecast_bounds.{k}"] = _entry(
                v, "derived", basis, url=own["url"], as_of=gm[-1][0], sha256=own["sha256"])
    else:
        for k in ("margin_floor_gross", "margin_ceiling_gross"):
            prov[f"assumptions.forecast_bounds.{k}"] = _entry(
                fb[k], "default", "existing default; gross margin history not available "
                "from companyfacts (review)")
    segs = KNOWN_SEGMENT_BOUNDS.get(ticker)
    growth = hist.get("revenue_growth") or []
    ctx = (f"; consolidated FY revenue growth was {min(g for _, g in growth):.3f}.."
           f"{max(g for _, g in growth):.3f} (context only)") if growth else ""
    if segs:
        fb["segments"] = copy.deepcopy(segs)
        for name, b in segs.items():
            prov[f"assumptions.forecast_bounds.segments.{name}"] = _entry(
                b, "default", f"existing default bounds for {ticker}; segment history is not "
                f"in SEC companyfacts (no dimensioned facts){ctx}")
    else:
        fb["segments"] = {}
        miss("assumptions.forecast_bounds.segments",
             "no default segment set for this ticker; segment history is not in companyfacts")
    for k in ("dcf_comps_tolerance", "terminal_tolerance", "nwc_pct_incremental"):
        prov[f"assumptions.{k}"] = _entry(a[k], "default", "existing default, unchanged")
    prov["assumptions.rating_bands"] = _entry(a["rating_bands"], "default",
                                              "existing default bands (buy +15%, sell -10%)")

    # ---- WACC ---------------------------------------------------------------
    a["wacc"] = None
    wacc_why = []
    rf = beta_info = None
    try:
        rf = md.risk_free(cutoff, fetch)
    except Exception as exc:
        wacc_why.append(f"risk-free: {exc}")
    try:
        bench = md.weekly_closes(md.BENCHMARK, cutoff, fetch)
        stock = md.weekly_closes(ticker, cutoff, fetch)
        beta_info = md.regress_beta(stock["series"], bench["series"])
        beta_info.update(stock=stock, bench=bench)
    except Exception as exc:
        wacc_why.append(f"beta: {exc}")
    mt = (own or {}).get("metrics") or {}
    sh, debt = mt.get("diluted_shares"), mt.get("total_debt")
    if not (price and sh and debt):
        wacc_why.append("capital structure: need price, diluted shares and total debt")
    if not wacc_why:
        raw_beta = beta_info["beta"]
        beta = min(max(raw_beta, BETA_RANGE[0]), BETA_RANGE[1])
        clamped = beta != raw_beta
        ke = rf["rate"] + beta * erp
        kd_pre = rf["rate"] + debt_spread
        tax = a["tax_rate"]
        e_val = price["price"] * sh["value"]
        d_val = debt["value"]
        we, wd = e_val / (e_val + d_val), d_val / (e_val + d_val)
        wacc = we * ke + wd * kd_pre * (1 - tax)
        a["wacc"] = _round(wacc, 4)
        inputs = {
            "risk_free": _src(rf, value=rf["rate"], as_of=rf["date"]),
            "beta": {**_src(beta_info["stock"]), "raw": _round(raw_beta, 4),
                     "used": _round(beta, 4), "clamped": clamped,
                     "clamp_range": list(BETA_RANGE), "n_weekly_returns": beta_info["n_obs"],
                     "r_squared": _round(beta_info["r_squared"], 4),
                     "benchmark": md.BENCHMARK, "benchmark_source": _src(beta_info["bench"])},
            "equity_risk_premium": {"value": erp, "source": "configurable default"},
            "debt_spread": {"value": debt_spread, "source": "configurable default"},
            "tax_rate": {"value": tax},
            "cost_of_equity": _round(ke, 5), "pre_tax_cost_of_debt": _round(kd_pre, 5),
            "market_cap": {"value": _round(e_val, 2), "price": price["price"],
                           "diluted_shares": sh["value"]},
            "total_debt": {"value": d_val, "tag": debt["tag"], "as_of": debt["as_of"]},
            "weights": {"equity": _round(we, 4), "debt": _round(wd, 4)},
        }
        prov["assumptions.wacc"] = _entry(
            a["wacc"], "derived",
            f"CAPM: Ke = rf {rf['rate']:.4f} + beta {beta:.2f} x ERP {erp:.3f}; "
            f"Kd = rf + {debt_spread:.3f} spread, after {tax:.0%} tax; weights by market cap "
            "and total debt" + ("; beta clamped to " + str(BETA_RANGE) if clamped else ""),
            url=rf["url"], as_of=cutoff, inputs=inputs)
        if clamped:
            warnings.append(f"beta {raw_beta:.2f} clamped to {beta:.2f}")
    else:
        miss("assumptions.wacc", "; ".join(wacc_why))
        warnings.extend(f"wacc: {w}" for w in wacc_why)

    # sensitivity grid follows the proposed wacc/terminal growth
    if a["wacc"] is not None:
        a["sensitivity"] = cli._mandate_assumption_extras(
            a["wacc"], a["terminal_growth"], cutoff)["sensitivity"]
        prov["assumptions.sensitivity"] = _entry(
            a["sensitivity"], "derived", "grid centred on the proposed WACC and terminal growth "
            "(+/- 2 and 4 points WACC, +/- 1 and 2 points growth)", as_of=cutoff)

    m["status"] = "proposed"
    m["proposed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m["missing"] = sorted(set(missing))
    m["warnings"] = warnings
    m["provenance"] = prov
    return m


# ----------------------------------------------------- review / edit / accept

_EDIT_ROOTS = ("report_policy", "assumptions", "limits")


def _flatten(obj: Any, prefix: str, out: dict) -> None:
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = obj
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list) and any(isinstance(x, (dict, list)) for x in obj):
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}.{i}", out)
    else:
        out[prefix] = obj


def _leaves(m: dict) -> dict:
    out: dict = {}
    for root in _EDIT_ROOTS:
        if root in m:
            _flatten(m[root], root, out)
    return out


def _is_derived_note(path: str) -> bool:
    return path.endswith((".note", ".notional_note"))


def edits_between(proposed: dict, final: dict) -> list[dict]:
    """[{path, proposed, final}] for every reviewed leaf whose value changed."""
    p, f = _leaves(proposed), _leaves(final)
    edits = []
    for path in sorted(set(p) | set(f)):
        if _is_derived_note(path):
            continue
        if p.get(path) != f.get(path):
            edits.append({"path": path, "proposed": p.get(path), "final": f.get(path)})
    return edits


FORM_CONTROLLED = ("wacc", "terminal_growth", "exit_multiple_ebitda", "tax_rate",
                   "forecast_bounds", "peer_block", "market", "own_band", "sensitivity",
                   "dcf_comps_tolerance")


def overlay_form(proposed: dict, built: dict) -> dict:
    """The proposal with the fields a reviewed form controls taken from ``built``.

    ``built`` is the mandate the UI wizard assembled from the submitted form;
    keys the form does not carry (tolerances, rating bands, limits) stay as
    proposed so they are not reported as edits.
    """
    out = copy.deepcopy(proposed)
    for k in ("mandate_id", "case_id", "ticker", "cik", "cutoff_date", "trigger_document"):
        if k in built:
            out[k] = built[k]
    out["report_policy"] = copy.deepcopy(built["report_policy"])
    for k in FORM_CONTROLLED:
        if k in (built.get("assumptions") or {}):
            out["assumptions"][k] = copy.deepcopy(built["assumptions"][k])
    return out


def parse_set(spec: str) -> tuple[str, Any]:
    if "=" not in spec:
        raise PrefillError(f"--set expects path=value, got {spec!r}")
    path, raw = spec.split("=", 1)
    path = path.strip()
    if not path:
        raise PrefillError(f"--set expects path=value, got {spec!r}")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    return path, value


def finalize(proposed: dict, final: dict, by: str = "human",
             now: Callable[[], str] | None = None) -> dict:
    """Turn a reviewed ``final`` mandate into an accepted one (in place on a copy).

    Recomputes the sensitivity grid when WACC / terminal growth were edited
    and the grid itself was not, validates (schema, business rules, live
    rules), records ``approval`` with every edit and keeps ``provenance``.
    Raises PrefillError listing every problem.
    """
    from . import cli
    out = copy.deepcopy(final)
    pa, fa = proposed.get("assumptions") or {}, out.get("assumptions") or {}
    if ((fa.get("wacc"), fa.get("terminal_growth")) != (pa.get("wacc"), pa.get("terminal_growth"))
            and fa.get("sensitivity") == pa.get("sensitivity")
            and isinstance(fa.get("wacc"), (int, float)) and isinstance(
                fa.get("terminal_growth"), (int, float))):
        fa["sensitivity"] = cli._mandate_assumption_extras(
            fa["wacc"], fa["terminal_growth"], fa.get("as_of") or out["cutoff_date"])["sensitivity"]
    edits = edits_between(proposed, out)
    prov = copy.deepcopy(proposed.get("provenance") or {})
    for e in edits:
        entry = prov.setdefault(e["path"], _entry(
            e["proposed"], "none", "added by the reviewer"))
        entry["edited"] = True
        entry["final"] = e["final"]
    still = [p for p in (proposed.get("missing") or [])
             if _get_path(out, p) in (None, {}, [])]
    out["provenance"] = prov
    out["missing"] = still
    out["status"] = "accepted"
    out["approval"] = {"by": by or "human",
                       "at": (now or (lambda: datetime.now(timezone.utc).strftime(
                           "%Y-%m-%dT%H:%M:%SZ")))(),
                       "edits": edits}
    out.pop("warnings", None)
    problems = list(mandate_check.validate_mandate_full(out))
    problems += [f"still missing: {p} (supply it with --set {p}=<value>)" for p in still]
    problems += cli.live_mandate_problems(out)
    if problems:
        raise PrefillError("mandate cannot be accepted:\n  - " + "\n  - ".join(dict.fromkeys(problems)))
    return out


def accept_mandate(proposed: dict, sets: list[str] | None = None, by: str = "human",
                   now: Callable[[], str] | None = None) -> dict:
    """Apply ``--set path=value`` edits to a proposal and accept it."""
    if proposed.get("status") != "proposed":
        raise PrefillError(f"draft status is {proposed.get('status')!r}, expected 'proposed'")
    final = copy.deepcopy(proposed)
    for spec in sets or []:
        path, value = parse_set(spec)
        if path.split(".")[0] not in _EDIT_ROOTS:
            raise PrefillError(f"--set may only edit {', '.join(_EDIT_ROOTS)} (got {path})")
        try:
            _set_path(final, path, value)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise PrefillError(f"cannot set {path}: {exc}") from exc
    return finalize(proposed, final, by=by, now=now)


def review_rows(m: dict) -> list[tuple[str, str, str, str, str]]:
    """(field, value, source, as_of, basis) rows in provenance order; edited fields marked."""
    rows = []
    for path, e in (m.get("provenance") or {}).items():
        if path.endswith(".peers") and isinstance(e.get("value"), list):
            val = ", ".join(e["value"])
        else:
            val = _fmt(e.get("final") if e.get("edited") else e.get("value"))
        src = e.get("source") or ""
        basis = (e.get("basis") or "").replace("\n", " ")
        if e.get("edited"):
            basis = f"EDITED (proposed {_fmt(e.get('value'))}); " + basis
        rows.append((path, val, src, str(e.get("as_of") or ""), basis))
    return rows


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.6g}"
    return json.dumps(v) if isinstance(v, (list, dict)) else str(v)


def review_table(m: dict, basis_width: int = 70) -> str:
    """Plain-text review table: field, value, source, as_of, basis."""
    rows = review_rows(m)
    head = ("field", "value", "source", "as_of", "basis")
    w = [max(len(r[i]) for r in rows + [head]) for i in range(4)]
    w = [min(w[0], 52), min(w[1], 28), min(w[2], 24), w[3]]
    def cut(s, n):
        return s if len(s) <= n else s[:n - 1] + "…"
    lines = []
    for r in [head] + rows:
        lines.append("  ".join(cut(r[i], w[i]).ljust(w[i]) for i in range(4))
                     + "  " + cut(r[4], basis_width))
    status = m.get("status", "")
    foot = [f"status: {status}"]
    if m.get("missing"):
        foot.append("missing (enter manually): " + ", ".join(m["missing"]))
    for wn in m.get("warnings") or []:
        foot.append("warning: " + wn)
    return "\n".join(lines + [""] + foot)


# ------------------------------------------------------------- draft storage

def draft_location(ws: Path, ticker: str, cutoff: str, period: str = "fy2026q3") -> Path:
    """The case dir when the case exists, else workspace/drafts/<ticker>-<cutoff>/."""
    from .orchestrator import new_case_id
    case_dir = ws / "cases" / new_case_id(ticker, period, cutoff)
    if (case_dir / "state.json").exists():
        return case_dir
    return ws / "drafts" / f"{ticker.lower()}-{cutoff}"


def write_json(path: Path, obj: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def resolve_draft(ws: Path, ref: str) -> Path:
    """A proposal file from a file path, a draft/case directory, or a draft name."""
    p = Path(ref)
    cands = [p] if p.suffix == ".json" else [
        p / "mandate.proposed.json",
        ws / "drafts" / ref / "mandate.proposed.json",
        ws / "cases" / ref / "mandate.proposed.json"]
    for c in cands:
        if c.is_file():
            return c
    raise PrefillError(f"no mandate.proposed.json found for {ref!r}")


def accepted_path(proposed_file: Path) -> Path:
    """mandate.json beside the proposal; in an existing case dir (which already
    has an immutable mandate.json) mandate.accepted.json, never an overwrite."""
    target = proposed_file.with_name("mandate.json")
    return proposed_file.with_name("mandate.accepted.json") if target.exists() else target
