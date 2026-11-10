# Accounting Definitions — canonical line-item names v1.1

> v1.1 changelog: regenerated verbatim from `references/model-schema.json` (schema v1.0, stream A1 — the authoritative 80-field schema). Ids here and ids there are now the same set; `tests/test_naming_parity.py` fails the build on any drift in either direction.

One canonical name per line. Extraction, model, and forecast artifacts use these names verbatim (coordinate with references/model-schema.json, which pins the same names — stream A1 owns that file; this file is its rendered projection). All values USD millions (USDmm) unless the field kind says otherwise: per-share fields are USD, share counts are millions, ratios are fractions. Filings are extracted "as reported"; versioned mapping tables (references/segment-mapping-v1.json) translate as-reported captions to these names.

## income_statement.* (Income Statement)

- `income_statement.revenue` — Revenue (USDmm)
- `income_statement.cost_of_revenue` — Cost of Revenue (USDmm)
- `income_statement.gross_profit` — Gross Profit (USDmm); derived: revenue - cost_of_revenue
- `income_statement.research_and_development` — Research And Development Expenses (USDmm)
- `income_statement.selling_general_and_administrative` — Selling General And Administrative Expenses (USDmm)
- `income_statement.other_operating_income_expenses` — Other Operating Income (Expenses) (USDmm)
- `income_statement.operating_expenses` — Operating Expenses (USDmm); derived: research_and_development + selling_general_and_administrative + other_operating_income_expenses
- `income_statement.ebitda` — EBITDA (USDmm); derived: operating_income + depreciation_and_amortization
- `income_statement.operating_income` — Operating Income (USDmm)
- `income_statement.depreciation_and_amortization` — Depreciation And Amortization (USDmm). Alias of `cash_flow.depreciation_amortization`: the figure is reported once, in the cash flow statement. Extract it under the cash-flow name and leave this one absent rather than duplicating the value.
- `income_statement.interest_expense` — Interest Expense (USDmm)
- `income_statement.other_non_operating_income` — Other Non-Operating Income (Expense), Net (USDmm)
- `income_statement.income_before_tax` — Pretax Income (USDmm)
- `income_statement.income_tax_expense` — Income Tax Expense (USDmm)
- `income_statement.net_income` — Net Income attributable to the parent (USDmm). Use the "net income attributable to <issuer>" line, NOT the pre-attribution "net income (loss)" total: it must tie to diluted EPS x diluted shares, and the non-controlling-interest line is excluded.
- `income_statement.basic_eps` — Basic EPS (USD)
- `income_statement.diluted_eps` — Diluted EPS (USD)
- `income_statement.diluted_shares_outstanding` — Weighted Average Diluted Shares (millions)

## balance_sheet.* (Balance Sheet)

- `balance_sheet.cash_and_equivalents` — Cash And Cash Equivalents (USDmm)
- `balance_sheet.short_term_investments` — Short-Term Investments (USDmm)
- `balance_sheet.accounts_receivable` — Accounts Receivable, Net (USDmm)
- `balance_sheet.inventories` — Inventories (USDmm)
- `balance_sheet.total_current_assets` — Total Current Assets (USDmm)
- `balance_sheet.property_plant_equipment_net` — Property Plant And Equipment, Net (USDmm)
- `balance_sheet.goodwill` — Goodwill (USDmm)
- `balance_sheet.intangible_assets_net` — Intangible Assets, Net (USDmm)
- `balance_sheet.deferred_tax_assets` — Deferred Tax Assets (USDmm)
- `balance_sheet.total_assets` — Total Assets (USDmm)
- `balance_sheet.accounts_payable` — Accounts Payable (USDmm)
- `balance_sheet.accrued_liabilities` — Accrued Liabilities (USDmm)
- `balance_sheet.short_term_debt` — Short-Term Debt (USDmm)
- `balance_sheet.total_current_liabilities` — Total Current Liabilities (USDmm)
- `balance_sheet.long_term_debt` — Long-Term Debt (USDmm)
- `balance_sheet.total_liabilities` — Total Liabilities (USDmm)
- `balance_sheet.common_stock` — Common Stock (USDmm)
- `balance_sheet.additional_paid_in_capital` — Additional Paid-In Capital (USDmm)
- `balance_sheet.treasury_stock` — Treasury Stock (USDmm)
- `balance_sheet.retained_earnings` — Retained Earnings (USDmm)
- `balance_sheet.accumulated_other_comprehensive_income` — Accumulated Other Comprehensive Income (USDmm)
- `balance_sheet.total_stockholders_equity` — Total Stockholders' Equity (USDmm)

## cash_flow.* (Cash Flow Statement)

- `cash_flow.net_income` — Net Income (USDmm)
- `cash_flow.depreciation_amortization` — Depreciation And Amortization (USDmm)
- `cash_flow.stock_based_compensation` — Stock-Based Compensation (USDmm)
- `cash_flow.deferred_taxes` — Deferred Taxes (USDmm)
- `cash_flow.working_capital_changes` — Change In Working Capital (USDmm)
- `cash_flow.operating_activities_other` — Other Operating Activities (USDmm)
- `cash_flow.operating_cash_flow` — Operating Cash Flow (USDmm)
- `cash_flow.capital_expenditure` — Capital Expenditure (USDmm); positive value = cash spent
- `cash_flow.investing_activities_other` — Other Investing Activities (USDmm)
- `cash_flow.investing_cash_flow` — Investing Cash Flow (USDmm)
- `cash_flow.dividend_paid` — Dividend Paid (USDmm); positive value = cash paid
- `cash_flow.share_issuance_repurchase` — Share Issuance And Repurchase, Net (USDmm)
- `cash_flow.debt_issuance_repayment` — Debt Issuance And Repayment, Net (USDmm)
- `cash_flow.financing_activities_other` — Other Financing Activities (USDmm)
- `cash_flow.financing_cash_flow` — Financing Cash Flow (USDmm)
- `cash_flow.cash_at_end_of_period` — Cash At End Of Period (USDmm)

## segments.* (Segments)

Canonical segments (schema names): CCG, DCAI, NEX, Foundry, Altera, Mobileye — canonical series ids (lowercase slugs): ccg, dcai, nex, foundry, altera, mobileye. forecast_bounds / mandate keys use the schema names (CASE-SENSITIVE); model series and facts use the lowercase ids below.

Quarterly facts use the AS-REPORTED full segment names from the filing (e.g. `segments.Client Computing Group.revenue`); the deterministic model engine translates them onto the canonical ids via references/segment-mapping-v1.json (as-reported captions are pinned there per period). FY/annual facts use the canonical ids directly. Where a period does not separately quantify a canonical segment, the residual is kept in the optional `segments.all_other_*` fields and the canonical series point stays absent (no interpolation, no carry-forward).

- `segments.ccg.revenue` — Revenue (CCG) (USDmm)
- `segments.dcai.revenue` — Revenue (DCAI) (USDmm)
- `segments.nex.revenue` — Revenue (NEX) (USDmm)
- `segments.foundry.revenue` — Revenue (Foundry) (USDmm)
- `segments.altera.revenue` — Revenue (Altera) (USDmm)
- `segments.mobileye.revenue` — Revenue (Mobileye) (USDmm)
- `segments.ccg.operating_income` — Operating Income (CCG) (USDmm)
- `segments.dcai.operating_income` — Operating Income (DCAI) (USDmm)
- `segments.nex.operating_income` — Operating Income (NEX) (USDmm)
- `segments.foundry.operating_income` — Operating Income (Foundry) (USDmm)
- `segments.altera.operating_income` — Operating Income (Altera) (USDmm)
- `segments.mobileye.operating_income` — Operating Income (Mobileye) (USDmm)
- `segments.ccg.operating_margin` — Operating Margin (CCG) (fraction); derived: operating_income / revenue
- `segments.dcai.operating_margin` — Operating Margin (DCAI) (fraction); derived: operating_income / revenue
- `segments.nex.operating_margin` — Operating Margin (NEX) (fraction); derived: operating_income / revenue
- `segments.foundry.operating_margin` — Operating Margin (Foundry) (fraction); derived: operating_income / revenue
- `segments.altera.operating_margin` — Operating Margin (Altera) (fraction); derived: operating_income / revenue
- `segments.mobileye.operating_margin` — Operating Margin (Mobileye) (fraction); derived: operating_income / revenue
- `segments.ccg.revenue_share_of_total` — Share of Total Revenue (CCG) (fraction); derived: revenue / income_statement.revenue
- `segments.dcai.revenue_share_of_total` — Share of Total Revenue (DCAI) (fraction); derived: revenue / income_statement.revenue
- `segments.nex.revenue_share_of_total` — Share of Total Revenue (NEX) (fraction); derived: revenue / income_statement.revenue
- `segments.foundry.revenue_share_of_total` — Share of Total Revenue (Foundry) (fraction); derived: revenue / income_statement.revenue
- `segments.altera.revenue_share_of_total` — Share of Total Revenue (Altera) (fraction); derived: revenue / income_statement.revenue
- `segments.mobileye.revenue_share_of_total` — Share of Total Revenue (Mobileye) (fraction); derived: revenue / income_statement.revenue
- `segments.all_other_revenue` — All Other Revenue (residual) (USDmm); optional residual
- `segments.all_other_operating_income` — All Other Operating Income (residual) (USDmm); optional residual

## derived.* (computed by the deterministic engines)

- `derived.gross_margin` — Gross Margin (fraction); expr: income_statement.gross_profit / income_statement.revenue
- `derived.operating_margin` — Operating Margin (fraction); expr: income_statement.operating_income / income_statement.revenue
- `derived.ebitda_margin` — EBITDA Margin (fraction); expr: income_statement.ebitda / income_statement.revenue
- `derived.net_margin` — Net Margin (fraction); expr: income_statement.net_income / income_statement.revenue
- `derived.fcf` — Free Cash Flow (USDmm); expr: cash_flow.operating_cash_flow - cash_flow.capital_expenditure
- `derived.net_debt` — Net Debt (USDmm); expr: balance_sheet.short_term_debt + balance_sheet.long_term_debt - balance_sheet.cash_and_equivalents
- `derived.revenue_yoy` — Revenue Growth YoY (fraction); expr: yoy(income_statement.revenue)
- `derived.revenue_qoq` — Revenue Growth QoQ (fraction); expr: qoq(income_statement.revenue)
- `derived.diluted_eps_yoy` — Diluted EPS Growth YoY (fraction); expr: yoy(income_statement.diluted_eps)
- `derived.net_income_yoy` — Net Income Growth YoY (fraction); expr: yoy(income_statement.net_income)

## period conventions

- Periods label as `YYYY-QN` (quarterly) or `FY2024`/annual `YYYY`.
- A column that covers more than the quarter it ends at is labelled `YYYY-QN-YTD` (e.g. the nine-month column of a Q3 10-Q is `2026-Q3-YTD`). Never label a year-to-date column `YYYY`: that is the annual period and the two are not interchangeable.
- Extract every column a statement prints, each under the label that matches its true span. A line item that only appears in a year-to-date column is a `YYYY-QN-YTD` fact, not a missing one.
- YoY = vs. same period prior year; QoQ = vs. prior quarter (A1 engine computes).
