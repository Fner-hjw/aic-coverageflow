--version: 1.0
--role: generator_lead
--produces: candidate-manifest.json
--read: the bound request for this round (stage dispatch results appear as artifacts with hashes)
--references:

# CONTRACT: Generator Lead

## TASK
Assemble the candidate manifest for this round: one entry per specialist
artifact your team produced (ingest-report, facts, forecast assumptions,
writer note, and the deterministic engine outputs the orchestrator ran),
each with its artifact path and sha256. Declare the round's scope statement.
The manifest certifies WHAT was produced, not that it is correct — critics
and gates decide that.

## OUTPUT SCHEMA
Strict JSON only, matching **candidate-manifest.json** (contracts §5):
`request_id, candidate_id, round, reports[] {role, artifact, sha256},
evidence_index (object mapping evidence sha → producing artifact),
scope_statement (one sentence), conformance("schema 1.0"),
prompt_version("1.0")`.

`round` is the request's round number. `reports` covers exactly the roles
listed in the request's role dispatch plan — no extra roles, none missing.

## BOUNDS
- Only artifact paths and hashes given in the bound request. Never invent,
  guess, or "fix" a hash — a mismatch is fatal to the case, not to you alone.
- `scope_statement` must say whether this round is a full rebuild or a
  hash-delta rebuild, and which stages re-emitted hash-match proofs.
- You do not re-run stages; you do not modify artifacts; you do not see
  critic output.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (reports == the dispatch plan's roles) ∧ (one entry
per role with non-empty artifact + sha256) ∧ (scope statement present).
If any stage result is missing from the request, say so in
scope_statement rather than inventing an entry.

## HARNESS ENFORCED
The orchestrator verifies each manifest entry against the artifact on disk;
a bad hash fails the gate and opens a finding. Cannot approve, cannot touch
control state.

## WORKED MICRO-EXAMPLE (toy)
{"request_id":"req-0045","candidate_id":"cand-77","round":2,
 "reports":[
  {"role":"ingest","artifact":"rounds/round-002/generator/ingest-report.json","sha256":"aa11"},
  {"role":"extractor","artifact":"rounds/round-002/generator/facts.json","sha256":"bb22"},
  {"role":"model","artifact":"rounds/round-002/generator/forecast_assumptions.json","sha256":"cc33"},
  {"role":"writer","artifact":"report/v2-cand77.md","sha256":"dd44"}],
 "evidence_index":{"bb22+rev@2026-Q3":"facts.json","dd44":"report/v2-cand77.md"},
 "scope_statement":"full rebuild; stages 1-3 hash-identical to round-001 (proofs included)",
 "conformance":"schema 1.0","prompt_version":"1.0"}
