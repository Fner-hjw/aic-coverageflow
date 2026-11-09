# Critic Rubric — v1.0

**LOCKED — not evolvable by proposals.** This rubric is part of the system's
acceptance standard; a proposal cannot weaken its own acceptance standard
(design-decisions §5). Candidate prompt cards may evolve; this rubric may
not. Four fixed review areas, each with an explicit check list. Every critic
review must check its own area end-to-end and emit findings for every failed
check.

## Area C1 — Evidence & Citation
- Every fact in facts.json carries a citation {doc_id, slice_id, anchor,
  locator} that exists in the bound candidate evidence.
- Every number in report.md exists in the verified evidence ledger (facts,
  model, valuation) — no number absent from the ledger.
- Citations are not stale: cited doc_ids are in the case input manifest.
- Missing disclosures are declared in `missing`, not silently omitted.
- No invented values, no outside knowledge, no plausible-looking fillers.

## Area C2 — Robustness
- Growth rates, margins, and ratios are recomputed from facts and match the
  model artifacts (arithmetic is code; the model specialist's proposal must
  be consistent with history).
- Forecast assumptions stay inside the mandate's declared bounds per
  segment and margin floor/ceiling.
- DCF and comps cross-check delta is within mandate tolerance or the
  disagreement is already a recorded finding.
- Year-over-year / quarter-over-quarter conventions are applied correctly.
- The writer's claims match what the computed artifacts actually say.

## Area C3 — Timing & Cutoff
- Only documents at or before the mandate cutoff are used anywhere in the
  candidate (post-cutoff documents must be structurally absent).
- Every artifact header's cutoff and input manifest reference match the
  mandate's.
- The candidate does not use estimates, news, or events dated after cutoff.
- Dated blocks (peer multiples, prices) respect their as-of dates.

## Area C4 — Risk & Controls
- Write-scope respected: candidate artifacts contain only what their role
  was permitted to write; no control state, no other roles' artifacts.
- Budgets: reported token usage for role calls is within the per-role cap.
- Required artifact schemas present with conforming headers (request ID,
  candidate ID, prompt versions, hashes, conformance).
- Rollout discipline: any system-version-dependent behavior is pinned;
  unexplained version drift is a finding.

## Verdict policy (from docs/contracts/state-and-artifacts.md §6)
- verdict = `clean` iff zero findings; otherwise `findings`.
- Each finding: finding_id, severity (major|minor), summary, evidence,
  affected_artifacts, required_evidence (what a repair must show).
- Coverage: record rubric_version ("1.0") and areas_checked.
