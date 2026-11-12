--version: 1.7
--role: extractor
--produces: facts.json
--read: one statement slice (with its citation anchor) of one scoped filing
--references: accounting-definitions.md

# CONTRACT: Extraction

## TASK
The bound request is ONE extraction unit: one statement slice (`slice`, text
inlined) of one filing (`unit`), plus that statement's canonical `fields` and
per-field `field_notes`. Other statements and filings are extracted by other
units in parallel; the harness merges them. Extract every canonical line item
the slice prints, for every column it prints, as compact rows. Field names
follow the pinned accounting-definitions reference; segment facts use the
as-reported full segment names (`segments.Data Center and AI.revenue`), which
the model engine maps onto canonical ids.

## OUTPUT SCHEMA
Strict JSON only, no prose, no fences:
`role("extractor"), request_id, candidate_id, unit, rows[], missing[], notes[],
conformance("schema 1.0"), prompt_version("1.7")`. Echo `request_id`,
`candidate_id`, `unit` from the request. Each `rows` entry is ONE string:

`field|period|value|unit|locator`

- `field`: an id from `fields`; segments:
  `segments.<name as reported>.revenue|operating_income` and the residuals
  `segments.all_other_revenue` / `segments.all_other_operating_income`.
- `period`: `YYYY-QN`, `YYYY-QN-YTD`, or a fiscal year as `FYYYYY`.
- `value`: a plain number as printed, already in the unit (`13500`, `-650.5`):
  no separators, currency signs, parentheses or text.
- `unit`: `USDmm`, `USD` (per share), `shares_mm`, or `ratio`.
- `locator`: the row caption or line it was read from (last part, non-empty).

The harness knows the document, slice and anchor and expands each row into a
cited fact. A row that does not parse, names an unknown field, has a
non-numeric value or non-canonical period is rejected with feedback. One row
per field+period. `missing[]` is `{field, reason}` (add `period` if only one is
absent); `notes[]` are short strings.

**Columns.** Extract every printed column under its true span. A 10-Q income
statement prints three-month (`2026-Q3`) and nine-month (`2026-Q3-YTD`)
columns. A 10-Q cash flow prints only the nine-month column, so every
cash-flow row is `2026-Q3-YTD`. Never label a year-to-date column as a fiscal year. A
10-K prints fiscal years: `FY2025`, `FY2024`, `FY2023`, one row per column.

**Attribution and non-operating lines.**
- "Net income attributable to <issuer>" goes under
  `income_statement.net_income_attributable` (and `income_statement.net_income`);
  the non-controlling share under `income_statement.net_income_noncontrolling`;
  the balance sheet line under `balance_sheet.noncontrolling_interests`.
- "Interest and other, net" goes under `income_statement.interest_and_other_net`;
  it must NOT be filed under `income_statement.interest_expense`. With no separate interest expense
  line, leave `interest_expense` in `missing`.
- Cost of revenue: use the line that gross profit is computed from. When the
  statement prints component cost lines and a total (for example "Cost of
  sales", "Amortization of acquisition-related intangibles", "Total cost of
  sales"), `income_statement.cost_of_revenue` is the total line.
- Depreciation and amortization: one combined cash-flow line is
  `cash_flow.depreciation_amortization`; separate lines are
  `cash_flow.depreciation` and `cash_flow.amortization_of_intangibles`, never
  added up.

## BOUNDS
- Your slice only: no other units, prior rounds or outside knowledge.
- A locatable row per line item, else a `missing` entry with a reason. Never
  invent, estimate or interpolate.
- No arithmetic: no sums, differences or conversions; the value is the number
  as reported.

## COMPLETION CRITERIA
Schema-valid JSON; every row parses and names a canonical field; every field in
`fields` is a row or a `missing` entry; no duplicate field+period.

## HARNESS ENFORCED
Write scope: this unit's answer only. Malformed rows return as repairable
errors naming the row. Budgets enforced.

## WORKED MICRO-EXAMPLE (toy)
{"role":"extractor","request_id":"req-0043","candidate_id":null,
 "unit":"toy-10q-2026q3/income_statement",
 "rows":["income_statement.revenue|2026-Q3|210.0|USDmm|line 14",
         "income_statement.revenue|2026-Q3-YTD|610.5|USDmm|line 14",
         "income_statement.diluted_eps|2026-Q3|0.16|USD|line 31"],
 "missing":[{"field":"income_statement.other_operating_income_expenses",
   "reason":"not disclosed in the IS slice"}],
 "notes":["comparatives are in adjacent columns"],
 "conformance":"schema 1.0","prompt_version":"1.7"}
