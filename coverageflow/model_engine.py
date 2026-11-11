"""Deterministic financial-model engine.

Owner stream: A1 (runtime & data).

Contract (docs/contracts/interfaces.md section 4)::

    def build_model(facts: Facts, mapping_version: str) -> Model

Turns schema-bound extraction facts into the consistent canonical time
series (design-decisions section 4): applies the versioned segment mapping
tables (references/segment-mapping-v*.json are DATA; this module interprets
them), derives growth (YoY/QoQ), margins, FCF (= OCF - capex), net debt,
EBITDA and EPS linkage, enforces coherence with typed errors
(MissingFactError, BoundsViolation), and emits the versioned model.json
artifact per docs/contracts/state-and-artifacts.md section 5. Pure Python,
no LLM, no I/O beyond the reference mapping files (path explicitly
resolvable and overridable).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ENGINE_NAME = "model_engine@1.0"
_SEGMENT_FIELD_RE = re.compile(r"^segments\.([A-Za-z0-9 &_'-]+?)\.(revenue|operating_income)$")
_Q_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_FY_RE = re.compile(r"^FY(\d{4})$")


class ModelEngineError(Exception):
    """Base class for model-engine errors."""


class MissingFactError(ModelEngineError):
    """A field required for the build is absent from the facts."""


class BoundsViolation(ModelEngineError):
    """A derived value or coherence check violates its bound."""


# --------------------------------------------------------------------- facts


@dataclass(frozen=True)
class Fact:
    field: str
    period: str
    value: float
    unit: str = "USDmm"
    citation: dict | None = None


def _normalize_period(raw: str) -> str:
    p = str(raw).strip().upper().replace(" ", "")
    m = _Q_RE.match(p)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}"
    if re.match(r"^\d{4}Q[1-4]$", p):
        return f"{p[:4]}-Q{p[5]}"
    m = re.match(r"^(\d{4})-?Q([1-4])-?YTD$", p)
    if m:
        return f"{m.group(1)}-Q{m.group(2)}-YTD"
    if p.startswith("FY") and len(p) >= 6:
        return f"FY{p[2:6]}"
    if re.match(r"^\d{4}$", p):
        # the reference allows a bare year for an annual period; normalise it
        # to the FY form so every downstream consumer sees one spelling
        return f"FY{p}"
    return p


def _infer_ticker(facts: tuple["Fact", ...]) -> str | None:
    """Ticker from the citations' doc ids (``intc-10q-2026q3`` -> ``intc``).

    Bundle documents are named ``<ticker>-<form>-<period>``; when every cited
    document agrees on the prefix, that is the issuer. Mixed or absent
    citations give None (default mapping).
    """
    prefixes = {str(f.citation["doc_id"]).split("-")[0].lower()
                for f in facts
                if isinstance(f.citation, dict) and f.citation.get("doc_id")}
    return prefixes.pop() if len(prefixes) == 1 else None


@dataclass(frozen=True)
class Facts:
    facts: tuple[Fact, ...]
    candidate_id: str | None = None
    request_id: str | None = None
    ticker: str | None = None

    @classmethod
    def from_payload(cls, payload: dict | list | None) -> "Facts":
        if isinstance(payload, list):
            payload = {"facts": payload}
        if not isinstance(payload, dict) or "facts" not in payload:
            raise ModelEngineError("facts payload must be a facts.json dict or a list")
        facts = tuple(
            Fact(
                field=str(f["field"]),
                period=_normalize_period(f["period"]),
                value=float(f["value"]),
                unit=str(f.get("unit", "USDmm")),
                citation=f.get("citation"),
            )
            for f in sorted(payload["facts"], key=lambda x: (str(x["field"]), str(x["period"])))
        )
        return cls(
            facts=facts,
            candidate_id=payload.get("candidate_id"),
            request_id=payload.get("request_id"),
            ticker=payload.get("ticker") or _infer_ticker(facts),
        )

    @property
    def sha256(self) -> str:
        """Stable hash over the canonical fact list (input_facts_sha256)."""
        canon = json.dumps(
            [{"field": f.field, "period": f.period, "value": f.value} for f in self.facts],
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# -------------------------------------------------------------------- model


# The formula behind each derived series, published in model.json so the
# figure is reproducible from the artifact rather than from prose.
DERIVATIONS = {
    "derived.net_debt": ("balance_sheet.short_term_debt + "
                         "balance_sheet.long_term_debt - "
                         "balance_sheet.cash_and_equivalents - "
                         "balance_sheet.short_term_investments"),
    "derived.fcf": "cash_flow.operating_cash_flow - cash_flow.capital_expenditure",
    "income_statement.ebitda": ("income_statement.operating_income + "
                                "cash_flow.depreciation_amortization"),
    "derived.gross_margin": "income_statement.gross_profit / income_statement.revenue",
    "derived.operating_margin": ("income_statement.operating_income / "
                                 "income_statement.revenue"),
    "derived.ebitda_margin": "income_statement.ebitda / income_statement.revenue",
    "derived.net_margin": "income_statement.net_income / income_statement.revenue",
    "segments.sum_revenue": "sum of canonical segment revenues",
    "segments.reconciling_difference": ("income_statement.revenue - "
                                        "segments.sum_revenue"),
    "segments.<slug>.revenue_share_of_total": ("segments.<slug>.revenue / "
                                               "segments.sum_revenue"),
}


@dataclass
class Model:
    series: dict[str, dict[str, float]] = field(default_factory=dict)
    yoy: dict[str, dict[str, float]] = field(default_factory=dict)
    qoq: dict[str, dict[str, float]] = field(default_factory=dict)
    # growth cells that exist but are not meaningful (zero or negative base):
    # {field: {"yoy": {period: "n/m"}, "qoq": {period: "n/m"}}}. The value is
    # absent from yoy/qoq (the engine's representation of "no value"); this
    # map says WHY, so a reader can tell n/m from "no base period".
    growth_status: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    checks: dict[str, dict[str, str]] = field(default_factory=dict)
    facts_sha: str = ""
    candidate_id: str | None = None
    cutoff: str | None = None
    mapping_version: str = "1.0"
    # canonical segment slugs of this issuer, from its mapping table
    segment_slugs: list[str] = field(default_factory=list)
    mapping_file: str | None = None
    # period label -> period-end date (ISO). Not derivable from facts; the
    # orchestrator fills it from the input manifest (period_ends_from_manifest).
    period_ends: dict[str, str] = field(default_factory=dict)

    @property
    def periods(self) -> list[str]:
        seen: dict[str, None] = {}
        for p2v in self.series.values():
            for p in p2v:
                seen.setdefault(p, None)
        return sorted(seen)

    def value(self, fld: str, period: str) -> float | None:
        return self.series.get(fld, {}).get(period)

    def to_dict(self) -> dict:
        series_view: dict[str, dict] = {}
        for fld in self.series:
            entry = dict(self.series[fld])
            entry["yoy"] = dict(self.yoy.get(fld, {}))
            entry["qoq"] = dict(self.qoq.get(fld, {}))
            entry["growth_status"] = {k: dict(v) for k, v in self.growth_status.get(fld, {}).items()}
            entry["yoy_computed"] = bool(self.yoy.get(fld) or self.qoq.get(fld))
            series_view[fld] = entry
        return {
            "artifact": "model",
            "engine": ENGINE_NAME,
            "input_facts_sha256": self.facts_sha,
            "candidate_id": self.candidate_id,
            "cutoff": self.cutoff,
            "series": series_view,
            "mapping_tables": self.mapping_version,
            "mapping_file": self.mapping_file,
            "segment_slugs": list(self.segment_slugs),
            "period_ends": dict(self.period_ends),
            "checks": self.checks,
            # Every derived series states the formula the engine actually
            # applied. Without this the only description of net debt was the
            # extractor's prose note, which went stale the moment the engine
            # started netting short-term investments -- and a critic then
            # "reproduced" the figure from the stale formula and found it
            # wrong.
            "derivations": dict(DERIVATIONS),
        }


# ------------------------------------------------------------------ mapping


def _default_mapping_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "references"


def slugify(name: str) -> str:
    """Canonical segment slug: lower case, non-alphanumerics collapsed to _."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _mapping_path(n: str, mapping_dir: Path | None, ticker: str | None) -> Path:
    base = mapping_dir or _default_mapping_dir()
    if ticker:
        specific = base / f"segment-mapping-{ticker.lower()}-v{n}.json"
        if specific.is_file():
            return specific
    return base / f"segment-mapping-v{n}.json"


def load_mapping(mapping_version: str, mapping_dir: Path | None = None,
                 ticker: str | None = None) -> dict:
    """Mapping table for *ticker* (``segment-mapping-<ticker>-v<N>.json``),
    else the default ``segment-mapping-v<N>.json``."""
    m = re.match(r"v?(\d+)", str(mapping_version).strip().lower())
    if not m:
        raise ModelEngineError(f"uninterpretable mapping version: {mapping_version!r}")
    path = _mapping_path(m.group(1), mapping_dir, ticker)
    if not path.is_file():
        raise ModelEngineError(f"segment mapping table not found: {path}")
    data = json.loads(path.read_text("utf-8"))
    data["_file"] = path.name
    return data


def mapping_segment_slugs(mapping: dict) -> list[str]:
    """Canonical segment slugs declared by the mapping table."""
    return [slugify(n) for n in
            (mapping.get("canonical_series") or {}).get("segments", [])]


def _alias_table(mapping: dict) -> dict[str, str]:
    """Lower-cased as-reported name -> slug, from the table's ``aliases``
    block plus every direct rule and the canonical names themselves."""
    out: dict[str, str] = {}
    for n in (mapping.get("canonical_series") or {}).get("segments", []):
        out[str(n).strip().lower()] = slugify(n)
    for name, canon in (mapping.get("aliases") or {}).items():
        out[str(name).strip().lower()] = slugify(canon)
    return out


def period_ends_from_manifest(manifest: dict) -> dict[str, str]:
    """Period label -> period-end date from an input manifest.

    10-K ``...-fy2025`` -> ``FY2025``; 10-Q ``...-2026q3`` -> ``2026-Q3`` and
    ``2026-Q3-YTD`` (a 10-Q's year-to-date columns end on the same date).
    """
    out: dict[str, str] = {}
    for d in (manifest or {}).get("documents", []):
        end, doc_id = d.get("period"), str(d.get("doc_id", "")).lower()
        if not end:
            continue
        m = re.search(r"-10k-fy(\d{4})$", doc_id)
        if m:
            out[f"FY{m.group(1)}"] = end
            continue
        m = re.search(r"-10q-(\d{4})q([1-4])$", doc_id)
        if m:
            out[f"{m.group(1)}-Q{m.group(2)}"] = end
            out[f"{m.group(1)}-Q{m.group(2)}-YTD"] = end
    return out


def _rule_index(mapping: dict) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Per-period as_reported->canonical alias table + global fallback alias."""
    per_period: dict[str, dict[str, str]] = {}
    global_alias: dict[str, str] = {}
    for period, entry in mapping.get("periods", {}).items():
        table: dict[str, str] = {}
        for rule in entry.get("rules", []):
            name, canon = rule.get("as_reported"), rule.get("canonical")
            if name is None:
                continue
            if isinstance(canon, str) and rule.get("op") in (None, "direct"):
                table[name] = canon
                global_alias.setdefault(name, canon)
        per_period[period] = table
    return per_period, global_alias


def _canonical_segment_field(name: str, measure: str, period: str,
                             per_period: dict, global_alias: dict,
                             slugs: list[str], aliases: dict[str, str]) -> str:
    norm = name.strip()
    if norm.lower() in slugs:
        return f"segments.{norm.lower()}.{measure}"
    if "all other" in norm.lower():
        return f"segments.all_other_{measure}"
    canon = per_period.get(period, {}).get(norm) or global_alias.get(norm)
    if canon:
        return f"segments.{canon}.{measure}"
    # the filing's own name for a segment the mapping already knows
    alias = aliases.get(norm.lower())
    if alias:
        return f"segments.{alias}.{measure}"
    return f"segments.{norm}.{measure}"  # leave untouched (visible, not canonical)


# ------------------------------------------------------------------- engine

_EPS_ABS_TOL = 0.01   # per share
_EPS_REL_TOL = 0.005  # 0.5% of reported EPS
_ABS_TOL = 0.5

# Net income attributable to the company's shareholders is the numerator of
# EPS. Consolidated net income includes non-controlling interests and does
# not tie. Accepted keys, in priority order; else attributable = NI - NCI.
NI_ATTRIBUTABLE_KEYS = (
    "income_statement.net_income_attributable",
    "income_statement.net_income_attributable_to_parent",
    "income_statement.net_income_attributable_to_company",
)
NCI_KEYS = (
    "income_statement.net_income_noncontrolling",
    "income_statement.net_income_attributable_to_nci",
    "income_statement.noncontrolling_interest",
)


def _eps_tolerance(reported: float) -> float:
    """Tight EPS tie tolerance: the larger of 0.01/share and 0.5% of EPS."""
    return max(_EPS_ABS_TOL, _EPS_REL_TOL * abs(reported))


def _attributable_net_income(model: "Model", p: str) -> float | None:
    for k in NI_ATTRIBUTABLE_KEYS:
        v = model.series.get(k, {}).get(p)
        if v is not None:
            return v
    ni = model.series.get("income_statement.net_income", {}).get(p)
    if ni is not None:
        for k in NCI_KEYS:
            nci = model.series.get(k, {}).get(p)
            if nci is not None:
                return ni - nci
    return None


def build_model(
    facts: Facts | dict | list,
    mapping_version: str,
    *,
    candidate_id: str | None = None,
    cutoff: str | None = None,
    required_fields: list[str] | None = None,
    mapping_dir: Path | None = None,
    absolute_tol: float = _ABS_TOL,
    ticker: str | None = None,
    period_ends: dict[str, str] | None = None,
) -> Model:
    """Build the consistent canonical series from facts. Deterministic, pure.

    Raises MissingFactError when a required field has no facts at all, and
    BoundsViolation when a coherence/sanity bound is violated (EPS linkage,
    gross-profit reconciliation, segment revenue reconciliation, negative
    revenue/capex, non-positive share counts).
    """
    if not isinstance(facts, Facts):
        facts = Facts.from_payload(facts)
    if not facts.facts:
        raise MissingFactError("facts payload is empty")

    ticker = ticker or facts.ticker
    mapping = load_mapping(mapping_version, mapping_dir, ticker) if mapping_version else {}
    per_period, global_alias = _rule_index(mapping)
    slugs = mapping_segment_slugs(mapping)
    aliases = _alias_table(mapping)

    series: dict[str, dict[str, float]] = {}
    for f in facts.facts:
        fld = f.field
        m = _SEGMENT_FIELD_RE.match(fld)
        if m:
            fld = _canonical_segment_field(m.group(1), m.group(2), f.period,
                                           per_period, global_alias, slugs, aliases)
        series.setdefault(fld, {})[f.period] = f.value

    # ---- required fields -------------------------------------------------
    present = set(series)
    for req in required_fields or []:
        if req not in present:
            raise MissingFactError(f"missing required fact field: {req}")

    model = Model(
        series=series,
        facts_sha=facts.sha256,
        candidate_id=candidate_id or facts.candidate_id,
        cutoff=cutoff,
        mapping_version=str(mapping_version),
        segment_slugs=slugs,
        mapping_file=mapping.get("_file"),
        period_ends=dict(period_ends or {}),
    )

    _sanity_bounds(model)
    _reconciliation_checks(model, absolute_tol)
    _derive_levels(model)
    _derive_margins(model)
    _derive_growth(model)
    return model


# ----------------------------------------------------------- checks/bounds


def _sanity_bounds(model: Model) -> None:
    for f in ("income_statement.revenue",):
        for p, v in model.series.get(f, {}).items():
            if v < 0:
                raise BoundsViolation(f"negative revenue in {p}: {v}")
    for p, v in model.series.get("cash_flow.capital_expenditure", {}).items():
        if v < 0:
            raise BoundsViolation(f"negative capex in {p}: {v} (sign convention: positive = spent)")
    for p, v in model.series.get("income_statement.diluted_shares_outstanding", {}).items():
        if v <= 0:
            raise BoundsViolation(f"non-positive diluted share count in {p}: {v}")


def _reconciliation_checks(model: Model, tol: float) -> None:
    eps_check: dict[str, str] = {}
    gp_check: dict[str, str] = {}
    seg_check: dict[str, str] = {}
    revenue = model.series.get("income_statement.revenue", {})
    for p in model.periods:
        ni = _attributable_net_income(model, p)
        sh = model.series.get("income_statement.diluted_shares_outstanding", {}).get(p)
        eps = model.series.get("income_statement.diluted_eps", {}).get(p)
        if eps is not None and sh is not None:
            if ni is None:
                # cannot tie EPS without attributable NI; never report "ok"
                eps_check[p] = "not_computable"
            else:
                implied = ni / sh
                if abs(implied - eps) <= _eps_tolerance(eps):
                    eps_check[p] = "ok"
                else:
                    raise BoundsViolation(
                        f"EPS linkage violation in {p}: net income attributable {ni} / "
                        f"shares {sh} = {implied:.4f} but reported diluted EPS is {eps}"
                    )
        rev = revenue.get(p)
        cogs = model.series.get("income_statement.cost_of_revenue", {}).get(p)
        gp = model.series.get("income_statement.gross_profit", {}).get(p)
        if None not in (rev, cogs, gp):
            if abs((rev - cogs) - gp) <= tol:
                gp_check[p] = "ok"
            else:
                raise BoundsViolation(
                    f"Gross profit reconciliation violation in {p}: revenue {rev} - "
                    f"cost of revenue {cogs} = {rev - cogs} but reported gross profit is {gp}"
                )
        if rev is not None:
            seg_sum = sum(
                model.series.get(f"segments.{s}.revenue", {}).get(p, 0.0)
                for s in model.segment_slugs
            )
            have_all = all(p in model.series.get(f"segments.{s}.revenue", {}) for s in model.segment_slugs)
            residual = model.series.get("segments.all_other_revenue", {}).get(p, 0.0)
            if have_all and abs(seg_sum + residual - rev) <= tol:
                seg_check[p] = "ok"
            elif have_all:
                raise BoundsViolation(
                    f"Segment revenue reconciliation violation in {p}: canonical segments "
                    f"sum {seg_sum} + residual {residual} = {seg_sum + residual} but revenue is {rev}"
                )
    model.checks.update(
        {
            "eps_linkage": eps_check,
            "gross_profit_reconciliation": gp_check,
            "segment_revenue_reconciliation": seg_check,
        }
    )


# ----------------------------------------------------------- derivations


def _derive_levels(model: Model) -> None:
    # EBITDA = operating income + D&A; FCF = OCF - capex; net debt
    for p in model.periods:
        oi = model.series.get("income_statement.operating_income", {}).get(p)
        # D&A is reported once, in the cash flow statement; the income-statement
        # name is an alias. Accept either so a filing that only carries the
        # cash-flow caption still yields EBITDA.
        da = model.series.get("income_statement.depreciation_and_amortization",
                              {}).get(p)
        if da is None:
            da = model.series.get("cash_flow.depreciation_amortization",
                                  {}).get(p)
        if None not in (oi, da):
            model.series.setdefault("income_statement.ebitda", {})[p] = oi + da
        ocf = model.series.get("cash_flow.operating_cash_flow", {}).get(p)
        capex = model.series.get("cash_flow.capital_expenditure", {}).get(p)
        if None not in (ocf, capex):
            model.series.setdefault("derived.fcf", {})[p] = ocf - capex
        st = model.series.get("balance_sheet.short_term_debt", {}).get(p)
        lt = model.series.get("balance_sheet.long_term_debt", {}).get(p)
        cash = model.series.get("balance_sheet.cash_and_equivalents", {}).get(p)
        # Short-term investments are cash-like. A net-debt figure that nets
        # only cash overstates leverage for any issuer holding a large liquid
        # portfolio, and the equity bridge then understates value.
        sti = model.series.get("balance_sheet.short_term_investments", {}).get(p) or 0
        if None not in (st, lt, cash):
            model.series.setdefault("derived.net_debt", {})[p] = st + lt - cash - sti


def _derive_margins(model: Model) -> None:
    rev = model.series.get("income_statement.revenue", {})
    pairs = {
        "derived.gross_margin": "income_statement.gross_profit",
        "derived.operating_margin": "income_statement.operating_income",
        "derived.ebitda_margin": "income_statement.ebitda",
        "derived.net_margin": "income_statement.net_income",
    }
    for p, rv in rev.items():
        if rv == 0:
            continue
        for derived, src in pairs.items():
            v = model.series.get(src, {}).get(p)
            if v is not None:
                model.series.setdefault(derived, {})[p] = v / rv
        seg_sum = 0.0
        have_seg = False
        for s in model.segment_slugs:
            sr = model.series.get(f"segments.{s}.revenue", {}).get(p)
            if sr is not None:
                seg_sum += sr
                have_seg = True
        for s in model.segment_slugs:
            seg_rev = model.series.get(f"segments.{s}.revenue", {}).get(p)
            seg_oi = model.series.get(f"segments.{s}.operating_income", {}).get(p)
            if seg_rev:
                if seg_oi is not None:
                    model.series.setdefault(f"segments.{s}.operating_margin", {})[p] = seg_oi / seg_rev
                if seg_sum:
                    # share of the segment total, so shares sum to 100%
                    model.series.setdefault(f"segments.{s}.revenue_share_of_total", {})[p] = seg_rev / seg_sum
        if have_seg:
            model.series.setdefault("segments.sum_revenue", {})[p] = seg_sum
            # consolidated minus segment sum (intersegment eliminations /
            # "all other"), shown rather than absorbed into the shares
            model.series.setdefault("segments.reconciling_difference", {})[p] = rv - seg_sum


def _growth_base(period: str, kind: str) -> str | None:
    mq = _Q_RE.match(period)
    if mq:
        y, q = int(mq.group(1)), int(mq.group(2))
        if kind == "yoy":
            return f"{y - 1}-Q{q}"
        return f"{y - 1}-Q4" if q == 1 else f"{y}-Q{q - 1}"
    mf = _FY_RE.match(period)
    if mf:
        return f"FY{int(mf.group(1)) - 1}" if kind == "yoy" else None
    return None


def _derive_growth(model: Model) -> None:
    for fld, pv in list(model.series.items()):
        for p, cur in pv.items():
            for kind in ("yoy", "qoq"):
                base_p = _growth_base(p, kind)
                if base_p is None:
                    continue
                base = model.series.get(fld, {}).get(base_p)
                if base is None:
                    continue  # no base period: absent, not flagged
                if base <= 0:
                    # growth on a zero/negative base is not meaningful; leave
                    # the value absent (None) and record n/m
                    model.growth_status.setdefault(fld, {}).setdefault(kind, {})[p] = "n/m"
                    continue
                target = model.yoy if kind == "yoy" else model.qoq
                target.setdefault(fld, {})[p] = cur / base - 1.0
