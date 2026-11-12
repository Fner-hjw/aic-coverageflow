"""Deterministic valuation engine: DCF + relative valuation + sensitivity.

Owner stream: A1 (runtime & data).

Contract (docs/contracts/interfaces.md section 4)::

    def value_company(model: Model, forecast: Forecast, assumptions: Assumptions) -> Valuation

Implements design-decisions section 4, revised for valuation_engine@2.0:
(a) DCF -- ONE base period (last twelve months: FY + current YTD - prior-year
    YTD, or the FY when the latest filing is a 10-K); unlevered FCF built from
    EBIT margin, D&A, capex, working capital and the mandate tax rate;
    5-year horizon driven by the forecast proposal (segment growth blended by
    LTM segment mix, optional operating-margin and capex paths); discounting
    from the valuation date using the period-end dates; terminal value both
    by exit multiple and Gordon growth, reconciled against each other; EV ->
    equity (debt, cash, non-controlling interests) -> per diluted share;
    target price and rating.
(b) Relative valuation from the dated mandate peer block + own historical
    multiple band.
(c) Sensitivity grid WACC x terminal growth (Gordon basis).
(d) cross_check_delta between DCF and comps upside; beyond mandate tolerance
    the note routes to the human / C2.

Pure Python, deterministic, never calls an LLM; typed BoundsViolation /
MissingFactError for out-of-bounds forecasts and missing base facts.
"""

from __future__ import annotations

import re
import statistics
from datetime import date
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from coverageflow.model_engine import Model

from coverageflow.model_engine import slugify

ENGINE_NAME = "valuation_engine@2.0"


class ValuationError(Exception):
    """Base class for valuation-engine errors."""


class MissingFactError(ValuationError):
    """A base input needed by the valuation is absent from the model."""


class BoundsViolation(ValuationError):
    """The forecast or assumptions violate mandate bounds."""




# ------------------------------------------------------------------ inputs

DEFAULT_TERMINAL_TOLERANCE = 0.35
DEFAULT_RATING_BANDS = {"buy": 0.15, "sell": -0.10}


@dataclass(frozen=True)
class Forecast:
    """Model-specialist forecast proposal (forecast_assumptions.json)."""
    horizon_years: int
    segments: tuple[tuple[str, tuple[float, ...]], ...]
    margin_glide: tuple[float, ...]
    operating_margin_path: tuple[float, ...] | None = None
    capex_pct_revenue_path: tuple[float, ...] | None = None

    @classmethod
    def from_payload(cls, payload: dict) -> "Forecast":
        horizon = int(payload.get("horizon_years", 5))
        segs = tuple(
            (str(s["segment"]).lower(), tuple(float(x) for x in s["growth"]))
            for s in payload.get("segments", [])
        )
        glide = tuple(float(x) for x in payload.get("margin_glide", {}).get("gross", []))

        def path(key):
            v = payload.get(key)
            return None if v is None else tuple(float(x) for x in v)

        return cls(horizon_years=horizon, segments=segs, margin_glide=glide,
                   operating_margin_path=path("operating_margin_path"),
                   capex_pct_revenue_path=path("capex_pct_revenue_path"))


@dataclass(frozen=True)
class Assumptions:
    """Human-owned mandate assumptions subset used by the engine."""
    wacc: float
    terminal_growth: float
    exit_multiple_ebitda: float
    discount_convention: str
    dcf_comps_tolerance: float
    peer_block: dict
    own_band: dict | None
    market_price: float | None
    forecast_bounds: dict | None
    sensitivity_points: tuple[list[float], list[float]]
    tax_rate: float = 0.0
    nwc_pct_incremental: float = 0.0
    terminal_tolerance: float = DEFAULT_TERMINAL_TOLERANCE
    rating_bands: dict | None = None
    candidate_id: str | None = None
    cutoff: str | None = None
    valuation_date: str | None = None

    @classmethod
    def from_payload(cls, p: dict) -> "Assumptions":
        if "wacc" not in p or "terminal_growth" not in p or "exit_multiple_ebitda" not in p:
            raise ValuationError("assumptions must include wacc, terminal_growth, exit_multiple_ebitda")
        if p.get("tax_rate") is None:
            raise MissingFactError(
                "mandate assumption tax_rate is missing; unlevered FCF cannot be built")
        sens = p.get("sensitivity", {})
        return cls(
            wacc=float(p["wacc"]),
            terminal_growth=float(p["terminal_growth"]),
            exit_multiple_ebitda=float(p["exit_multiple_ebitda"]),
            discount_convention=str(p.get("discount_convention", "year_end")),
            dcf_comps_tolerance=float(p.get("dcf_comps_tolerance", 0.25)),
            peer_block=dict(p.get("peer_block", {"peers": []})),
            own_band=p.get("own_band"),
            market_price=(p.get("market") or {}).get("price"),
            forecast_bounds=p.get("forecast_bounds"),
            tax_rate=float(p["tax_rate"]),
            nwc_pct_incremental=float(p.get("nwc_pct_incremental") or 0.0),
            terminal_tolerance=float(
                p.get("terminal_tolerance", DEFAULT_TERMINAL_TOLERANCE)),
            rating_bands=p.get("rating_bands"),
            candidate_id=p.get("candidate_id"),
            cutoff=p.get("cutoff") or p.get("as_of"),
            valuation_date=p.get("as_of") or p.get("cutoff"),
            sensitivity_points=(
                list(sens.get("wacc_points") or cls._around(float(p["wacc"]), 0.02)),
                list(sens.get("terminal_growth_points") or cls._around(float(p["terminal_growth"]), 0.01)),
            ),
        )

    @staticmethod
    def _around(center: float, step: float) -> list[float]:
        return [center - 2 * step, center - step, center, center + step, center + 2 * step]


@dataclass
class Valuation:
    payload: dict

    def to_dict(self) -> dict:
        return self.payload

    @property
    def dcf(self) -> dict:
        return self.payload["dcf"]

    @property
    def per_share(self) -> float:
        return self.payload["dcf"]["per_diluted_share"]["exit_multiple"]


# ------------------------------------------------------------------ helpers


def _model_get(model, name: str, default=None):
    if isinstance(model, dict):
        return model.get(name, default)
    return getattr(model, name, default)


def _series_values(model: "Model | dict", fld: str) -> dict[str, float]:
    """Period->value map from a Model or its to_dict() view (growth keys ignored)."""
    series = _model_get(model, "series", {}) or {}
    entry = series.get(fld, {})
    return {k: v for k, v in entry.items()
            if k not in ("yoy", "qoq", "yoy_computed", "growth_status")
            and isinstance(v, (int, float)) and not isinstance(v, bool)}


def _merged(model, names: tuple[str, ...]) -> dict[str, float]:
    """First series in *names* that has a period wins for that period."""
    out: dict[str, float] = {}
    for n in reversed(names):
        out.update(_series_values(model, n))
    return out


def _latest(values: dict[str, float]) -> tuple[str, float] | None:
    if not values:
        return None
    p = _latest_period(values)
    return p, values[p]


def _period_key(period: str) -> tuple[int, int, int]:
    """Sort key; an unparseable label sorts before every real period."""
    try:
        if period.startswith("FY"):
            return int(period[2:]), 5, 0
        if period.isdigit():
            return int(period), 5, 0
        p, ytd = period, 0
        if p.endswith("-YTD"):
            # a year-to-date column ends at the same quarter as the quarter
            # column but covers more time; rank it just below the quarter
            p, ytd = p[:-4], 1
        year, q = p.split("-Q")
        return int(year), int(q), -ytd
    except ValueError:
        return -1, 0, 0


def _latest_period(values: dict[str, float]) -> str:
    return max(values, key=_period_key)


_YTD_RE = re.compile(r"^(\d{4})-Q([1-4])-YTD$")
_Q1_RE = re.compile(r"^(\d{4})-Q1$")


def _ytd_parse(label: str) -> tuple[int, int] | None:
    """(year, quarter) for a year-to-date label; a Q1 quarter is its own YTD."""
    m = _YTD_RE.match(label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _Q1_RE.match(label)
    if m:
        return int(m.group(1)), 1
    return None


def _ytd_labels(year: int, q: int) -> tuple[str, ...]:
    return (f"{year}-Q{q}-YTD",) + ((f"{year}-Q1",) if q == 1 else ())


def _ytd_value(series: dict[str, float], year: int, q: int):
    for lab in _ytd_labels(year, q):
        if lab in series:
            return series[lab], lab
    return None, None


def _require(value, what: str) -> float:
    if value is None:
        raise MissingFactError(f"{what} missing from the model; cannot value")
    return float(value)


def _seg_bound(bounds: dict, seg: str):
    for k, v in (bounds.get("segments") or {}).items():
        if slugify(k) == slugify(seg):
            return v
    return None


def _check_path(name: str, path, h: int, lo, hi) -> None:
    if len(path) != h:
        raise BoundsViolation(f"{name} has {len(path)} points, horizon is {h}")
    for v in path:
        if lo is not None and v < float(lo) - 1e-12:
            raise BoundsViolation(f"{name} value {v} below mandate floor {lo}")
        if hi is not None and v > float(hi) + 1e-12:
            raise BoundsViolation(f"{name} value {v} above mandate ceiling {hi}")


def _validate_forecast(forecast: Forecast, assumptions: Assumptions) -> dict:
    """Validate against mandate bounds; returns what was bounded."""
    h = forecast.horizon_years
    if h <= 0:
        raise BoundsViolation(f"horizon years must be positive, got {h}")
    bounds = assumptions.forecast_bounds or {}
    for seg, growth in forecast.segments:
        if len(growth) != h:
            raise BoundsViolation(
                f"forecast horizon mismatch: segment {seg} has {len(growth)} growth points, horizon is {h}")
        seg_bounds = _seg_bound(bounds, seg)
        if seg_bounds is not None:
            lo, hi = float(seg_bounds[0]), float(seg_bounds[1])
            for g in growth:
                if not lo - 1e-12 <= g <= hi + 1e-12:
                    raise BoundsViolation(f"segment {seg} growth {g} outside mandate bounds [{lo}, {hi}]")
    if len(forecast.margin_glide) != h:
        raise BoundsViolation(
            f"margin glide has {len(forecast.margin_glide)} points, horizon is {h}")
    floor, ceil = bounds.get("margin_floor_gross"), bounds.get("margin_ceiling_gross")
    for m in forecast.margin_glide:
        if floor is not None and m < float(floor) - 1e-12:
            raise BoundsViolation(f"margin glide {m} below mandate floor {floor}")
        if ceil is not None and m > float(ceil) + 1e-12:
            raise BoundsViolation(f"margin glide {m} above mandate ceiling {ceil}")
    status = {"operating_margin_path": "absent", "capex_pct_revenue_path": "absent"}
    if forecast.operating_margin_path is not None:
        lo, hi = bounds.get("operating_margin_floor"), bounds.get("operating_margin_ceiling")
        _check_path("operating_margin_path", forecast.operating_margin_path, h, lo, hi)
        status["operating_margin_path"] = "bounded" if (lo, hi) != (None, None) else "unbounded"
    if forecast.capex_pct_revenue_path is not None:
        cb = bounds.get("capex_pct_bounds")
        lo, hi = (cb[0], cb[1]) if cb else (None, None)
        _check_path("capex_pct_revenue_path", forecast.capex_pct_revenue_path, h, lo, hi)
        status["capex_pct_revenue_path"] = "bounded" if cb else "unbounded"
    if float(assumptions.terminal_growth) >= float(assumptions.wacc):
        raise BoundsViolation(
            f"terminal growth {assumptions.terminal_growth} must be below WACC {assumptions.wacc}")
    return status


# --------------------------------------------------------------------- base

_NI_KEYS = ("income_statement.net_income_attributable",
            "income_statement.net_income_attributable_to_parent",
            "income_statement.net_income_attributable_to_company",
            "income_statement.net_income")
_FLOWS = {  # name -> (series candidates, required)
    "revenue": (("income_statement.revenue",), True),
    "ebit": (("income_statement.operating_income",), True),
    "da": (("cash_flow.depreciation_amortization",
            "income_statement.depreciation_and_amortization"), True),
    "ocf": (("cash_flow.operating_cash_flow",), True),
    "capex": (("cash_flow.capital_expenditure",), True),
    "gross_profit": (("income_statement.gross_profit",), False),
    "net_income": (_NI_KEYS, False),
}
_FLOW_LABEL = {"revenue": "revenue", "ebit": "operating income (EBIT)",
               "da": "depreciation and amortization", "ocf": "operating cash flow",
               "capex": "capital expenditure", "gross_profit": "gross profit",
               "net_income": "net income"}


def _latest_fy(rev: dict[str, float]) -> str | None:
    fy = [p for p in rev if p.startswith("FY")]
    return max(fy, key=_period_key) if fy else None


def _ltm_flows(flows: dict[str, dict[str, float]], fy: str, cur: tuple[int, int],
               prior: tuple[int, int]):
    """(values, missing) with LTM = FY + YTD_cur - YTD_prior for every flow."""
    out, missing = {}, []
    for name, series in flows.items():
        c, _ = _ytd_value(series, *cur)
        p, _ = _ytd_value(series, *prior)
        f = series.get(fy)
        if None in (c, p, f):
            missing.append(name)
        else:
            out[name] = f + c - p
    return out, missing


def _resolve_base(model) -> dict:
    """The single base window for every flow, plus balance items."""
    flows = {n: _merged(model, c) for n, (c, _) in _FLOWS.items()}
    rev = flows["revenue"]
    if not rev:
        raise MissingFactError("revenue missing from the model; cannot value")
    fy = _latest_fy(rev)
    ytds = [(k, lab) for lab in rev if (k := _ytd_parse(lab))]
    ytd = max(ytds)[0] if ytds else None
    fy_year = int(fy[2:]) if fy else None

    method, reason, window, values = None, None, {}, {}
    if ytd and (fy is None or ytd[0] > fy_year):
        cur, prior = ytd, (ytd[0] - 1, ytd[1])
        fy_last = f"FY{ytd[0] - 1}"
        required = {n: flows[n] for n, (_, req) in _FLOWS.items() if req}
        got, missing = _ltm_flows(required, fy_last, cur, prior)
        if not missing:
            opt = {n: flows[n] for n, (_, req) in _FLOWS.items() if not req}
            more, _ = _ltm_flows(opt, fy_last, cur, prior)
            values = {**got, **more}
            method = "ltm"
            window = {"fy": fy_last,
                      "ytd_current": _ytd_value(rev, *cur)[1],
                      "ytd_prior": _ytd_value(rev, *prior)[1]}
            end_label = window["ytd_current"]
        else:
            what = ", ".join(_FLOW_LABEL[m] for m in missing)
            labels = []
            for lab, get in (
                    (fy_last, lambda sr: sr.get(fy_last)),
                    (f"{cur[0]}-Q{cur[1]}-YTD", lambda sr: _ytd_value(sr, *cur)[0]),
                    (f"{prior[0]}-Q{prior[1]}-YTD", lambda sr: _ytd_value(sr, *prior)[0])):
                if any(get(required[m]) is None for m in missing):
                    labels.append(lab)
            reason = (f"cannot build last-twelve-months to {cur[0]}-Q{cur[1]}: "
                      f"{', '.join(labels)} missing for {what}")
            if fy is None:
                raise MissingFactError(
                    f"no fiscal year and no complete year-to-date pair: {reason}")
    if method is None:
        if fy is None:
            raise MissingFactError("no fiscal-year revenue in the model; cannot value")
        values = {n: flows[n].get(fy) for n in flows}
        for n, (_, req) in _FLOWS.items():
            if req and values[n] is None:
                raise MissingFactError(
                    f"{_FLOW_LABEL[n]} for {fy} missing from the model; cannot value")
        newer = sorted((k for k in (rev) if not k.startswith("FY")
                        and _period_key(k)[0] > fy_year))
        if reason is None and newer:
            reason = (f"no year-to-date columns to build last-twelve-months "
                      f"(newest period {newer[-1]} is a single quarter)")
        method = "fiscal_year" if reason is None else "fiscal_year_fallback"
        window = {"fy": fy, "ytd_current": None, "ytd_prior": None}
        end_label = fy

    revenue = values["revenue"]
    if not revenue or revenue <= 0:
        raise MissingFactError(f"base revenue is not positive ({revenue}); cannot value")
    ebit, da = values["ebit"], values["da"]
    capex, ocf = values["capex"], values["ocf"]
    base = {
        "method": method,
        "window": window,
        "reason": reason,
        "end_label": end_label,
        "revenue": revenue,
        "gross_profit": values.get("gross_profit"),
        "gross_margin": (values["gross_profit"] / revenue
                         if values.get("gross_profit") is not None else None),
        "ebit": ebit, "ebit_margin": ebit / revenue,
        "da": da, "da_pct_revenue": da / revenue,
        "ebitda": ebit + da, "ebitda_margin": (ebit + da) / revenue,
        "ocf": ocf,
        "capex": capex, "capex_pct_revenue": capex / revenue,
        "fcf": ocf - capex, "fcf_margin": (ocf - capex) / revenue,
        "net_income": values.get("net_income"),
    }
    return base


def _balance(model) -> dict:
    """Bridge lines from the latest balance-sheet date."""
    def ser(n):
        return _series_values(model, n)

    st, lt = ser("balance_sheet.short_term_debt"), ser("balance_sheet.long_term_debt")
    cash, sti = ser("balance_sheet.cash_and_equivalents"), ser("balance_sheet.short_term_investments")
    common = set(st) & set(lt) & set(cash)
    out: dict = {"period": None, "short_term_debt": None, "long_term_debt": None,
                 "total_debt": None, "cash_and_equivalents": None,
                 "short_term_investments": None, "cash_total": None}
    if common:
        p = max(common, key=_period_key)
        s_i = sti.get(p) or 0.0
        out.update(period=p, short_term_debt=st[p], long_term_debt=lt[p],
                   total_debt=st[p] + lt[p], cash_and_equivalents=cash[p],
                   short_term_investments=s_i, cash_total=cash[p] + s_i)
        net_debt = out["total_debt"] - out["cash_total"]
    else:
        nd = _latest(ser("derived.net_debt"))
        if nd is None:
            raise MissingFactError(
                "debt and cash missing from the model (no balance-sheet period "
                "carries short-term debt, long-term debt and cash); cannot value")
        out["period"] = nd[0]
        out["components_absent"] = True
        net_debt = nd[1]
    nci_series = _merged(model, ("balance_sheet.noncontrolling_interests",
                                 "balance_sheet.noncontrolling_interest",
                                 "balance_sheet.minority_interest"))
    nci_p = out["period"] if out["period"] in nci_series else (
        max(nci_series, key=_period_key) if nci_series else None)
    if nci_p is not None:
        out["nci"], out["nci_source"], out["nci_period"] = nci_series[nci_p], "balance_sheet", nci_p
    else:
        out["nci"], out["nci_source"], out["nci_period"] = 0.0, "absent", None
    out["net_debt"] = net_debt
    out["net_claims"] = net_debt + out["nci"]
    return out


# ----------------------------------------------------------------- discount


def _add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + n, day=28)


def _discount_plan(model, base: dict, assumptions: Assumptions, horizon: int):
    conv = assumptions.discount_convention
    if conv not in ("year_end", "year-end", "mid_year", "mid-year"):
        raise BoundsViolation(f"unknown discount convention {conv!r}")
    mid = conv in ("mid_year", "mid-year")
    ends = _model_get(model, "period_ends", {}) or {}
    end_label = base["end_label"]
    end_s = ends.get(end_label)
    if end_label.endswith("-YTD") and not end_s:
        end_s = ends.get(end_label[:-4])
    val_s = assumptions.valuation_date
    plan = {"convention": conv, "valuation_date": val_s, "ltm_end": end_s}
    try:
        end_d = date.fromisoformat(end_s) if end_s else None
        val_d = date.fromisoformat(val_s) if val_s else None
    except ValueError:
        end_d = val_d = None
    if end_d is None or val_d is None:
        plan["discount_timing"] = "integer_fallback"
        exps = [t - (0.5 if mid else 0.0) for t in range(1, horizon + 1)]
        dates = [None] * horizon
    else:
        plan["discount_timing"] = "dated"
        exps, dates = [], []
        for t in range(1, horizon + 1):
            cf = _add_years(end_d, t)
            e = (cf - val_d).days / 365.25 - (0.5 if mid else 0.0)
            exps.append(max(e, 0.0))
            dates.append(cf.isoformat())
    plan["exponents"] = exps
    plan["cash_flow_dates"] = dates
    return plan


# ------------------------------------------------------------------ segments


def _segment_blend(model, forecast: Forecast, base: dict) -> tuple[list[float], dict]:
    slugs = list(_model_get(model, "segment_slugs", []) or [])
    growth_map = {slugify(k): v for k, v in forecast.segments}
    h = forecast.horizon_years
    seg_series = {s: _series_values(model, f"segments.{s}.revenue") for s in slugs}
    with_data = {s: v for s, v in seg_series.items() if v}
    info: dict = {"segment_slugs": slugs,
                  "segments_without_data": [s for s in slugs if s not in with_data]}

    weights, method, period = None, None, None
    if with_data:
        w = base["window"]
        if base["method"] == "ltm":
            cur = _ytd_parse(w["ytd_current"])
            prior = _ytd_parse(w["ytd_prior"])
            got, missing = _ltm_flows(
                {s: v for s, v in with_data.items()}, w["fy"], cur, prior)
            if not missing:
                weights, method, period = got, "ltm_segments", w
        elif base["method"] in ("fiscal_year", "fiscal_year_fallback"):
            fy = w["fy"]
            if all(fy in v for v in with_data.values()):
                weights = {s: v[fy] for s, v in with_data.items()}
                method, period = "fiscal_year_segments", fy
        if weights is None:
            common = set.intersection(*(set(v) for v in with_data.values()))
            if common:
                p = max(common, key=_period_key)
                weights = {s: v[p] for s, v in with_data.items()}
                method, period = "latest_common_period", p

    if weights and sum(max(x, 0.0) for x in weights.values()) > 0:
        missing_seg = [s for s in slugs if s not in growth_map]
        if missing_seg:
            raise BoundsViolation(
                "proposal omits segment " + ", ".join(missing_seg)
                + " (every segment in segment_slugs needs a growth path)")
        total = sum(max(x, 0.0) for x in weights.values())
        blended = [sum(max(weights[s], 0.0) * growth_map[s][t] for s in weights) / total
                   for t in range(h)]
        info.update(method=method, period=period,
                    weights={s: max(x, 0.0) / total for s, x in weights.items()})
        extra = sorted(set(growth_map) - set(slugs))
        if extra:
            info["ignored_proposal_segments"] = extra
        return blended, info
    g = growth_map.get("total")
    if g is None:
        raise MissingFactError(
            "no usable segment revenue in the model and no 'total' growth in the forecast")
    info.update(method="total_growth", period=None, weights={})
    return list(g), info


# ------------------------------------------------------------------- engine


def value_company(
    model: "Model | dict",
    forecast: Forecast | dict,
    assumptions: Assumptions | dict,
) -> Valuation:
    """Deterministic DCF + comps + sensitivity on the built model series."""
    if not isinstance(forecast, Forecast):
        forecast = Forecast.from_payload(forecast)
    if not isinstance(assumptions, Assumptions):
        assumptions = Assumptions.from_payload(assumptions)
    path_status = _validate_forecast(forecast, assumptions)

    base = _resolve_base(model)
    bridge = _balance(model)
    shares_t = _latest(_series_values(
        model, "income_statement.diluted_shares_outstanding"))
    shares = _require(shares_t[1] if shares_t else None, "diluted shares")
    h = forecast.horizon_years
    blended, blend = _segment_blend(model, forecast, base)
    plan = _discount_plan(model, base, assumptions, h)
    w, g, tax = assumptions.wacc, assumptions.terminal_growth, assumptions.tax_rate
    nwc_pct = assumptions.nwc_pct_incremental

    years = []
    prev_rev = base["revenue"]
    for t in range(1, h + 1):
        revenue = prev_rev * (1.0 + blended[t - 1])
        ebit_m = (forecast.operating_margin_path[t - 1]
                  if forecast.operating_margin_path is not None else base["ebit_margin"])
        capex_pct = (forecast.capex_pct_revenue_path[t - 1]
                     if forecast.capex_pct_revenue_path is not None
                     else base["capex_pct_revenue"])
        ebit = revenue * ebit_m
        da = revenue * base["da_pct_revenue"]
        capex = revenue * capex_pct
        nwc_change = nwc_pct * (revenue - prev_rev)
        nopat = ebit * (1.0 - tax)
        ufcf = nopat + da - capex - nwc_change
        e = plan["exponents"][t - 1]
        df = (1.0 + w) ** -e
        years.append({
            "year": t, "revenue": revenue, "growth": blended[t - 1],
            "gross_margin": forecast.margin_glide[t - 1],
            "ebit_margin": ebit_m, "ebit": ebit,
            "da": da, "ebitda": ebit + da, "ebitda_margin": ebit_m + base["da_pct_revenue"],
            "capex": capex, "capex_pct_revenue": capex_pct,
            "nwc_change": nwc_change, "nopat": nopat,
            "ufcf": ufcf, "fcf": ufcf, "fcf_margin": ufcf / revenue,
            "date": plan["cash_flow_dates"][t - 1],
            "exponent": e, "discount_factor": df, "pv": ufcf * df,
        })
        prev_rev = revenue
    pv_sum = sum(y["pv"] for y in years)

    ufcf_final, ebitda_final = years[-1]["ufcf"], years[-1]["ebitda"]
    df_final = years[-1]["discount_factor"]
    tv_exit = ebitda_final * assumptions.exit_multiple_ebitda
    terminal_fcf = _terminal_fcf(years[-1], g, nwc_pct)
    tv_gordon = terminal_fcf / (w - g)
    implied_multiple = tv_gordon / ebitda_final if ebitda_final else None
    # solve tv_exit = nopat_H(1+g)/(w-g) for g (nwc term is second order and
    # is left out of the inversion; the reported figure is labelled implied)
    nopat_final = years[-1]["nopat"]
    denom = tv_exit + nopat_final
    implied_g = (tv_exit * w - nopat_final) / denom if denom else None
    ratio = tv_exit / tv_gordon if tv_gordon > 0 else None
    within = ratio is not None and abs(ratio - 1.0) <= assumptions.terminal_tolerance
    reconciliation = {"ratio": ratio, "tolerance": assumptions.terminal_tolerance,
                      "within": bool(within)}

    ev = {"exit_multiple": pv_sum + tv_exit * df_final,
          "gordon": pv_sum + tv_gordon * df_final}
    equity = {k: v - bridge["net_claims"] for k, v in ev.items()}
    per_share = {k: v / shares for k, v in equity.items()}

    if within:
        target = (per_share["exit_multiple"] + per_share["gordon"]) / 2.0
        target_basis = "mean_exit_multiple_and_gordon"
    else:
        target = per_share["gordon"]
        target_basis = "gordon_only_terminal_values_disagree"
    price = assumptions.market_price
    upside = (target / price - 1.0) if price else None
    bands = {**DEFAULT_RATING_BANDS, **(assumptions.rating_bands or {})}
    rating = None
    if upside is not None:
        rating = ("Buy" if upside > bands["buy"]
                  else "Sell" if upside < bands["sell"] else "Hold")

    comps = _relative_valuation(assumptions, base, bridge["net_claims"], shares)
    dcf_payload = {
        "base": {
            "method": base["method"],
            "window": base["window"],
            "reason": base["reason"],
            "period_end": plan["ltm_end"],
            "revenue": base["revenue"],
            "gross_profit": base["gross_profit"], "gross_margin": base["gross_margin"],
            "ebit": base["ebit"], "ebit_margin": base["ebit_margin"],
            "da": base["da"], "da_pct_revenue": base["da_pct_revenue"],
            "ebitda": base["ebitda"], "ebitda_margin": base["ebitda_margin"],
            "ocf": base["ocf"],
            "capex": base["capex"], "capex_pct_revenue": base["capex_pct_revenue"],
            "fcf": base["fcf"], "fcf_margin": base["fcf_margin"],
            "net_income": base["net_income"],
        },
        "cash_flow_build": {
            "tax_rate": tax, "nwc_pct_incremental": nwc_pct,
            "ebit_margin_source": ("proposal.operating_margin_path"
                                   if forecast.operating_margin_path is not None
                                   else "ltm_flat"),
            "capex_source": ("proposal.capex_pct_revenue_path"
                             if forecast.capex_pct_revenue_path is not None
                             else "ltm_flat"),
            "proposal_paths": path_status,
        },
        "blend": blend,
        "blended_growth": blended,
        "years": years,
        "pv_sum": pv_sum,
        "discounting": plan,
        "terminal": {
            "exit_multiple": {
                "ebitda_final": ebitda_final,
                "multiple": assumptions.exit_multiple_ebitda,
                "terminal_value": tv_exit, "pv": tv_exit * df_final,
            },
            "gordon": {
                "wacc": w, "terminal_growth": g, "ufcf_final": ufcf_final,
                "terminal_fcf": terminal_fcf,
                "terminal_fcf_basis": ("normalised: NOPAT and working capital "
                                       "grow at g, capex = depreciation"),
                "terminal_value": tv_gordon, "pv": tv_gordon * df_final,
            },
            "implied_exit_multiple_from_gordon": implied_multiple,
            "implied_growth_from_exit": implied_g,
            "reconciliation": reconciliation,
        },
        "ev": ev,
        "bridge": bridge,
        "equity_value": equity,
        "per_diluted_share": per_share,
        "target_price": target,
        "target_basis": target_basis,
        "target_flag": None if within else "terminal_values_outside_tolerance",
        "upside": upside,
        # citable as [EV:dcf.market_price]: the note states the price the
        # upside and rating are measured against, and had no key for it
        "market_price": assumptions.market_price,
        "rating": rating,
        "rating_bands": bands,
        "net_debt": bridge["net_debt"],
        "net_debt_period": bridge["period"],
        "diluted_shares": shares,
        "shares_period": shares_t[0],
        "discount_convention": assumptions.discount_convention,
        "wacc": w,
        "dcf_upside": upside,
    }

    sensitivity = _sensitivity_grid(years, assumptions, bridge["net_claims"], shares)

    comps_upside = comps["comps_upside"]
    within_tolerance = None
    delta = None
    absent = []
    if assumptions.market_price is None:
        absent.append("the mandate's market price")
    if comps.get("comps_price") is None:
        absent.append("a dated peer comparable price")
    if absent:
        subject = " and ".join(absent) + (" are" if len(absent) > 1 else " is")
    else:
        subject = "an input is"
    note = ("DCF and relative valuation cross-check unavailable: "
            + subject + " not set, so neither a DCF upside nor a comps upside "
              "can be computed. The own-band implied prices are reported for "
              "reference only; they are not a cross-check.")
    if upside is not None and comps_upside is not None:
        delta = upside - comps_upside
        within_tolerance = abs(delta) <= assumptions.dcf_comps_tolerance
        if within_tolerance:
            note = "DCF and relative valuation agree within mandate tolerance."
        else:
            note = (
                f"DCF upside ({upside:+.1%}) and comps upside ({comps_upside:+.1%}) "
                f"disagree by {delta:+.1%}, beyond mandate tolerance "
                f"{assumptions.dcf_comps_tolerance:.0%}: escalated to the human approver "
                f"and critic C2 for review of forecast drivers, peer block and multiples."
            )

    facts_sha = _model_get(model, "facts_sha", None) or _model_get(model, "input_facts_sha256", None)
    candidate_id = assumptions.candidate_id or _model_get(model, "candidate_id", None)
    payload = {
        "artifact": "valuation",
        "engine": ENGINE_NAME,
        "input_facts_sha256": facts_sha,
        "candidate_id": candidate_id,
        "cutoff": assumptions.cutoff,
        "assumptions": {
            "wacc": w, "terminal_growth": g,
            "exit_multiple_ebitda": assumptions.exit_multiple_ebitda,
            "discount_convention": assumptions.discount_convention,
            "tax_rate": tax, "nwc_pct_incremental": nwc_pct,
            "terminal_tolerance": assumptions.terminal_tolerance,
            "valuation_date": assumptions.valuation_date,
            "peer_block_as_of": assumptions.peer_block.get("as_of"),
        },
        "dcf": dcf_payload,
        "comps": comps,
        "sensitivity": sensitivity,
        "cross_check_delta": delta,
        "cross_check_note": note,
        "within_tolerance": within_tolerance,
    }
    return Valuation(payload)


def _relative_valuation(assumptions: Assumptions, base: dict,
                        net_claims: float, shares: float) -> dict:
    """Peer multiples applied to own LTM metrics; own historical band."""
    peers = assumptions.peer_block.get("peers", [])
    ev_ebitda = [float(p["ev_ebitda"]) for p in peers if p.get("ev_ebitda") is not None]
    p_e = [float(p["p_e_ttm"]) for p in peers if p.get("p_e_ttm") is not None]
    p_s = [float(p["p_s"]) for p in peers if p.get("p_s") is not None]

    ebitda = base["ebitda"]
    ni = base.get("net_income")
    revenue = base["revenue"]

    applied: dict[str, dict] = {}
    if ev_ebitda:
        med = statistics.median(ev_ebitda)
        applied["ev_ebitda"] = {
            "peer_median": med, "own_metric": ebitda,
            "implied_ev": med * ebitda,
            "implied_price": (med * ebitda - net_claims) / shares,
        }
    if p_e and ni is not None:
        med = statistics.median(p_e)
        applied["p_e"] = {
            "peer_median": med, "own_metric": ni,
            "implied_price": ni / shares * med,
        }
    if p_s:
        med = statistics.median(p_s)
        applied["p_s"] = {
            "peer_median": med, "own_metric": revenue,
            "implied_price": revenue / shares * med,
        }
    prices = [a["implied_price"] for a in applied.values()]
    comps_price = statistics.median(prices) if prices else None
    comps_upside = comps_price / assumptions.market_price - 1.0 \
        if comps_price is not None and assumptions.market_price else None

    band_out: dict[str, dict] = {}
    if assumptions.own_band:
        for key, compute in (
            ("ev_ebitda", lambda m: (m * ebitda - net_claims) / shares),
            ("p_e", lambda m: ni / shares * m),
            ("p_s", lambda m: revenue / shares * m),
        ):
            if key not in assumptions.own_band:
                continue
            lo, hi = assumptions.own_band[key]
            try:
                lo_p, hi_p = compute(lo), compute(hi)
            except TypeError:  # metric missing -> band not computable
                continue
            band_out[key] = {"band": [lo, hi], "implied_price": [lo_p, hi_p]}

    return {
        "as_of": assumptions.peer_block.get("as_of"),
        "peers": peers,
        "applied": applied,
        "comps_price": comps_price,
        "comps_upside": comps_upside,
        "own_band": band_out,
    }


def _terminal_fcf(last_year: dict, g: float, nwc_pct: float) -> float:
    """Steady-state free cash flow for the first year after the horizon.

    Gordon growth needs a cash flow that can grow at ``g`` for ever. The
    final forecast year is not one when capex still runs ahead of
    depreciation (a build-out phase): its free cash flow is depressed, often
    negative, and a perpetuity on it says nothing about the business. So the
    terminal year is normalised: revenue and EBIT grow at ``g``, capex equals
    depreciation (maintenance only), and working capital grows with revenue.
    """
    nopat_next = last_year["nopat"] * (1.0 + g)
    nwc_next = nwc_pct * last_year["revenue"] * g
    return nopat_next - nwc_next


def _sensitivity_grid(years, assumptions: Assumptions, net_claims, shares) -> dict:
    wacc_points, g_points = assumptions.sensitivity_points
    e_final = years[-1]["exponent"]
    grid = []
    for w in wacc_points:
        row = []
        for g in g_points:
            if g >= w:  # Gordon undefined; mark cell as None
                row.append(None)
                continue
            pv_flows = sum(y["ufcf"] / (1.0 + w) ** y["exponent"] for y in years)
            tv = _terminal_fcf(years[-1], g, assumptions.nwc_pct_incremental) / (w - g)
            ev = pv_flows + tv / (1.0 + w) ** e_final
            row.append((ev - net_claims) / shares)
        grid.append(row)
    return {
        "basis": "gordon",
        "wacc_points": wacc_points,
        "terminal_growth_points": g_points,
        "grid": grid,
    }
