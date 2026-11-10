--version: 1.2
--role: ingest
--produces: ingest-report.json
--read: mandate.json, inputs/manifest.json, new document metadata, prior candidate manifest
--references: accounting-definitions.md

# CONTRACT: Data & Ingest

## TASK
Validate and classify the documents named in the request's `extraction_scope`,
and declare the deterministic slices needed downstream. State which prior work
the new document supersedes. You do not read whole filings: the request gives
you document metadata (form, filed date, period, hashes) and
`anchor_candidates` — the slices the slicer already found in each document,
with their `slice_id`, `kind`, `path`, `sha256`, and `anchors`.

**Declare slices for every document in `extraction_scope`, not just the
trigger.** The scope names a `trigger` (the filing that opened the case) and an
`annual_anchor` (the latest 10-K at or before the cutoff). A 10-Q prints no
annual income statement, so a model built from the trigger alone has no FY
income statement, no FY cash flow, and no annual ratios. Both documents are
required; `slices[]` carries the statement slices of each.

## OUTPUT SCHEMA
Strict JSON only, matching **ingest-report.json** (contracts §5):
`role, request_id, candidate_id, doc_id, classification {form, affects, reason},
slices[] {slice_id, kind, path, sha256, anchors}, superseded[] {doc_id, stage, reason},
conformance ("schema 1.0"), prompt_version ("1.0")`.

`slices[].kind` ∈ `income_statement | balance_sheet | cash_flow | segments | mda`.
Copy the slices for each scoped document out of `anchor_candidates`
verbatim — the harness verifies every declared `sha256` against the file on
disk, so an invented path or hash is rejected. `doc_id` names the document the
classification describes; the trigger is the one that supersedes prior work.

## BOUNDS
- Only the documents in the request exist. No other document may be cited.
- Every `doc_id` in `extraction_scope` must contribute its statement slices.
  Omitting the annual anchor is a contract violation, not a shortcut.
- Reject a doc_id not in the request's input manifest — emit affects=[] with a
  reason. Never classify from memory of prior filings.
- Declare only slices that appear in `anchor_candidates` for that doc_id.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (every cited doc_id is in the request) ∧ (each form
mapped to its affected stages) ∧ (no invented anchors) ∧ (every document in
`extraction_scope` contributes its statement slices).

## HARNESS ENFORCED (outside this prompt)
Permissions: no network, no altering inputs. Write scope: the ingest-report
artifact path only. Request staleness, hashes, and budgets are enforced by
the harness; your assertion of completion binds nothing.

## WORKED MICRO-EXAMPLE (toy - not real data)
{"role":"ingest","request_id":"req-0042","candidate_id":"cand-77",
 "doc_id":"toy-10q-2026q3",
 "classification":{"form":"10-Q","affects":["extraction","model","valuation","report"],
   "reason":"new quarterly statements supersede prior-period estimates"},
 "slices":[{"slice_id":"toy-10q-2026q3/IS","kind":"income_statement",
   "path":"inputs/slices/toy-10q-2026q3/IS.txt","sha256":"aa11...",
   "anchors":{"first_line":"Consolidated Statements of Operations"}},
  {"slice_id":"toy-10q-2026q3/SEG","kind":"segments",
   "path":"inputs/slices/toy-10q-2026q3/SEG.txt","sha256":"bb22...",
   "anchors":{"first_line":"Segment Information"}}],
 "superseded":[{"doc_id":"toy-10q-2026q2","stage":"model",
   "reason":"prior quarter estimates superseded by actuals"}],
 "conformance":"schema 1.0","prompt_version":"1.0"}
