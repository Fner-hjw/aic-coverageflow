--version: 1.4
--role: extractor
--produces: facts.json
--read: statement slices (with citation anchors) from the scoped filings
--references: accounting-definitions.md

# CONTRACT: Extraction

## TASK
From each statement slice in the bound request, extract the canonical
line-item values for the requested period(s) as schema-bound facts. Field
names use the pinned accounting-definitions reference (e.g.
`income_statement.revenue`, `cash_flow.capital_expenditure`,
`segments.Data Center and AI.revenue` — quarterly segment facts use the
as-reported full segment names; the deterministic model engine translates
them onto the canonical segment ids via the versioned mapping table). Every
fact carries a citation naming the slice,
anchor heading, and a line locator.

## OUTPUT SCHEMA
Strict JSON only, matching **facts.json** (contracts §5):
`role("extractor"), request_id, candidate_id,
facts[] {field, period, value, unit("USDmm" | "USD" | "shares_mm" | "ratio"),
  confidence (0–1), citation {doc_id, slice_id, anchor, locator}},
missing[] {field, reason}, notes[], conformance("schema 1.0"), prompt_version("1.4")`.
`period` format: `YYYY-QN`, `YYYY-QN-YTD`, or `YYYY`.

**Columns.** Extract every column a statement prints, each under the label
that matches its true span. A 10-Q income statement prints a three-month and
a nine-month column: the three-month figures are `2026-Q3`, the nine-month
figures are `2026-Q3-YTD`. A 10-Q cash flow statement prints only the
nine-month column, so every cash-flow fact is `2026-Q3-YTD`. Never label a
year-to-date column `YYYY` — that is the annual period, and a line item that
only appears year-to-date is a `YYYY-QN-YTD` fact, not a `missing` one.

**Annual columns.** A 10-K prints fiscal years ("Years Ended Dec 27, 2025 |
Dec 28, 2024 | Dec 30, 2023"). Those are `2025`, `2024`, `2023` — the `YYYY`
form, one fact per column. The request's slices may come from more than one
filing: extract the annual columns from the 10-K slices and the quarterly and
year-to-date columns from the 10-Q slices, all into the same `facts[]`. A
field may therefore carry both `2025` and `2026-Q3` facts; that is expected,
not a duplicate.

**Attribution and non-operating lines.**
- File "Net income attributable to <issuer>" under
  `income_statement.net_income_attributable` (and also `income_statement.net_income`),
  and the non-controlling interests' share of net income under
  `income_statement.net_income_noncontrolling`. On the balance sheet file the
  non-controlling interests line under `balance_sheet.noncontrolling_interests`.
- A line captioned "Interest and other, net" goes under
  `income_statement.interest_and_other_net`. It must NOT be filed under
  `income_statement.interest_expense`: that field is only for a line that is
  interest expense. If the filing prints no separate interest expense line,
  leave `interest_expense` in `missing`.

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
 "missing":[{"field":"income_statement.other_operating_income_expenses",
   "reason":"not disclosed in the IS slice"}],
 "notes":["figures are one period only; comparatives in adjacent columns"],
 "conformance":"schema 1.0","prompt_version":"1.4"}
