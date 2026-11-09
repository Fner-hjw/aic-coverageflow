# Accounting Definitions — canonical line-item names v1.0

One canonical name per line. Extraction, model, and forecast artifacts use
these names verbatim (coordinate with references/model-schema.json, which
pins the same names — stream A1 owns that file). All values USD millions unless
the field is a ratio/share count. Filings are extracted "as reported";
versioned mapping tables translate as-reported captions to these names.

## income_statement.*
- income_statement.revenue — total net revenue (all segments + other)
- income_statement.cost_of_revenue — cost of sales
- income_statement.gross_profit — revenue − cost of revenue
- income_statement.r_and_d — research and development expense
- income_statement.sg_a — selling, general and administrative (incl. marketing)
- income_statement.restructuring_charges — restructuring and other charges
- income_statement.amortization_acquisition_intangibles — within opex
- income_statement.operating_expenses — total operating expense
- income_statement.operating_income — gross profit − operating expenses
- income_statement.gains_losses_equity_investments — net, non-operating
- income_statement.interest_expense — interest and other, net (as reported)
- income_statement.other_income_expense — other, net
- income_statement.income_before_taxes — pre-tax income
- income_statement.provision_for_income_taxes — tax provision (current + deferred)
- income_statement.income_continuing_operations — after-tax continuing ops
- income_statement.discontinued_operations — discontinued ops, net
- income_statement.net_income — GAAP net income attributable to the company
- income_statement.basic_eps — basic earnings per share, USD
- income_statement.diluted_eps — diluted earnings per share, USD
- income_statement.basic_shares — weighted basic shares, millions
- income_statement.diluted_shares — weighted diluted shares, millions

## balance_sheet.*
- balance_sheet.cash_and_equivalents
- balance_sheet.short_term_investments
- balance_sheet.trading_assets
- balance_sheet.accounts_receivable
- balance_sheet.inventories
- balance_sheet.other_current_assets
- balance_sheet.total_current_assets
- balance_sheet.ppe_gross — property, plant and equipment, gross
- balance_sheet.accumulated_depreciation
- balance_sheet.ppe_net — property, plant and equipment, net
- balance_sheet.operating_lease_right_of_use_assets
- balance_sheet.goodwill
- balance_sheet.acquisition_intangibles_net
- balance_sheet.equity_investments_noncurrent
- balance_sheet.deferred_tax_assets
- balance_sheet.other_noncurrent_assets
- balance_sheet.total_assets
- balance_sheet.accounts_payable
- balance_sheet.accrued_compensation
- balance_sheet.accrued_liabilities_other
- balance_sheet.deferred_revenue_current
- balance_sheet.income_taxes_payable
- balance_sheet.short_term_debt
- balance_sheet.total_current_liabilities
- balance_sheet.long_term_debt
- balance_sheet.operating_lease_liabilities_noncurrent
- balance_sheet.deferred_tax_liabilities
- balance_sheet.deferred_revenue_noncurrent
- balance_sheet.other_noncurrent_liabilities
- balance_sheet.total_liabilities
- balance_sheet.retained_earnings
- balance_sheet.accumulated_other_comprehensive_loss
- balance_sheet.total_stockholders_equity
- balance_sheet.non_controlling_interests

## cash_flow.*
- cash_flow.net_income
- cash_flow.depreciation
- cash_flow.amortization_acquisition_intangibles
- cash_flow.share_based_compensation
- cash_flow.deferred_taxes
- cash_flow.changes_in_working_capital
- cash_flow.cash_from_operating_activities
- cash_flow.ppe_additions — capital expenditures (capex)
- cash_flow.acquisitions_net_of_cash
- cash_flow.dividends_paid
- cash_flow.repurchases_of_stock
- cash_flow.cash_from_investing_activities
- cash_flow.cash_from_financing_activities
- cash_flow.net_change_in_cash

## segment.<SEGMENT>.<measure> (SEGMENT ∈ CCG, DCAI, NEX, Foundry, Altera, Mobileye)
- segment.<SEGMENT>.revenue
- segment.<SEGMENT>.operating_income

## ratio conventions
- Periods label as `YYYY-QN` (quarterly) or `YYYY` (annual).
- YoY = vs. same period prior year; QoQ = vs. prior quarter (A1 engine computes).
