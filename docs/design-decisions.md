# CoverageFlow — Locked Design Decisions

**Project:** AI Coding in Finance (Fall 2026) — Final Project
**Author:** Gaetano Marco Chiantia (ID 25210660138)
**Status:** Locked design record (Rounds A–F). This document is the single reference for every
implementation decision. Changes require a new numbered decision entry.

---

## 1. Task domain (Requirement 1)

- **Task:** Long-running agent workflow maintaining sell-side equity coverage: new dated
  documents trigger staged rebuilds (extract → model → valuation → report), independent
  verification, human approval before publication.
- **Intended user:** A sell-side equity analyst maintaining coverage; secondary user: the
  supervisory analyst/human approver.
- **Current manual process:** analyst re-keys numbers from filings into Excel, updates
  DCF/comps and the Word note by hand, no version discipline, no independent verification,
  no recovery, stale citations, no evidence trail.
- **Demo companies:** Intel Corporation (primary, 12 verified EDGAR documents) and
  Advanced Micro Devices (second bundle for held-out / generality evidence).
  Bundles are frozen, hashed snapshots of EDGAR filings; the system accepts
  any company via the EDGAR bundle builder.
- **Recurring triggers:** (1) new dated document arrival; (2) scheduled staleness check
  (report older than N days vs. newest available document); (3) human action
  (approve/reject/rollback/evolution deployment).
- **Permitted sources & cutoffs:** only the frozen document bundle; no web/news/transcripts.
  Information cutoff stamped into the mandate; every artifact records cutoff + input
  manifest (doc IDs + SHA-256). No-leakage is proven by perturbation test.
- **Work products:** model artifacts (facts/model/valuation JSON+CSV+xlsx), versioned
  research note (report.md → LaTeX PDF dossier + HTML snapshot), evidence trace, finding
  ledger, human approval record.
- **Why persistent, not one-shot:** event-driven across days; interruption survival;
  repeated evaluation; carried-state revision loop; human approval pause/resume;
  evolvable system under regression testing.

## 2. Architecture (Requirement 2)

```
Analyst mandate (company, cutoff, report policy, limits)
        │
        ▼
   ORCHESTRATOR (pure Python, no LLM): dispatch + state + routing + gates + budgets
        │
        ▼
   GENERATOR LEAD — coordinates BUILD SPECIALISTS:
     1. Data & Ingest      (validate, classify relevance, deterministic slicing, provenance)
     2. Extraction         (schema-bound facts with citations, per statement slice)
     3. Model & Valuation  (deterministic engines; specialist PROPOSES bounded forecast)
     4. Report Draft       (report.md strictly from evidence ledger)
        │  candidate manifest (IDs + hashes)
        ▼
   numerical gates via Orchestrator
        │
        ▼
   CRITIC LEAD — fresh reviewers each round:
     C1 Evidence & Citation   C2 Robustness (recompute)
     C3 Timing & Cutoff       C4 Risk & Controls (budgets, permissions, scope)
        │  merge + coverage check → findings to Orchestrator
        ▼
   Repair packet: findings + required evidence; decision: repeat / end
        │  (acceptance: 2 consecutive clean full reviews, same candidate;
        │   any candidate change resets the streak)
        ▼
   HUMAN approval gate (pause; auditable approval record) → published report version

   EVOLUTION: prompt cards / mapping tables / engine code changes tested on
   frozen baseline cases, version registry, human approve, rollback.
   Locked (never evolvable): controller, gates, acceptance rules, rubric.

   Shared state & evidence: request IDs, artifacts, hashes, findings, versions.
```

### Role contracts (harness-enforced, not prompt-begged)

| Role | Sees | Writes | Never |
|---|---|---|---|
| Generator lead (controller code) | artifact hashes the orchestrator computed | candidate manifest | n/a: no agent, see below |
| Data & Ingest | mandate, new doc, prior manifest | ingest-report + slices | network; alter inputs |
| Extraction | slices only | facts.json (every fact cited) | invent facts; uncited numbers |
| Model & Valuation | facts + mandate assumptions | model/valuation/forecast artifacts | LLM arithmetic (engines are code) |
| Report Draft | facts, model, valuation, prior note, findings | report.md (evidence-linked) | numbers absent from ledger |
| Critic lead (controller code) | the four critic reports | merged findings (conflicts kept) | n/a: no agent, see below |
| C1–C4 | evidence + fixed criteria; fresh context every round | findings | edit candidate artifacts |
| Orchestrator | everything (it is code) | state.json, trace, ledger | be persuaded; mandate is fixed |

### Controller code, not lead agents (cost round)

The generator lead and critic lead are no longer dispatched. Both did work the
orchestrator already does or can do exactly, and each cost a call of 55,000 and
11,000 estimated tokens per round. Their cards (`prompts/generator_lead.md`,
`prompts/critic_lead.md`) and role registry entries were deleted rather than
kept unused, so no dead card can drift or show up in `prompt_versions`.
Decision rule: an agent decides, the controller executes.

- **Candidate manifest** (`Orchestrator._build_candidate_manifest`): the lead
  only echoed hashes the orchestrator computed, was checked against them, and
  had its header overwritten. The manifest is now written directly from those
  hashes and the harness header. `request_id` is a controller id `ctl-rNNN`
  because no agent request exists. Same file, same gates.
- **Critic merge** (`Orchestrator._merge_critic_reports`): union of all
  findings, each unchanged apart from `source_critic` and the computed
  `dedup_key` (`ledger.compute_dedup_key`). Same-key findings collapse into
  the first (`duplicates` lists the rest). A disagreement stays explicit: if
  one critic fails a check of an overlap group (`CHECK_OVERLAP`, currently
  `numbers` = C1.2 and C2.1) on an artifact and another critic passes a check
  of that group, the finding carries `conflict_group = "<group>:<artifact>"`
  and `disagreeing_critics`. Nothing is dropped or accepted by the merge.
  `clean`, `coverage_ok`, `reviewer_count` and the fresh-context proofs are
  computed from the reports. The human, not the merge, resolves conflicts.

Payload cost rules: prompts use compact JSON and one pipe-delimited row per
fact (`field|period|value|unit|doc_id|slice_id|locator`); the model role gets
income-statement, segment, capex, D&A and operating-cash facts for all periods;
the writer gets the current period, its YTD, the prior-year comparables, the
previous quarter and the last fiscal year. The ledger on disk and the evidence
index are unchanged, so scoping affects only what a role reads.

### Agent assembly model

Each role call = one fresh CLI/API invocation; context assembled by the controller:
base harness preamble (shared, versioned) + role card (versioned, hashed) + pinned
references (versioned per role-card manifest) + bound request data. Read-scope is what
the controller includes in the prompt; write-scope is one artifact path; schema validated
on submission; stale request_id rejected. No in-LLM tool calling in v1.

## 3. Loop design (Round B)

- **Case states:** CREATED → RUNNING → PENDING_APPROVAL → APPROVED/REJECTED → CLOSED;
  terminal: BLOCKED, EXHAUSTED, STOPPED. EXHAUSTED never grants acceptance.
- **Acceptance:** all numerical gates pass; 2 consecutive clean full critic reviews of
  the same candidate (any change resets the streak); all carried findings closed by
  critic-verified repairs; human approval.
- **Invalid submissions:** repair at the same role (new request, no round consumed);
  the repair budget (`retries_max`) bounds it, and exhausting it → EXHAUSTED.
- **Blocks are re-runnable:** BLOCKED is terminal for the run, not the case. A re-run
  clears the block, resets the blocked role's repair budget and resumes from the last
  verified stage; only APPROVAL_EXPIRED requires a fresh decision. EXHAUSTED stays
  terminal — the budget is a hard safety control.
- **Budgets:** 4 revision rounds; 3 retries per specialist dispatch; 45-minute case
  clock; per-role token cap (configurable). Checked at every dispatch; breach →
  EXHAUSTED with precise record.
- **State-first transitions:** validate → write state + checkpoint atomically → trace.
  Only the Orchestrator writes control state. A conversation's last line never
  establishes completion — only verified artifact + written checkpoint does.
- **Resume:** on startup, inspect identity + hashes; discard unverified partial
  output; re-dispatch pending role only; completed verified stages return saved
  artifacts; a completed case returns its saved result untouched.
- **Stale approval:** approval deadline (48h, mandate-configurable); expiry → BLOCKED
  (APPROVAL_EXPIRED); re-approval restarts acceptance from zero. **Decision: restart
  acceptance (strict).**
- **Repair rounds:** **full re-build** each round (all 4 specialist stages re-run,
  fresh eyes); stages may short-circuit internal work by emitting hash-match proofs
  (e.g., ingest re-emits pointer to unchanged slices with hashes). Changes candidate
  ID → resets clean streak.
- **Trace events:** CASE_CREATED, REQUEST_BOUND, AGENT_DISPATCHED, AGENT_RESULT,
  GATE_RUN, GATE_PASS, GATE_FAIL, STATE_TRANSITION, FINDING_OPENED,
  FINDING_VERIFIED_CLOSED, REPAIR_PACKET_ISSUED, BUDGET_CHECK, APPROVAL_REQUESTED,
  APPROVAL_RECORDED, CASE_CLOSED — each with IDs, hashes, timestamps, backend & token usage.

## 4. Financial model & valuation (Round C)

- **Depth:** ~80 fields, quarterly + annual; segments included
  (CCG, DCAI, NEX, Foundry, Altera, Mobileye) with **versioned mapping tables**
  (schema v1/v2 keyed to Intel's FY2024–26 segment reorganizations). Filings extract
  "as reported"; deterministic translation produces the consistent time series the
  engine consumes.
- **Line-item naming** adopts the conventions of the supplied xlsx templates
  (Revenue, Cost of Revenue, Gross Profit, R&D, SG&A, EBITDA, Operating Income, D&A, …).
- **Determinism boundary:** extraction is the only LLM step; growth rates, margins,
  FCF, DCF, comps, sensitivity are pure Python (`runtime/` engines), unit-tested.
- **Forecast:** **specialist-proposed growth path** — the Model & Valuation specialist
  proposes per-segment growth years 1–5 + margin glide path, each line citing evidence
  drivers from the facts ledger. Mandate bounds it (growth min/max per segment, margin
  floors/ceilings); controller rejects out-of-bounds at the gate. WACC and terminal
  growth are human-owned (mandate). The forecast proposal is part of the candidate
  (changes reset the clean streak); C2 challenges forecast-vs-history; deterministic
  DCF math runs on the accepted path.
- **Valuation:** DCF (OCF − capex; 5 years + terminal via exit multiple AND Gordon
  cross-check; year-end discounting convention stated in assumptions) + relative
  valuation from a dated peer-multiple block in the human-owned assumptions file
  (as-of date ≤ cutoff, hashed like any input) + own historical multiple band.
  DCF-vs-comps disagreement beyond mandate tolerance → finding for C2, human decides.
- **Ratios:** YoY/QoQ conventions follow the supplied "Complete Quantitative Analysis"
  workbook layout.

### Critic rubric v1.1 (supersedes v1.0, which is kept unchanged)

- **Why.** Under v1.0 the critics had an open-ended job: no definition of major
  versus minor, so findings were reworded every round and two consecutive clean
  reviews were unreachable. Two of four critics (C3, C4) were checking exact
  properties (headers, version pins, token budgets) that code checks better, and
  the controller deleted the budget findings with a regular expression under a
  made-up hash, quietly overriding a rubric declared locked.
- **What changed.** Severity is defined (major: wrong number, unsupported claim,
  cutoff, bounds or write-scope breach; everything else minor and never blocking).
  Each area is a numbered checklist (C1.1 ... C4.2); a critic returns
  `checks[]` for every ID plus `findings[]` only for failed checks. A missing ID, a
  finding for a passed check, a wrong verdict or silence on a carried finding is an
  invalid submission, repaired by the existing same-role retry. The dedup key is
  computed by the ledger from (critic, check_id, sorted artifacts). Critic payloads
  are role-scoped; `dispatch_records` and the budget regex filter are gone.
- **What moved to gates.** Header completeness, version pins and drift, per-role
  token budgets and write scope by path are the deterministic gate
  `controls_conformance` (run last in the gate order, before review). Its findings
  carry `owner: "system"` because the writer cannot repair them.

## 5. Evolution (Round D)

- **Evolvable:** role prompt cards, segment mapping tables, engine code (strict:
  regression must prove unchanged outputs on the bundle), report templates, engine
  default configs. **Locked:** controller logic, gate thresholds, acceptance rules,
  rubric — a proposal cannot weaken its own acceptance standard.
- **Loop:** capture failure → evo-prepare (links case + parent version + scope + budget)
  → scoped patch + tests by Generator → baseline regression on development corpus
  (offline replay backend, never live tokens) → fresh-critic review (evidence, scope,
  regressions) → version registry entry → human approve/reject/defer → active pointer
  swap; rejected/deferred retain active version; rollback restores prior pointer;
  existing cases keep their original version; new case verifies rolled-back behavior.
- **Corpus:** 4 development cases + 2 held-out cases, across two companies (Intel, AMD).
  Held-out cases never touch development loops; the registry records this risk.
- **Evidence demo (3 parts):** (5a) change passes → approved → new active version;
  (5b) deliberately over-broad proposal → auto-rejected at scope gate;
  (5c) authorized rollback with verification run.

## 6. State & evidence formats (Round B/C)

- Storage: **JSON files + JSONL trace**, atomic writes (temp + rename), no database.
- Case directory: mandate.json, inputs/ (+manifest), request.json, rounds/round-NNN/
  (generator|numerical|critic subdirs), findings.json (ledger with open/closed),
  state.json, trace.jsonl, result.json, approvals/, report/ (versioned), export/.
- All artifact headers carry: request ID, candidate ID, prompt versions, hashes,
  cutoff, input manifest ref.

## 7. Harness & backends

- Python package, no framework lock-in; `python -m coverageflow` CLI.
- Backends (auto-detect → ask; flag/config override):
  1. CLI headless: `claude -p`, `codex exec`, kimi headless — uses local login, no keys.
  2. OpenAI-compatible API: configurable base_url/model/key (env or config).
  3. Replay: recorded responses; used by the offline test suite and recovery evidence;
     recorded from live runs we already do.
- Long filings → deterministic slicer (income statement, cash flow, segments, MD&A)
  feeding extractors bounded slices with citation anchors.

## 8. Web UI (Rounds E)

FastAPI + no-build vanilla-JS SPA, institutional/paper aesthetic, WebSocket push.
Capabilities (all approved): run control; live monitoring dashboard (state, rounds,
findings, trace, budgets); approval gate with dossier; evolution management; case
inspector + artifact viewer; report version diff with click-through evidence links;
EDGAR new-company bundle builder; demo control panel (labeled "scripted rehearsal":
seed finding, kill run, perturbation check); run replay viewer; backend manager with
per-role token/cost meter; case export. **Full form mandate/assumptions editor + raw
JSON side-by-side**; reusable assumption templates per company. All UI actions route
through the controller — the UI never bypasses Orchestrator gates. No auth (local tool).

## 9. Exports per published case

```
report.md (master)
  → model.xlsx   (openpyxl; Statements Model / DCF / Company Comparison / Ratios tabs,
                  per supplied template conventions)
  → dossier.pdf  (GS/MS-style LaTeX research note; pdflatex headless; TeX Live present)
  → dossier.html (self-contained snapshot for browser/printing)
```
All renderers consume the same JSON evidence; exports cannot drift from the verified record.

## 10. Evidence & verification package (Requirement 2 bullet 3)

1. Clean coverage-update run (full trace).
2. Seeded finding → repair loop → finding verified closed.
3. Kill mid-run → resume (pending role only; no repeated completed work).
4. Cutoff/no-leakage perturbation test (post-cutoff changes leave outputs ≤ cutoff unchanged).
5. Evolution: 3-part demo (approve / scope-reject / rollback+verification).
Plus unit + regression test suite, all runnable offline on the replay backend.

## 11. Build plan & deliverables

- Repo: `Project/coverageflow/` (git). Zip: `25210660138-final-project.zip` including `.git/`.
- Streams (parallel subagents, contract-first): A1 runtime/data (EDGAR fetcher, bundles,
  slicer, engines, tests); A2 controller (state machine, gates, ledger, budgets, resume,
  registry, CLI); A3 agent layer (backends, role cards, dispatch, trace); A4 web UI;
  A5 docs & evidence (design note, diagram, README, 5 evidence runs, replays).
- Timeline: Mon Nov 9 repo/contracts; Tue A1–A3 integration, A4 start; Wed UI +
  runs 1–3; Thu evolution + runs 4–5 + docs; Fri buffer/walkthrough; Sat submit.
- Cut order if slipping: (1) UI extras beyond core; (2) LaTeX → HTML-only;
  (3) AMD held-out bundle (last resort). Never cut: controller, gates, evidence runs,
  recovery, docs, Git history.
