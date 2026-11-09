"""Deterministic valuation engine: DCF + relative valuation + sensitivity.

Owner stream: A1 (runtime & data).

Contract (docs/contracts/interfaces.md section 4)::

    def value_company(model: Model, forecast: Forecast, assumptions: Assumptions) -> Valuation

Implements design-decisions section 4:
(a) DCF — FCF = OCF - capex from model facts as the base margin; 5-year
    horizon driven by the forecast proposal (segment revenue growth blended
    by latest segment mix + gross-margin glide path); terminal value BOTH
    via exit multiple (primary) AND Gordon growth (cross-check); WACC and
    terminal growth are human-owned mandate assumptions; year-end (or
    mid-year, per assumptions) discounting; EV -> equity -> per diluted share.
(b) Relative valuation from the dated mandate peer block + own historical
    multiple band.
(c) Sensitivity grid WACC x terminal growth (Gordon basis).
(d) cross_check_delta between DCF and comps upside; beyond mandate tolerance
    the note routes to the human / C2.

Pure Python, deterministic, never calls an LLM; typed BoundsViolation /
MissingFactError for out-of-bounds forecasts and missing base facts.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from coverageflow.model_engine import Model

ENGINE_NAME = "valuation_engine@1.0"


class ValuationError(Exception):
    """Base class for valuation-engine errors."""


class MissingFactError(ValuationError):
    """A base input needed by the valuation is absent from the model."""


class BoundsViolation(ValuationError):
    """The forecast or assumptions violate mandate bounds."""


# ------------------------------------------------------------------ inputs


@dataclass(frozen=True)
class Forecast:
    """Model-specialist forecast proposal (forecast_assumptions.json)."""
    horizon_years: int
    segments: tuple[tuple[str, tuple[float, ...]], ...]
    margin_glide: tuple[float, ...]

    @classmethod
    def from_payload(cls, payload: dict) -> "Forecast":
        horizon = int(payload.get("horizon_years", 5))
        segs = tuple(
            (str(s["segment"]).lower(), tuple(float(x) for x in s["growth"]))
            for s in payload.get("segments", [])
        )
        glide = tuple(float(x) for x in payload.get("margin_glide", {}).get("gross", []))
        return cls(horizon_years=horizon, segments=segs, margin_glide=glide)


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
    candidate_id: str | None = None
    cutoff: str | None = None

    @classmethod
    def from_payload(cls, p: dict) -> "Assumptions":
        if "wacc" not in p or "terminal_growth" not in p or "exit_multiple_ebitda" not in p:
            raise ValuationError("assumptions must include wacc, terminal_growth, exit_multiple_ebitda")
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
            candidate_id=p.get("candidate_id"),
            cutoff=p.get("cutoff") or p.get("as_of"),
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


def _series_values(model: "Model | dict", fld: str) -> dict[str, float]:
    """Period->value map from a Model or its to_dict() view (growth keys ignored)."""
    series = model.series if hasattr(model, "series") else model.get("series", {})
    entry = series.get(fld, {})
    return {k: v for k, v in entry.items()
            if k not in ("yoy", "qoq", "yoy_computed") and isinstance(v, (int, float))}


def _latest(values: dict[str, float]) -> tuple[str, float] | None:
    if not values:
        return None
    p = _latest_period(values)
    return p, values[p]


def _period_key(period: str) -> tuple[int, int]:
    if period.startswith("FY"):
        return int(period[2:]), 5
    year, q = period.split("-Q")
    return int(year), int(q)


def _latest_period(values: dict[str, float]) -> str:
    return max(values, key=_period_key)


def _require(value, what: str) -> float:
    if value is None:
        raise MissingFactError(f"{what} missing from the model; cannot value")
    return float(value)


def _validate_forecast(forecast: Forecast, assumptions: Assumptions) -> None:
    h = forecast.horizon_years
    if h <= 0:
        raise BoundsViolation(f"horizon years must be positive, got {h}")
    for seg, growth in forecast.segments:
        if len(growth) != h:
            raise BoundsViolation(
                f"forecast horizon mismatch: segment {seg} has {len(growth)} growth points, horizon is {h}")
        bounds = assumptions.forecast_bounds or {}
        seg_bounds = (bounds.get("segments") or {}).get(seg.upper()) or \
                     (bounds.get("segments") or {}).get(seg)
        if seg_bounds is not None:
            lo, hi = float(seg_bounds[0]), float(seg_bounds[1])
            for g in growth:
                if not lo - 1e-12 <= g <= hi + 1e-12:
                    raise BoundsViolation(f"segment {seg} growth {g} outside mandate bounds [{lo}, {hi}]")
    if len(forecast.margin_glide) != h:
        raise BoundsViolation(
            f"margin glide has {len(forecast.margin_glide)} points, horizon is {h}")
    if assumptions.forecast_bounds:
        floor = assumptions.forecast_bounds.get("margin_floor_gross")
        ceil = assumptions.forecast_bounds.get("margin_ceiling_gross")
        for m in forecast.margin_glide:
            if floor is not None and m < float(floor) - 1e-12:
                raise BoundsViolation(f"margin glide {m} below mandate floor {floor}")
            if ceil is not None and m > float(ceil) + 1e-12:
                raise BoundsViolation(f"margin glide {m} above mandate ceiling {ceil}")
    if float(assumptions.terminal_growth) >= float(assumptions.wacc):
        raise BoundsViolation(
            f"terminal growth {assumptions.terminal_growth} must be below WACC {assumptions.wacc}")


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
    _validate_forecast(forecast, assumptions)

    base_period, base = _resolve_base(model)
    revenue0 = _require(base["revenue"], "latest annual revenue")
    gross0 = _require(base["gross_margin"], "gross margin")
    ebitda_m0 = _require(base["ebitda_margin"], "EBITDA margin")
    fcf_base = _require(base["fcf"], "FCF (OCF - capex)")
    fcf_m0 = fcf_base / revenue0
    net_debt = _require(base["net_debt"], "net debt")
    shares = _require(base["shares"], "diluted shares")

    blended = _blended_growth(model, forecast, base_period)
    w, g = assumptions.wacc, assumptions.terminal_growth
    conv = assumptions.discount_convention
    exponent = _exponents(forecast.horizon_years, conv)

    years = []
    revenue = revenue0
    for t in range(1, forecast.horizon_years + 1):
        revenue = revenue * (1.0 + blended[t - 1])
        gross_t = forecast.margin_glide[t - 1]
        fcf_m_t = fcf_m0 + (gross_t - gross0)
        ebitda_m_t = ebitda_m0 + (gross_t - gross0)
        fcf_t = revenue * fcf_m_t
        df = (1.0 + w) ** -exponent[t - 1]
        years.append({
            "year": t,
            "revenue": revenue,
            "gross_margin": gross_t,
            "fcf_margin": fcf_m_t,
            "ebitda_margin": ebitda_m_t,
            "fcf": fcf_t,
            "discount_factor": df,
            "pv": fcf_t * df,
        })
    pv_sum = sum(y["pv"] for y in years)

    fcf_final = years[-1]["fcf"]
    ebitda_final = years[-1]["revenue"] * years[-1]["ebitda_margin"]
    df_final = years[-1]["discount_factor"]
    tv_exit = ebitda_final * assumptions.exit_multiple_ebitda
    tv_exit_pv = tv_exit * df_final
    tv_gordon = fcf_final * (1.0 + g) / (w - g)
    tv_gordon_pv = tv_gordon * df_final

    ev = {"exit_multiple": pv_sum + tv_exit_pv, "gordon": pv_sum + tv_gordon_pv}
    equity = {k: v - net_debt for k, v in ev.items()}
    per_share = {k: v / shares for k, v in equity.items()}

    dcf_upside = (per_share["exit_multiple"] / assumptions.market_price - 1.0) \
        if assumptions.market_price else None

    comps = _relative_valuation(model, assumptions, base_period, base, net_debt, shares)
    dcf_payload = {
        "base": {
            "period": base_period,
            "revenue": revenue0,
            "gross_margin": gross0,
            "ebitda_margin": ebitda_m0,
            "fcf": fcf_base,
            "fcf_margin": fcf_m0,
        },
        "blended_growth": blended,
        "years": years,
        "pv_sum": pv_sum,
        "terminal": {
            "exit_multiple": {
                "ebitda_final": ebitda_final,
                "multiple": assumptions.exit_multiple_ebitda,
                "terminal_value": tv_exit,
                "pv": tv_exit_pv,
            },
            "gordon": {
                "wacc": w, "terminal_growth": g,
                "terminal_value": tv_gordon, "pv": tv_gordon_pv,
            },
        },
        "ev": ev,
        "equity_value": equity,
        "per_diluted_share": per_share,
        "net_debt": net_debt,
        "diluted_shares": shares,
        "discount_convention": conv,
        "wacc": w,
        "dcf_upside": dcf_upside,
    }

    sensitivity = _sensitivity_grid(years, pv_sum, assumptions, net_debt, shares)

    comps_upside = comps["comps_upside"]
    within_tolerance = True
    delta = None
    note = "DCF and relative valuation agree within mandate tolerance."
    if dcf_upside is not None and comps_upside is not None:
        delta = dcf_upside - comps_upside
        within_tolerance = abs(delta) <= assumptions.dcf_comps_tolerance
        if not within_tolerance:
            note = (
                f"DCF upside ({dcf_upside:+.1%}) and comps upside ({comps_upside:+.1%}) "
                f"disagree by {delta:+.1%}, beyond mandate tolerance "
                f"{assumptions.dcf_comps_tolerance:.0%}: escalated to the human approver "
                f"and critic C2 for review of forecast drivers, peer block and multiples."
            )

    facts_sha = getattr(model, "facts_sha", None) or (model or {}).get("input_facts_sha256")
    candidate_id = assumptions.candidate_id or getattr(model, "candidate_id", None)
    payload = {
        "artifact": "valuation",
        "engine": ENGINE_NAME,
        "input_facts_sha256": facts_sha,
        "candidate_id": candidate_id,
        "cutoff": assumptions.cutoff,
        "assumptions": {
            "wacc": w, "terminal_growth": g,
            "exit_multiple_ebitda": assumptions.exit_multiple_ebitda,
            "discount_convention": conv,
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


def _resolve_base(model: "Model | dict") -> tuple[str, dict[str, float]]:
    """(period, base-dict) anchored on the latest annual period.

    Base flows: latest FY if present; else latest quarter (flows
    annualized x4, margins used as-is). Balance items (net debt, shares)
    come from the latest period of any kind."""
    rev_series = _series_values(model, "income_statement.revenue")
    fy = [p for p in rev_series if p.startswith("FY")]
    if fy:
        p = max(fy, key=_period_key)
        ann = 1.0
    else:
        if not rev_series:
            raise MissingFactError("revenue missing from the model; cannot value")
        p = _latest_period(rev_series)
        ann = 4.0
    revenue = rev_series[p] * ann if not p.startswith("FY") else rev_series[p]
    gm = _series_values(model, "derived.gross_margin").get(p)
    em = _series_values(model, "derived.ebitda_margin").get(p)
    fcf = _series_values(model, "derived.fcf").get(p)
    if fcf is None:
        ocf = _series_values(model, "cash_flow.operating_cash_flow").get(p)
        capex = _series_values(model, "cash_flow.capital_expenditure").get(p)
        if ocf is None or capex is None:
            fcf = None
        else:
            fcf = ocf - capex
    if fcf is not None and not p.startswith("FY"):
        fcf = fcf * ann
    net_debt = _latest(_series_values(model, "derived.net_debt"))
    nd = net_debt[1] if net_debt else None
    if nd is None:
        st = _latest(_series_values(model, "balance_sheet.short_term_debt"))
        lt = _latest(_series_values(model, "balance_sheet.long_term_debt"))
        cash = _latest(_series_values(model, "balance_sheet.cash_and_equivalents"))
        if None not in (st, lt, cash):
            nd = st[1] + lt[1] - cash[1]
    shares = _latest(_series_values(model, "income_statement.diluted_shares_outstanding"))
    ni_series = _series_values(model, "income_statement.net_income")
    if p.startswith("FY"):
        net_income = ni_series.get(p)
    else:
        net_income = ni_series.get(p) * ann if p in ni_series else None
    return p, {
        "revenue": revenue,
        "gross_margin": gm,
        "ebitda_margin": em,
        "fcf": fcf,
        "net_debt": nd,
        "shares": shares[1] if shares else None,
        "net_income": net_income,
    }


def _blended_growth(model, forecast: Forecast, base_period: str) -> list[float]:
    """Revenue-weighted blend of per-segment growth on the base-period mix."""
    seg_rev = {}
    for seg in (("ccg", "dcai", "nex", "foundry", "altera", "mobileye")):
        vals = _series_values(model, f"segments.{seg}.revenue")
        if base_period in vals:
            seg_rev[seg] = vals[base_period]
    total = sum(v for v in seg_rev.values() if v > 0) or None
    growth_map = dict(forecast.segments)
    blended = []
    for t in range(forecast.horizon_years):
        if total and seg_rev:
            num = sum(max(seg_rev.get(s, 0.0), 0.0) * growth_map.get(s, (0.0,) * forecast.horizon_years)[t]
                      for s in seg_rev)
            blended.append(num / total)
        else:
            g = growth_map.get("total")
            if g is None:
                raise MissingFactError(
                    "no segment revenue in the model and no 'total' growth in the forecast")
            blended.append(g[t])
    return blended


def _exponents(horizon: int, convention: str) -> list[float]:
    if convention in ("year_end", "year-end"):
        return [float(t) for t in range(1, horizon + 1)]
    if convention in ("mid_year", "mid-year"):
        return [t - 0.5 for t in range(1, horizon + 1)]
    raise BoundsViolation(f"unknown discount convention {convention!r}")


def _relative_valuation(model, assumptions: Assumptions, base_period, base,
                        net_debt, shares) -> dict:
    """Peer multiples applied to own base metrics; own historical band."""
    peers = assumptions.peer_block.get("peers", [])
    ev_ebitda = [float(p["ev_ebitda"]) for p in peers if p.get("ev_ebitda") is not None]
    p_e = [float(p["p_e_ttm"]) for p in peers if p.get("p_e_ttm") is not None]
    p_s = [float(p["p_s"]) for p in peers if p.get("p_s") is not None]

    ebitda = base["revenue"] * base["ebitda_margin"] if base["ebitda_margin"] is not None else None
    ni = base.get("net_income")

    applied: dict[str, dict] = {}
    if ev_ebitda and ebitda is not None:
        med = statistics.median(ev_ebitda)
        applied["ev_ebitda"] = {
            "peer_median": med, "own_metric": ebitda,
            "implied_ev": med * ebitda,
            "implied_price": (med * ebitda - net_debt) / shares,
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
            "peer_median": med, "own_metric": base["revenue"],
            "implied_price": base["revenue"] / shares * med,
        }
    prices = [a["implied_price"] for a in applied.values()]
    comps_price = statistics.median(prices) if prices else None
    comps_upside = comps_price / assumptions.market_price - 1.0 \
        if comps_price is not None and assumptions.market_price else None

    band_out: dict[str, dict] = {}
    if assumptions.own_band:
        for key, lo, hi, compute in (
            ("ev_ebitda", *assumptions.own_band["ev_ebitda"], (lambda m: (m * ebitda - net_debt) / shares)),
            ("p_e", *assumptions.own_band["p_e"], (lambda m: ni / shares * m)),
            ("p_s", *assumptions.own_band["p_s"], (lambda m: base["revenue"] / shares * m)),
        ):
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


def _sensitivity_grid(years, pv_sum, assumptions: Assumptions, net_debt, shares) -> dict:
    wacc_points, g_points = assumptions.sensitivity_points
    fcf_final = years[-1]["fcf"]
    n = len(years)
    grid = []
    for w in wacc_points:
        row = []
        for g in g_points:
            if g >= w:  # Gordon undefined; mark cell as None
                row.append(None)
                continue
            pv_flows = sum(y["fcf"] / (1.0 + w) ** (t + 1) for t, y in enumerate(years))
            tv = fcf_final * (1.0 + g) / (w - g)
            ev = pv_flows + tv / (1.0 + w) ** n
            row.append((ev - net_debt) / shares)
        grid.append(row)
    return {
        "basis": "gordon",
        "wacc_points": wacc_points,
        "terminal_growth_points": g_points,
        "grid": grid,
    }
