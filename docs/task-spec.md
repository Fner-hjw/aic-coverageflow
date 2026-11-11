# Task Specification — CoverageFlow

**AI Coding in Finance (Fall 2026) — Final Project, Requirement 1**
Author: Gaetano Marco Chiantia (25210660138)

## 1. The activity

CoverageFlow automates the maintenance of **sell-side equity coverage**: the continuous
updating of a financial model, a valuation, and a research note for a covered company
as new dated information arrives.

Concretely, each *coverage case* turns a **dated document bundle** (SEC filings:
10-K, 10-Q, 8-K) plus an **information cutoff date** into an updated, independently
verified, human-approved version of the coverage note — with a complete evidence trail
of what was known, calculated, reviewed, and decided, and when.

## 2. Intended user

- **Primary:** a sell-side equity analyst maintaining coverage of the company (here: Intel Corporation, with AMD as a second bundle) — a "desk of one" who must keep model, valuation, and note current.
- **Secondary:** a supervisory analyst / human approver who reviews updated coverage before it is published.

## 3. Current process and its problems

An earnings release or 8-K drops after market close. The analyst downloads the filing,
manually re-keys numbers into an Excel model, updates the DCF/comps tabs, edits the
research note by hand, re-checks figures by rereading the filing, and emails a draft to
the supervisor. The failures this process produces are exactly what a long-running
workflow exists to remove:

1. No version discipline — which numbers came from which filing, at what date?
2. No verification independent of the person who made the updates.
3. Work is lost when interrupted; after an interruption the analyst restarts from zero.
4. Citations go stale silently.
5. No evidence trail of what was known at which date (analysts face re-examinations).
6. Improvements to the process itself (templates, prompts, mapping rules) are never regression-tested; nobody can roll back safely.

## 4. Stages, dependencies, triggers

Stages per case (slide-4 structure):

```
mandate → Orchestrator → [Data & Ingest → Extraction → Model & Valuation → Report Draft]
→ gates → [C1 Evidence & Citation, C2 Robustness, C3 Timing & Cutoff, C4 Risk & Controls]
→ merge/coverage → repair loop → human approval → published version
```

Dependencies: ingest requires the frozen bundle; extraction requires slices; model/
valuation require facts + mandate assumptions; report requires the evidence ledger;
critics require the candidate + fixed criteria; publication requires all gates + 2
clean full reviews + human approval.

Recurring triggers:

- **New-input:** a dated document arrives (quarterly report, annual report, material 8-K) → new case with cutoff = filing date.
- **Scheduled staleness check:** newest published note older than N days vs. the newest available document → reassessment case.
- **Human action:** approval, rejection, rollback of system versions, evolution deployment.

## 5. Permitted sources and information cutoffs

- Inputs are restricted to the **frozen, hashed document bundle** (EDGAR-derived snapshots) plus the **dashboard assumptions file** (peer multiples, WACC, bounds — human-owned, dated, hashed, never automatable).
- No web browsing, news, or transcripts are permitted during a run.
- The cutoff date is stamped in the mandate; every artifact records the exact input manifest (document IDs + SHA-256 hashes) it was built from. Post-cutoff documents are *structurally absent* from a case directory — cutoff enforcement is not a prompt request. Perturbation testing proves no-leakage.

## 6. Work products

- Model artifact suite (facts, model, valuation, forecast — JSON/CSV; exported xlsx).
- Versioned research note (`report.md`). One versioned note per company; version pinned to a case, cutoff and approval.
- Exported dossier per published version (PDF/LaTeX, HTML, xlsx).
- Case dossier: state + trace + finding ledger + gate results + approval records.

## 7. Acceptance criteria

1. All numerical gates pass.
2. Two consecutive clean full critic reviews of the same candidate — any candidate change resets the streak.
3. All carried findings closed by critic-verified repairs.
4. Human approval recorded (who/when/what) — research acceptance never authorizes publication; only the human gate does.

Each criterion is fixed before the run and locked from agent proposals.

## 8. Why a persistent workflow, not a one-shot script

The work spans days between triggers and depends on prior state; revision requires
carried findings and repair packets; recovery requires verified checkpoints; lengthy
runs require budget control (all four lecture "why finance is different" properties
apply — consequential actions, dated evidence, bounded authority, continuity/change).
A single prompt or report update has none of these — and the requirements document
explicitly excludes it.

## 9. Scope compared to alternatives

- Buy-side quant research (the lecture's QT) shares the same skeleton but CoverageFlow's triggers are naturally event-driven (filings arrive continuously), making the long-runtime and resume properties more prominent.
- The special twist of this system over a plain coverage generator — the Model & Valuation **specialist proposes a bounded forecast** that critics challenge from fresh contexts — makes the revision loop do genuine analytical work.
