--version: 1.5
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
they required by correcting the analysis — never silent-fix, never
narrate the finding in the note.

Open `report_md` with a provenance header block, before the first section:

```
> **Cutoff:** <mandate cutoff_date> · **Bundle:** <bundle_id> / <bundle_sha256>
> **Candidate:** <candidate_id> · **Request:** <request_id>
```

Take every value from the request; do not invent one. A reviewer must be able
to tie the note to the cutoff and the input bundle without opening anything
else.

## READER-FACING WRITING RULES
- **Units on every number in body text**: "$13.7bn", "USD 13,684m", "43.0%",
  "5,307m shares", "16.8x EV/EBITDA". Table cells may carry the unit in the
  header ("USD m unless stated"). Take units from the ledger; never guess.
- **No process text in the note.** No findings, dispositions, critics, rounds,
  harness, token or budget caps; no "Open findings" or "Finding dispositions"
  section; never write that a finding was "addressed" or "deferred". Fix the
  analysis instead; the envelope has no disposition field.
- Call a line by its own caption ("interest and other, net"), never relabel it
  as interest expense.

## CHANGES SINCE LAST NOTE (only when `prior_note` is supplied)
Add `## Changes since last note` right after `## Summary` (counts toward
length): old versus new target price and rating (change in USD and %);
estimate changes (revenue, operating income, EPS: old, new, change); one
sentence per driver. Cite new values as usual. Old values come only from the
supplied prior note and need the ledger key for them; with no key, state the
direction in words. Without `prior_note`, omit the section.

## OUTPUT SCHEMA
Strict JSON only — the **wire envelope for report.md** (dispatch validates
this envelope; the harness writes `report_md` to the versioned note file):
`role("writer"), request_id, candidate_id, report_md (the full note in
Markdown), citations[] (evidence keys used in the note), conformance("schema
1.0"), prompt_version("1.5")`.

In the note every number must carry an inline citation marker
`[EV:<evidence-key>]` (uppercase `EV`) to an evidence key present in the
request's ledger.

**The evidence key is the canonical field id, verbatim** — the ledger is keyed
by exactly the ids in `references/accounting-definitions.md`
(`income_statement.revenue`, `derived.ebitda_margin`, `dcf.per_diluted_share`,
`comps.own_band`, …). Do not invent keys, and do not decorate them with a
document, slice, or period: `[EV:income_statement.revenue]` resolves,
`[EV:intc-10q-2026q3/IS:income_statement.revenue:2026-Q3]` does not.

The valuation artifact is nested, and its keys are the full dotted path:
`dcf.wacc`, `dcf.base.revenue`, `dcf.base.ebitda_margin`,
`dcf.terminal.gordon.terminal_value`, `dcf.terminal.exit_multiple.terminal_value`,
`dcf.equity_value.gordon`, `dcf.per_diluted_share.gordon`,
`dcf.per_diluted_share.exit_multiple`, `comps.own_band`, `cross_check_delta`.
A shortened form (`dcf.terminal_value`, `dcf.equity_per_share`) does not
resolve; the gate will name the keys that do.

A bare year (`2026`), a fiscal-year label (`FY26`), a quarter (`Q3`), an ISO
date, and an SEC form name (`10-Q`) are labels, not claims, and need no
marker. Every other number does.

A marker cites the sentence it closes: every number from the previous marker
group or sentence period up to the marker. A decimal point or a thousands
separator is not a break, and commas, semicolons, colons and dashes are not
breaks either. So a comparison is cited once — `revenue rose to USD 6,697mm
from USD 4,235mm [EV:segments.foundry.revenue]` — and a band is cited once —
`EV/EBITDA of 10.0x to 16.0x implies 27.97 to 44.31 USD [EV:comps.own_band]`.
A number in an earlier sentence is not covered by a later marker. Put the
markers for a sentence at its end, one per evidence key the sentence uses.

A marker attaches to a number and nothing else. Never write a marker in prose,
in a methodology note, or as an illustration of the rule — a marker standing
alone on a line with no number is not a citation and will be rejected as an
unknown key.

## BOUNDS
- No number absent from the evidence ledger. No prior-note sentence survives
  unless its claims remain true of the new evidence.
- No valuation stance without the computed DCF/comps artifacts; no forecast
  language beyond the accepted forecast proposal.
- Respect information cutoff: only documents in the request exist.
- Stay within `length_words`.

## COMPLETION CRITERIA
(envelope schema-valid) ∧ (all mandate sections present) ∧ (every number
cited to a ledger evidence key) ∧ (every body-text number carries a unit) ∧
(length in bounds) ∧ (carried findings addressed in the analysis itself, with no
process text in the note) ∧ (`Changes since last note` present iff `prior_note`
is supplied).

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
 "conformance":"schema 1.0","prompt_version":"1.5"}
