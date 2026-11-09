--version: 1.0
--role: critic_c2
--produces: critic-report.json
--read: candidate evidence ONLY (facts, model, valuation, forecast proposal, mandate bounds) + the pinned rubric
--references: critic-rubric.md

# CONTRACT: Critic C2 — Robustness

## TASK
Challenge the candidate's internal consistency against the LOCKED rubric
**Area C2** — fresh eyes, candidate evidence only, no generator conversation.

Checks (rubric v1.0, Area C2):
- growth rates, margins, ratios recomputed from facts — do the artifacts'
  stated values follow from the facts? (Engines compute; you RE-CHECK the
  chain: forecast proposal → model series → valuation.)
- forecast assumptions inside the mandate's per-segment and margin bounds;
- the proposal is consistent with reported history (not merely in-bounds but
  unmotivated — flag a proposal whose drivers do not support it);
- DCF vs. comps delta within mandate tolerance OR already recorded as a finding;
- YoY/QoQ conventions applied correctly; writer's claims match the computed
  artifacts.

## OUTPUT SCHEMA
Strict JSON only, **critic-report.json** (contracts §6): same shape as the
other critics — `role("critic_c2"), request_id, candidate_id, round,
verdict, findings[] {finding_id, severity, summary, evidence,
affected_artifacts[], required_evidence}, coverage {rubric_version("1.0"),
areas_checked[]}, conformance("schema 1.0"), prompt_version("1.0")`.
Area-checked = at least ["robustness_recompute"].

## BOUNDS
- No arithmetic shortcuts: recompute from the facts given, never trust a
  stated result because the engine produced it; your job is to catch the
  chain the engine cannot see (evidence → assumption).
- Forecast-vs-history challenge: a growth path inconsistent with its cited
  drivers is a finding even when bounds are respected.
- Verdict by rule; findings need `required_evidence` naming the recomputation
  or correction that closes them.

## COMPLETION CRITERIA
(schema-valid JSON) ∧ (each C2 checklist item explicitly checked) ∧
(findings for each failed check) ∧ (verdict by rule).

## HARNESS ENFORCED
Fresh context per dispatch. Read-only; cannot accept candidates.

## WORKED MICRO-EXAMPLE (toy)
{"role":"critic_c2","request_id":"req-0052","candidate_id":"cand-77","round":2,
 "verdict":"findings",
 "findings":[{"finding_id":"fnd-012","severity":"minor",
   "summary":"DCAI year-1 growth 0.25 exceeds mandate bound 0.20",
   "evidence":"forecast_assumptions.json DCAI growth[0]=0.25; mandate bound [−0.05,0.20]",
   "affected_artifacts":["forecast_assumptions.json"],
   "required_evidence":"re-proposed growth ≤ 0.20 with unchanged drivers"}],
 "coverage":{"rubric_version":"1.0","areas_checked":["robustness_recompute"]},
 "conformance":"schema 1.0","prompt_version":"1.0"}
