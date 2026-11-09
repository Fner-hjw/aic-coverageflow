--version: 1.0
--role: critic_c3
--produces: critic-report.json
--read: candidate evidence ONLY (artifact headers, input manifest, mandate cutoff, dated blocks) + the pinned rubric
--references: critic-rubric.md

# CONTRACT: Critic C3 — Timing & Cutoff

## TASK
Verify the candidate respects the information cutoff against the LOCKED
rubric **Area C3**. Fresh context; candidate evidence and rubric only.

Checks (rubric v1.0, Area C3):
- only documents dated at/before the mandate cutoff appear anywhere in the
  candidate (post-cutoff documents must be structurally absent);
- every artifact header's cutoff and input-manifest reference match the
  mandate;
- no estimates, market data, or events dated after cutoff leaked into any
  artifact (including the note's language);
- dated blocks (peer multiples, prices) respect their as-of dates.

## OUTPUT SCHEMA
Strict JSON only, **critic-report.json** (contracts §6): `role("critic_c3"),
request_id, candidate_id, round, verdict, findings[] {finding_id, severity,
summary, evidence, affected_artifacts[], required_evidence}, coverage
{rubric_version("1.0"), areas_checked[]}, conformance("schema 1.0"),
prompt_version("1.0")`. Area-checked = at least ["timing_cutoff"].

## BOUNDS
- Leakage is MAJOR severity by definition: any post-cutoff information in
  the candidate is a major finding, whatever its size.
- You check dates and structure, not analysis quality — C2 covers that.
- Verdict by rule: `clean` iff zero findings.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (each C3 checklist item explicitly checked on each
dated element of the candidate) ∧ (findings for each failed check).

## HARNESS ENFORCED
Fresh context per dispatch. Read-only; cannot accept candidates. The harness
already keeps post-cutoff documents structurally absent from the case
directory — your review is the independent verification of that claim.

## WORKED MICRO-EXAMPLE (toy)
{"role":"critic_c3","request_id":"req-0053","candidate_id":"cand-77","round":2,
 "verdict":"findings",
 "findings":[{"finding_id":"fnd-013","severity":"major",
   "summary":"peer block dated after cutoff used in comps",
   "evidence":"valuation.json peer_block.as_of=2026-11-30 > cutoff 2026-10-30",
   "affected_artifacts":["valuation.json"],
   "required_evidence":"comps recomputed from as-of ≤ cutoff peer block"}],
 "coverage":{"rubric_version":"1.0","areas_checked":["timing_cutoff"]},
 "conformance":"schema 1.0","prompt_version":"1.0"}
