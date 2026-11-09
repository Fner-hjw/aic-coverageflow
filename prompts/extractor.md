--version: 1.0
--role: extractor
--produces: facts.json
--read: statement slices (with citation anchors) for one period
--references: accounting-definitions.md

# CONTRACT: Extraction

## TASK
From each statement slice in the bound request, extract the canonical
line-item values for the requested period(s) as schema-bound facts. Field
names use the pinned accounting-definitions reference (e.g.
`income_statement.revenue`, `cash_flow.ppe_additions`,
`segment.DCAI.revenue`). Every fact carries a citation naming the slice,
anchor heading, and a line locator.

## OUTPUT SCHEMA
Strict JSON only, matching **facts.json** (contracts §5):
`role("extractor"), request_id, candidate_id,
facts[] {field, period, value, unit("USDmm" | "USD" | "shares_mm" | "ratio"),
  confidence (0–1), citation {doc_id, slice_id, anchor, locator}},
missing[] {field, reason}, notes[], conformance("schema 1.0"), prompt_version("1.0")`.
`period` format: `YYYY-QN` or `YYYY`.

## BOUNDS
- You see slices only. Never consult other parts of the filing, prior
  rounds, or outside knowledge.
- A locatable, verifiable fact per line item or entry in `missing` with a
  reason. Never invent, estimate, interpolate, or fill from memory.
- Value = the number as reported for that period, already in the requested
  unit (no unit arithmetic beyond what the slice states).
- Confidence is your locator clarity (1.0 = bold row line), not a guess.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (every fact cited to a slice in the request) ∧
(every canonical field either a fact or a `missing` entry) ∧ (no duplicate
field+period).

## HARNESS ENFORCED
Write scope: facts.json only. No ingest, no model. Uncited numbers fail the
schema check at dispatch and return as repairable errors. Budgets enforced.

## WORKED MICRO-EXAMPLE (toy)
{"role":"extractor","request_id":"req-0043","candidate_id":"cand-77",
 "facts":[{"field":"income_statement.revenue","period":"2026-Q3","value":210.0,
   "unit":"USDmm","confidence":0.99,
   "citation":{"doc_id":"toy-10q-2026q3","slice_id":"toy-10q-2026q3/IS",
     "anchor":"Consolidated Statements of Operations","locator":"line 14"}}],
 "missing":[{"field":"income_statement.restructuring_charges",
   "reason":"not disclosed in the IS slice"}],
 "notes":["figures are one period only; comparatives in adjacent columns"],
 "conformance":"schema 1.0","prompt_version":"1.0"}
