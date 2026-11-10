--version: 1.1
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
terminal_basis("mandate"), conformance("schema 1.0"), prompt_version("1.1")`.
`segment` ∈ CCG, DCAI, NEX, Foundry, Altera, Mobileye (as provided in the
request's mandate blocks). `growth` and margins are ratios (0.18 = +18%).

## BOUNDS (violations are rejected at the gate — not negotiable)
- Each year's per-segment growth within the mandate `forecast_bounds.segments`
  range for that segment.
- Each margin_glide value within the mandate gross margin floor/ceiling.
- WACC and terminal growth are human-owned mandate inputs — you never
  propose them; `terminal_basis` stays "mandate".
- Only facts in the ledger exist for you. No outside trends or estimates.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (5 growth values per provided segment) ∧ (5 margins) ∧
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
 "terminal_basis":"mandate","conformance":"schema 1.0","prompt_version":"1.0"}
