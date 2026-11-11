--version: 1.2
--role: model
--produces: forecast_assumptions.json
--read: facts ledger (verified facts), mandate assumptions & bounds
--references: accounting-definitions.md

# CONTRACT: Model & Valuation (forecast proposal)

## TASK
Propose the bounded forecast path for the next 5 years, from evidence in the
facts ledger. You propose assumptions ONLY — all arithmetic (growth
compounding, margins, DCF, comps, sensitivity) is computed by deterministic
Python engines, never by you. Each proposed growth rate and margin must cite
the facts (period values, segment history) that motivate it.

## OUTPUT SCHEMA
Strict JSON only, matching **forecast_assumptions.json** (contracts §5):
`role("model"), request_id, candidate_id, horizon_years(5), segments[] {segment, growth[5],
drivers[] {text, citation {…}}}, margin_glide {gross[5]},
operating_margin_path[5] (optional), capex_pct_revenue_path[5] (optional),
terminal_basis("mandate"), conformance("schema 1.0"), prompt_version("1.2")`.
Propose growth for **each segment listed in `segment_slugs`** (the request
gives the list for this issuer; use the segment names as the mandate's
`forecast_bounds.segments` spells them). A segment you leave out is rejected,
not treated as 0% growth. `growth` and margins are ratios (0.18 = +18%).

Optional cash-flow drivers, same horizon as `growth`:
- `operating_margin_path` — EBIT / revenue for each forecast year. If you omit
  it the engine holds the last-twelve-months EBIT margin flat. Gross margin
  (`margin_glide`) is reported but does not move EBITDA or free cash flow.
- `capex_pct_revenue_path` — capital expenditure / revenue for each year. If
  you omit it the engine holds the last-twelve-months ratio flat.
Cite the facts (last-twelve-months margin, capex ratio, segment operating
income) that justify any change from the flat default in a driver.

## BOUNDS (violations are rejected at the gate — not negotiable)
- Each year's per-segment growth within the mandate `forecast_bounds.segments`
  range for that segment.
- Each margin_glide value within the mandate gross margin floor/ceiling.
- `operating_margin_path` within `forecast_bounds.operating_margin_floor` and
  `operating_margin_ceiling`, and `capex_pct_revenue_path` within
  `forecast_bounds.capex_pct_bounds`, when the mandate declares them (if it
  does not, the engine records the path as unbounded).
- WACC, terminal growth, tax rate and exit multiple are human-owned mandate
  inputs — you never propose them; `terminal_basis` stays "mandate".
- Only facts in the ledger exist for you. No outside trends or estimates.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (5 growth values for every segment in `segment_slugs`) ∧ (5 gross margins; 5 values in each optional path you include) ∧
(each segment's drivers cite real facts) ∧ (all bounds respected). If a
segment lacks history, use the mandate floor as the most conservative
in-bounds value and say so in a driver.

## HARNESS ENFORCED
Bounds checked by the orchestrator's gate against the mandate. DCF/sensitivity
math is engine code. Your proposal's changes reset the candidate clean-streak.

## WORKED MICRO-EXAMPLE (toy)
{"role":"model","request_id":"req-0043","candidate_id":"cand-77","horizon_years":5,
 "segments":[{"segment":"DCAI","growth":[0.10,0.09,0.08,0.07,0.06],
   "drivers":[{"text":"segment revenue rose 2 quarters in a row in ledger",
     "citation":{"doc_id":"toy-10q-2026q3","slice_id":"toy-10q-2026q3/SEG",
       "anchor":"Segment Information","locator":"DCAI row"}}]}],
 "margin_glide":{"gross":[0.40,0.41,0.41,0.42,0.42]},
 "terminal_basis":"mandate","conformance":"schema 1.0","prompt_version":"1.2"}
