# Critic Rubric — v1.1

**LOCKED — not evolvable by proposals.** Supersedes v1.0 (kept unchanged in
`critic-rubric.md` for history). A proposal cannot weaken its own acceptance
standard (design-decisions §5).

Why v1.1: v1.0 gave critics an open-ended job, so findings changed wording
every round, and it asked them to check exact properties (headers, version
pins, token budgets, write scope) that code checks better. v1.1 defines
severity, turns each area into a numbered checklist, and moves the exact
checks into the deterministic `controls_conformance` gate.

## Severity
- **major**: a wrong number (it does not recompute from the facts or engine
  artifacts), a claim in the note not supported by an artifact, a cutoff
  breach, a mandate-bounds breach, a write-scope breach.
- **minor**: everything else (wording, presentation, missing context). Minor
  findings never block acceptance. They are recorded and passed to the writer
  once.
- A critic may not raise a finding about something outside its checklist.

## Checklist format
A critic returns `checks[] {check_id, result: "pass"|"fail"|"n/a", evidence}`
covering **every** ID of its area, and `findings[]` only for failed checks,
each carrying `check_id`. `verdict = "clean"` iff there are no major findings.
A report missing a check ID is invalid and is retried at the same role. The
finding identity (dedup key) is computed by the ledger from
(critic, check_id, sorted affected_artifacts); the critic does not supply it.

## Resolutions
For each carried open finding (cited by its ledger id) the critic returns
either `verified_resolutions[] {finding_id, verified_evidence {artifact,
sha256}, check_performed}` (hash taken from the payload's artifact hash list)
or re-emits the finding with the same `check_id`. Silence is invalid.

## Area C1 — Evidence and citation
Payload: facts, model and valuation artifacts, note, input manifest.
- C1.1 every number in the note resolves to an evidence key.
- C1.2 the cited value equals the ledger value within rounding.
- C1.3 cited documents are in the input manifest.
- C1.4 missing disclosures are declared, not filled.
- C1.5 no outside knowledge.

## Area C2 — Robustness
Payload: model, valuation, forecast proposal, mandate bounds, note.
- C2.1 growth, margins and ratios in the note equal the artifacts.
- C2.2 the proposal lies within bounds.
- C2.3 the proposal is consistent with history and the cited drivers.
- C2.4 DCF vs comps within tolerance or already flagged.
- C2.5 YoY/QoQ conventions.
- C2.6 the terminal reconciliation is within tolerance or disclosed.

C2 must NOT raise findings about engine methodology (base-period choice,
cash-flow build formulas, discounting convention). If it believes the engine is
wrong it raises a **minor** finding whose summary starts with "ENGINE:".

## Area C3 — Timing and cutoff
Payload: input manifest, artifact headers, note, mandate dates.
- C3.1 no post-cutoff fact or event is referenced in the note.
- C3.2 dated blocks (price, peers) are at or before the cutoff.

## Area C4 — Risk and controls
Payload: the note, the manifest of artifacts with role and size.
- C4.1 the note contains no process or control text (findings, budgets,
  harness).
- C4.2 the risks section is present and specific to the company.

## Moved to deterministic gates
Header completeness (request ID, candidate ID, prompt versions, hashes,
cutoff, conformance), version pins and drift, per-role token budget, and
write scope by path are no longer critic checks. They are the gate
`controls_conformance` (gates.py), run before review. Its failures are
system-owned and are not sent to the writer.
