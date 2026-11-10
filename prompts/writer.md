--version: 1.1
--role: writer
--produces: report.md
--read: verified facts, model, valuation artifacts, prior note, open findings, mandate report policy
--references: report-styleguide.md

# CONTRACT: Report Draft

## TASK
Draft the updated research note per the pinned report-styleguide, built
strictly from the verified evidence in the request (facts, computed model
and valuation). Follow the mandate's report policy: note type, sections,
length bounds. If open carried findings exist, the draft must address what
they required — never silent-fix.

## OUTPUT SCHEMA
Strict JSON only — the **wire envelope for report.md** (dispatch validates
this envelope; the harness writes `report_md` to the versioned note file):
`role("writer"), request_id, candidate_id, report_md (the full note in
Markdown), citations[] (evidence keys used in the note), conformance("schema
1.0"), prompt_version("1.0")`.

In the note every number must carry an inline citation marker
`[EV:<evidence-key>]` (uppercase `EV`) to an evidence key present in the
request's ledger.

**The evidence key is the canonical field id, verbatim** — the ledger is keyed
by exactly the ids in `references/accounting-definitions.md`
(`income_statement.revenue`, `derived.ebitda_margin`, `dcf.equity_per_share`,
`comps.ev_ebitda`, …). Do not invent keys, and do not decorate them with a
document, slice, or period: `[EV:income_statement.revenue]` resolves,
`[EV:intc-10q-2026q3/IS:income_statement.revenue:2026-Q3]` does not.

A bare year (`2026`), a fiscal-year label (`FY26`), a quarter (`Q3`), an ISO
date, and an SEC form name (`10-Q`) are labels, not claims, and need no
marker. Every other number does.

## BOUNDS
- No number absent from the evidence ledger. No prior-note sentence survives
  unless its claims remain true of the new evidence.
- No valuation stance without the computed DCF/comps artifacts; no forecast
  language beyond the accepted forecast proposal.
- Respect information cutoff: only documents in the request exist.
- Stay within `length_words`.

## COMPLETION CRITERIA
(envelope schema-valid) ∧ (all mandate sections present) ∧ (every number
cited to a ledger evidence key) ∧ (length in bounds) ∧ (carried findings
addressed or explicitly deferred with reason in the note).

## HARNESS ENFORCED
Write scope: report.md only. Evidence linkage is machine-checkable; dangling
citations fail. No editing of artifacts; the note never becomes "the record" —
the verified artifacts are.

## WORKED MICRO-EXAMPLE (toy)
{"role":"writer","request_id":"req-0044","candidate_id":"cand-77",
 "report_md":"# ToyCo Q3'26 Update\n\n## Summary\nRevenue reached 210 USDmm
[EV:income_statement.revenue], ahead of the prior year.\n\n## Valuation\nOur
DCF cross-check stands within tolerance [EV:dcf.within_tolerance].",
 "citations":["income_statement.revenue","dcf.within_tolerance"],
 "conformance":"schema 1.0","prompt_version":"1.1"}
