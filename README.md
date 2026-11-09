# CoverageFlow

A long-running agent workflow for **maintaining sell-side equity coverage** —
AI Coding in Finance (Fall 2026) final project.

**Author:** Gaetano Marco Chiantia — Student ID 25210660138

> ⚠️ Work in progress — kickoff commit. Setup/run instructions and the file guide
> will be completed before submission. See `docs/design-decisions.md` for the locked
> design record and `docs/task-spec.md` for the task specification.

## What this is

CoverageFlow turns dated SEC-filing bundles into independently verified, human-approved,
versioned coverage notes for covered companies (demo: Intel, AMD). New dated documents
trigger staged rebuilds — extract → model → valuation → report — with a
Generator/Critic revision loop, checkpoint/recovery, budget control, and an evolution
system that regression-tests changes to the system itself before human approval.

## Documents

- `docs/design-decisions.md` — locked design record (read this first)
- `docs/task-spec.md` — task specification (Requirement 1)
