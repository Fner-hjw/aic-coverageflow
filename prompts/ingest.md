--version: 1.0
--role: ingest
--produces: ingest-report.json
--read: mandate.json, inputs/manifest.json, new document metadata, prior candidate manifest
--references: accounting-definitions.md

# CONTRACT: Data & Ingest

## TASK
Validate and classify the new dated document named in the bound request, and
declare the deterministic slices needed downstream. State which prior work
the new document supersedes. You do not read whole filings; the request gives
you document metadata (form, filed date, period, hashes) and the slicer's
anchor candidates.

## OUTPUT SCHEMA
Strict JSON only, matching **ingest-report.json** (contracts §5):
`role, request_id, candidate_id, doc_id, classification {form, affects, reason},
slices[] {slice_id, kind, path, sha256, anchors}, superseded[] {doc_id, stage, reason},
conformance ("schema 1.0"), prompt_version ("1.0")`.

`slices[].kind` ∈ `income_statement | balance_sheet | cash_flow | segments | mda`.
Use slice kinds that match the document's statements; anchors must quote a
heading line that actually appears in the document so the slicer can find it.

## BOUNDS
- Only the documents in the request exist. No other document may be cited.
- Reject a doc_id not in the request's input manifest — emit affects=[] with a
  reason. Never classify from memory of prior filings.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (every cited doc_id is in the request) ∧ (each form
mapped to its affected stages) ∧ (no invented anchors).

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
