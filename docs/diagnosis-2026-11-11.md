> **Historical record.** This diagnosis was written on 2026-11-11, before the fixes it proposed (finding owners, ledger de-duplication, cheaper prompts, engine repairs) were made; those are in the git history. Source links below were written relative to the parent folder (`coverageflow/coverageflow/...`); inside this repository drop the first `coverageflow/`. The second phase is in `live-run-hardening.md`.

# CoverageFlow: diagnosis and fix plan

Date: 2026-11-11. Scope: the `coverageflow/` repository at commit `2da73f8`, checked against `Requirements.pdf` and the lecture `3_long_run_workflows.pdf`. Nothing inside the repository was changed.

## 1. Verdict

The controller plumbing is sound: state, checkpoints, resume, request binding, the gates and the offline test suite all work (542 tests pass in 21 s, and evidence run 1 reproduces). The three symptoms you reported come from a small number of causes.

**It never converges** for two reasons that compound:

1. **A ledger bug makes most open findings impossible to close.** Critics reuse finding IDs such as `fnd-c2-010` every round, and the ledger closes by ID, so it always hits the oldest row with that ID. In your live Intel case, 35 of the 40 open findings can never be closed, whatever the Generator does.
2. **Most blocking findings are about things no agent in the loop is allowed to change.** The critics keep finding real defects in the deterministic valuation engine, the model engine, the slicer and the harness headers. The Generator can only change segment growth rates, a gross-margin path and the note's wording. So it "addresses" each finding by describing the defect in the note, and the critic raises it again.

**It is slow and expensive** because every role is sent the whole evidence ledger as indented JSON (about 50,000 tokens by the harness's own estimate), one role does no work at all, and a single unmarked number in the note throws away a whole round.

**The output is weak** because the valuation engine is not a credible DCF, the slicer drops three of the four segment tables, and the note has no market price, target or rating.

Separately, your two target backends are not usable as they stand: the `codex` adapter fails on every call, and the `claude` adapter records 2 input tokens for a call that really used about 128,000, so the token budget never fires.

## 2. What I checked

- Read the controller, ledger, gates, dispatch, prompt assembly, CLI backends, both engines, the slicer, all role cards and the rubric. I only skimmed `ui/` (you did not list it as a problem).
- Ran the offline suite and re-ran evidence run 1 in a scratch folder.
- Analysed the existing live case `workspace/cases/intc-fy2026q3-20261030` (1,174 trace events, 101 ledger rows).
- Rebuilt the real writer and critic prompts from that case's round 11 and measured what is in them.
- Live calls, as you authorised: four `claude -p` calls with those real prompts (two with the adapter's current flags, two with stripped-down flags), plus trivial calls through the real `claude` and `codex` adapters. Total spend was about $2.20 for the four real calls plus a few cents.

I did not run a full live case end to end. The single-call tests already show that Claude Sonnet raises the same eight to eleven findings on the round-11 candidate that Mistral did, so a full run would have cost about $5 a round to confirm a failure that is already explained.

## 3. Why the loop never converges

Listed in the order I would fix them.

### 3.1 Finding IDs collide, so open findings cannot be closed (bug)

[ledger.py:112](coverageflow/coverageflow/ledger.py#L112) stores whatever `finding_id` the critic supplies. [ledger.py:135-138](coverageflow/coverageflow/ledger.py#L135-L138) closes the first row with that ID and returns silently if that row is already closed.

Critics number their findings from 1 each round, and the gates use `fnd-001` too. In the live case:

| Measure | Value |
|---|---|
| Ledger rows | 101 |
| Distinct finding IDs | 66 |
| Open rows | 40 (11 major) |
| Open rows hidden behind an older row with the same ID | 35 |
| `FINDING_VERIFIED_CLOSED` events in the trace | 240 |
| Rows actually closed | 61 |

The last two lines matter for the audit trail as well: the trace records closures that did not happen, because [orchestrator.py:1990-1997](coverageflow/coverageflow/orchestrator.py#L1990-L1997) emits the event without checking that anything changed.

**Fix.** The ledger assigns its own unique ID (`fnd-0001`, `fnd-0002`, …) and keeps the critic's label only as a reference field. `close_finding` takes the row, raises if the row is already closed or the ID is ambiguous, and the trace event is written only after a real state change. Add a test with two rounds that reuse an ID.

### 3.2 Findings are sent to roles that cannot fix them (design)

Of the findings that stay open or recur, almost all point at deterministic code:

| Recurring finding | Where the defect is | Can the Generator fix it? |
|---|---|---|
| Year-1 revenue is below the nine months already reported | [valuation_engine.py:424-426](coverageflow/coverageflow/valuation_engine.py#L424-L426) anchors on the last full fiscal year | No |
| FCF base mixes FY2025 and nine-month 2026 | [valuation_engine.py:468-472](coverageflow/coverageflow/valuation_engine.py#L468-L472) | No |
| EBITDA and FCF margins just follow gross margin one for one | [valuation_engine.py:271-272](coverageflow/coverageflow/valuation_engine.py#L271-L272) | No |
| Only three segments carry weight in the growth blend | [valuation_engine.py:509-543](coverageflow/coverageflow/valuation_engine.py#L509-L543) plus missing segment data (section 5.2) | No |
| Segment shares sum to 126% | [model_engine.py:452](coverageflow/coverageflow/model_engine.py#L452) divides by consolidated revenue | No |
| EPS check says "ok" when it does not tie | [model_engine.py:259-265](coverageflow/coverageflow/model_engine.py#L259-L265) tolerance was widened | No |
| Year-over-year sign is wrong on a negative base | [model_engine.py:479](coverageflow/coverageflow/model_engine.py#L479) | No |
| Engine artifacts lack request ID and conformance tag | orchestrator stamping | No |
| Engine version `0.1.0` in the manifest, `@1.0` in the artifacts | [orchestrator.py:1313-1316](coverageflow/coverageflow/orchestrator.py#L1313-L1316) | No |
| Prompt version drift | The cards themselves: `writer.md` is version 1.3 but tells the writer to emit `prompt_version("1.0")` | No |
| Per-role token cap breached | Budget accounting | No |

The model role may only propose segment growth and a gross-margin path ([prompts/model.md](coverageflow/prompts/model.md)). So the writer adds an "Open findings" section saying each one is "addressed", nothing in the artifacts changes, and the critic reports that too.

**Fix.** Three parts:

- Fix the engine and harness defects themselves (sections 5 and 3.4). This removes most of the findings.
- Give every finding an owner when it is opened: `writer`, `model`, `extractor`, `system` (engine, slicer, harness) or `mandate` (human-owned inputs). Only the first three go into the repair packet.
- A `system` or `mandate` finding stops the case straight away with a distinct terminal reason (for example `BLOCKED: needs_system_change`) and becomes the input to an evolution proposal. That is the lecture's split between the research loop and evolution (slides 28-29), and it stops the case spending ten more rounds on something a rewrite cannot fix.

### 3.3 Critics have no way to close a finding with evidence

The orchestrator supports `verified_resolutions` ([orchestrator.py:1999-2035](coverageflow/coverageflow/orchestrator.py#L1999-L2035)), but only the offline stub emits that field. No critic card mentions it, and no critic card asks for a `dedup_key` either. In my live Claude calls both critics returned zero resolutions and `dedup_key: null`.

So in a live run findings close only through [orchestrator.py:1949-1997](coverageflow/coverageflow/orchestrator.py#L1949-L1997): "the critic did not raise it again, so it is fixed". That contradicts lecture slide 19 directly ("Round 2 omits it: the finding stays open. Close only after the Critic verifies the repaired output"). It also means the five evidence runs demonstrate a closure path that live runs never take.

**Fix.** Add to each critic card: for every carried finding, return `verified_resolutions[] {finding_id, verified_evidence {artifact, sha256}, check_performed}` or restate the finding as still open. Remove the close-on-silence rule. Compute the dedup key in code (critic, rubric check ID, affected artifact) instead of asking the model for it.

### 3.4 The critics' job is open-ended, and two of the four check things code should check

- The cards say "challenge" and "emit findings for every failed check", with no definition of major versus minor. Critic C2 produced between 5 and 11 findings on every round, new wording each time. Two consecutive clean reviews from four such critics is not a realistic target.
- C3 (timing and cutoff) returned 60 output tokens in six of seven reviewed rounds: it had nothing to say, because the cutoff is already a gate. Each of those calls still read about 54,000 tokens.
- C4 (risk and controls) checks headers, version pins and token budgets. These are exact properties, and the rubric item on token budgets asks the critic to report something the orchestrator then deletes with a regular expression ([orchestrator.py:178-187](coverageflow/coverageflow/orchestrator.py#L178-L187), [1718-1728](coverageflow/coverageflow/orchestrator.py#L1718-L1728)) under a made-up hash `"budget-accounting"`. The rubric is declared locked, so the controller is quietly overriding the acceptance standard.

**Fix.**

- Turn each rubric area into a numbered checklist where the critic returns pass or fail per item with evidence, and define severity in the rubric: major means a wrong number, an unsupported claim, a cutoff breach or a bounds breach; everything else is minor.
- Move the header, version-pin and budget checks into deterministic gates. Stop sending `dispatch_records` to critics ([orchestrator.py:1793-1815](coverageflow/coverageflow/orchestrator.py#L1793-L1815)) and delete the regular-expression filter.
- Keep four critic reports if you want to match the lecture diagram, but give C3 and C4 a small payload (manifest, headers, input manifest, note) instead of the full ledger. This changes the rubric, so record it as a new rubric version rather than editing version 1.0 in place.

### 3.5 One unmarked number in the note costs a whole round

The evidence-link gate ([gates.py:526-603](coverageflow/coverageflow/gates.py#L526-L603)) needs a marker after every number. When it fails, [orchestrator.py:1433-1440](coverageflow/coverageflow/orchestrator.py#L1433-L1440) issues a repair packet and the next round re-runs model, writer and lead. In the live case rounds 1, 3, 7 and 10 ended this way, four of eleven, without ever reaching review.

The gate and the writer card also disagree. The card says a marker covers numbers back to "the last comma, semicolon, colon, dash or sentence period"; the gate only breaks on semicolons and sentence periods ([gates.py:456](coverageflow/coverageflow/gates.py#L456)). The exemption list has to be extended by hand for each new phrase: "year 1" is exempt but "Year-1" is not, which is what failed in my live test.

**Fix.** Treat a failure of this gate as an invalid writer submission: re-dispatch the writer only, with the gate's messages as feedback, without consuming a round. The mechanism already exists ([orchestrator.py:755-777](coverageflow/coverageflow/orchestrator.py#L755-L777)), and it is what slide 26 prescribes ("Repair invalid submissions at the same role"). Make the card and the gate state the same rule. Better still, change the format so the gate cannot misread prose: have the writer emit claims as structured items (`{text, value, evidence_key, period}`) and let code render the sentence markers.

### 3.6 Every repair is a full rebuild

[orchestrator.py:1748](coverageflow/coverageflow/orchestrator.py#L1748) always requests a "full re-build". Ingest and extraction are reused, but the model role runs every round even when all findings concern `report.md`. Because the model is not deterministic, the forecast changes, the candidate ID changes, and the critics see a different candidate from the one they criticised.

**Fix.** Rebuild only from the earliest stage named in the open findings' `affected_artifacts`, and carry the other artifacts forward byte for byte, as is already done for facts.

### 3.7 A case can be contaminated by the offline stub

`run` without `--backend` falls back to the scripted stub ([cli.py:92-95](coverageflow/coverageflow/cli.py#L92-L95), [358-387](coverageflow/coverageflow/cli.py#L358-L387)) and then overwrites the backend recorded on the case. This happened to your live case at 14:21:51 on 10 November: the stub ran rounds 9 to 12 in one second and took the case to approval. The state was later rolled back by hand, but those 31 stub events are still in `trace.jsonl` next to the live ones. `resume` was fixed for this; `run` was not.

**Fix.** Pin the backend at case creation. Refuse to run a case with a different backend unless an explicit flag is given, and never default to the stub outside tests and the demo launcher.

### 3.8 Escalation turns exhaustion into an approval route

When rounds run out, [orchestrator.py:2322-2361](coverageflow/coverageflow/orchestrator.py#L2322-L2361) moves the case to `PENDING_APPROVAL`, and [orchestrator.py:2145](coverageflow/coverageflow/orchestrator.py#L2145) then allows approval with zero clean reviews. The result file would read "human approved; 0 consecutive clean reviews". Slide 21 says exhaustion never grants acceptance. Having a human decide is reasonable, but it needs its own terminal status (for example `ESCALATED`, with an outcome such as `PUBLISHED_WITH_EXCEPTIONS`), not `APPROVED`.

There is also no stall detection, although slide 21 lists stalling as its own outcome. A simple rule would have stopped your case at round 4: stop when the set of open blocking dedup keys has not shrunk for two rounds.

## 4. Why it is slow and expensive

### 4.1 Measurements

From the live Mistral case: 11 rounds, 99 minutes of active time, 74 live calls, about 1.96 million tokens by the harness's characters-divided-by-four estimate. A reviewed round took 7 to 9 minutes; a round that died at the gate took 3 to 4.

What is in a prompt (round 11, harness estimate):

| Part of the writer prompt | Tokens | Share |
|---|---|---|
| `facts.json` (255 facts, each with a citation object, indented) | 25,550 | 50% |
| `model.json` | 5,630 | 11% |
| Carried findings | 2,890 | 6% |
| Preamble, role card, references | 2,116 | 4% |
| Everything else | 14,585 | 29% |
| Total | 50,771 | |

The model, writer, generator lead and all four critics each receive the full facts ledger.

Live Claude Sonnet calls with those prompts:

| Call | Flags | Input tokens billed | Wall time | Cost |
|---|---|---|---|---|
| Writer | current adapter | 127,658 | 99 s | $0.65 |
| Writer | stripped down | 76,785 | 68 s | $0.40 |
| Critic C2 | current adapter | 132,080 | 138 s | $0.72 |
| Critic C2 | stripped down | 84,388 | 84 s | $0.45 |

Two things follow. The real token count is about 50% higher than the harness estimate, because dense JSON does not average four characters per token. And the current adapter adds about 50,000 tokens to every call, because `claude -p` loads your full Claude Code setup (default system prompt, tools, plugins, connected servers). A reviewed round on Claude with today's code would cost roughly $5 and take 7 to 8 minutes; that figure is my estimate from the two measured roles.

### 4.2 Causes and fixes

| Cause | Fix | Expected effect |
|---|---|---|
| Payload is indented JSON ([_prompt_assembly.py:149-151](coverageflow/coverageflow/_prompt_assembly.py#L149-L151)) | Compact separators; render facts as one line per fact (`field, period, value, slice, locator`) | Facts drop from about 25,500 to about 5,000 estimated tokens |
| Every role gets everything ([orchestrator.py:120-133](coverageflow/coverageflow/orchestrator.py#L120-L133), [930-955](coverageflow/coverageflow/orchestrator.py#L930-L955)) | Role-scoped views: writer gets the periods it reports; C1 gets facts and note; C2 gets model, valuation, forecast and note; C3 and C4 get headers and manifests | Critic prompts fall by half or more |
| The generator lead does nothing | See below | One call fewer per round; it was the largest token consumer at 388,889 |
| `claude -p` loads the whole user environment | See section 6 | About 40% cheaper and 30-40% faster per call (measured) |
| Gate failure consumes a round (3.5) | Writer-only retry | Removes 4 of the 11 rounds in the live case |
| Model role re-runs every round (3.6) | Partial rebuild | Removes one call from most repair rounds |
| Critic lead re-emits every finding (about 5,000 output tokens, 50-80 s) | Merge in code; use the model only to mark duplicates, or not at all | Removes most of that minute |

**The generator lead.** It receives about 55,000 tokens, is asked to echo artifact hashes the orchestrator already computed, has its echo checked against those hashes ([orchestrator.py:1275-1284](coverageflow/coverageflow/orchestrator.py#L1275-L1284)), and then has its header fields overwritten ([orchestrator.py:1286-1319](coverageflow/coverageflow/orchestrator.py#L1286-L1319)). It cannot add information. Either build the manifest in code and say so in the design note (the requirement asks you to use deterministic code where appropriate and to separate agent decisions from controller execution), or give the lead a real task on a small payload: a disposition table stating, for each carried finding, which artifact changed and why.

With these changes a reviewed round should need five or six calls of 10,000 to 30,000 tokens instead of eight calls of 80,000 to 130,000. I have not measured the combined effect.

## 5. Why the output is weak

### 5.1 The valuation engine is not a defensible DCF

The latest candidate shows the problems plainly: base revenue is FY2025 (52,853) although nine months of FY2026 already total 46,218; year-1 revenue is forecast at 52,384, below the current run-rate of about 61,600; FCF margin jumps from 6.4% to 14.7% in year 1 only because the gross-margin path starts above the FY2025 base.

What to change in [valuation_engine.py](coverageflow/coverageflow/valuation_engine.py):

- **One base period.** Use the last twelve months (last fiscal year, plus current year to date, minus prior year to date) for revenue, EBITDA and FCF. Delete the special case that swaps in a different FCF period.
- **A real cash-flow build.** Revenue, then EBITDA margin, depreciation, tax at the mandate rate, capital expenditure and working capital. The mandate already carries `tax_rate: 0.15`; the engine never reads it. Let the model role propose the operating-margin and capex-to-revenue paths, within mandate bounds, so there is a driver for each line a critic will question.
- **Discount from the valuation date.** The first forecast year is discounted a full year from a 30 October cutoff.
- **Reconcile the two terminal values in code.** Report the exit multiple implied by the Gordon value and fail a gate when the two differ by more than a mandate tolerance. In earlier rounds they differed by 27 times.
- **No hardcoded company.** `SEGMENT_SLUGS` and the alias table name Intel's segments in both engines and in `prompts/model.md`. Read segments from the mapping table for the ticker. As written, the AMD bundle cannot produce a segment blend, so it cannot serve as the held-out case the design note describes.
- Bridge from enterprise to equity value should subtract non-controlling interests (the Claude critic cites 16,642 for this case; I did not verify the figure).

In [model_engine.py](coverageflow/coverageflow/model_engine.py): compute segment share against the sum of segments, mark growth on a negative base as not meaningful, compute quarter-on-quarter series, and tie EPS using net income attributable to the company rather than widening the tolerance.

### 5.2 The slicer keeps one segment table out of four

[slicer.py:309-339](coverageflow/coverageflow/slicer.py#L309-L339) selects a single best table per statement. The 10-Q segment note has four tables (quarter and nine months, current and prior year); the slice is 549 bytes and holds only the prior-year nine-month table. The note therefore compares nine months of 2025 against full-year 2024 and says "no FY2026 segment revenue exists in the bundle", which is false. The MD&A slice is 550 bytes, so the writer has no management commentary for the Outlook section.

**Fix.** For segments, keep every table between the note heading and the next note. Give the MD&A a useful length. Add a gate that the periods in the extracted facts include the trigger filing's current period for every statement.

### 5.3 The note is not what an analyst would publish

- The mandate has no market price and an empty peer list, so there is no upside, no target and no rating. Make these required mandate fields for a live case, and have the engine produce a target price and the comparison with comparable companies.
- The published note contains internal process text: an "Open findings" section that discusses a "per-role token cap" and "harness dispatch-accounting". Keep the finding dispositions in a separate artifact.
- The `[EV:…]` markers stay in the reader-facing text. Keep the marked version as the verified record and render a clean version (footnotes or a source table) for export.
- A recurring coverage note should say what changed since the last published version: estimate changes, and old versus new target. The prior note is in the writer's declared read scope but is not passed in.
- "Interest and other, net" is filed under `income_statement.interest_expense`, so the note cites a 12,184 "non-operating charge" to an interest-expense key. Add a field to the schema for it.
- Units are missing in the body text.

`out/INTC-fy2026q3-note.md`, committed in `2da73f8`, is the round-11 candidate. That case is still `RUNNING` with 11 open major findings and no approval, so the repository currently presents an unapproved draft as an output.

## 6. Claude and Codex backends

You said the submission should work with the `claude` or `codex` command-line tools. Both adapters in [cli_backends.py](coverageflow/coverageflow/backends/cli_backends.py) need work.

**Codex: every call fails.** The adapter looks for `item.item_type == "agent_message"` ([cli_backends.py:383-389](coverageflow/coverageflow/backends/cli_backends.py#L383-L389)). The installed codex-cli 0.155 emits `item.type`. I ran the adapter's exact command: the model answered correctly and the adapter returned "no agent message found". Other problems:

- It refuses to run outside a git repository; add `--skip-git-repo-check`.
- It runs as a full agent with a writable sandbox; add `--sandbox read-only`.
- It writes memories between calls, which undermines the "fresh context" claim; add `--ephemeral` and `--ignore-user-config`.
- A trivial prompt consumed between 18,000 and 48,000 input tokens of agent overhead.
- Use `--output-schema` so the reply is checked against the artifact schema at source.

**Claude: works, but the accounting is wrong and the call is not isolated.**

- Token accounting. The CLI reports the prompt under `cache_creation_input_tokens`; `_extract_usage` ([cli_backends.py:54-66](coverageflow/coverageflow/backends/cli_backends.py#L54-L66)) reads only `input_tokens`, which is 2. The adapter recorded `tokens_in=2` in my test. The per-role cap and the per-call cap ([dispatch.py:199-213](coverageflow/coverageflow/dispatch.py#L199-L213)) will therefore never fire on Claude. Sum the three input fields, and record `total_cost_usd`, which the CLI returns and the adapter discards.
- The prompt is passed as a command-line argument ([cli_backends.py:304-311](coverageflow/coverageflow/backends/cli_backends.py#L304-L311)). At about 200 KB this works on macOS but exceeds Linux's 128 KB per-argument limit, so it would fail on a Linux grader's machine. Pipe it through standard input.
- No tool restriction and no isolation. The call inherits your tools, plugins, connected servers and memory. The Mistral adapter replaces the system prompt and disables tools; the Claude adapter does neither.

The flags I used for the stripped-down calls, all present in your installed version 2.1.308:

```
claude -p --model sonnet --output-format json \
  --tools "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --setting-sources "" --disable-slash-commands --no-session-persistence \
  --system-prompt "<single-shot JSON emitter text>"  < prompt.md
```

Also worth adding: `--json-schema` for the artifact schema and `--max-budget-usd` as a hard per-call spending limit. One caution from the test: with the stripped-down flags the writer's note failed the evidence gate on four numbers, while the current-flags note passed. That is one sample each, so I would not read a quality difference into it, but it shows why the gate needs the writer-only retry from 3.5.

Both adapters hardcode the model name. Make it a mandate or configuration value and record it in the trace, since slide 32 asks for the model to be recorded with each version.

## 7. Gaps against the requirements and the lecture

| Rule | Status | What to do |
|---|---|---|
| Slide 19: a finding closes only after the critic verifies the repair | Not met: closed when not repeated (3.3) | Evidence-based closure only |
| Slide 20: all critics accept, no open findings, two clean rounds | Changed to "no blocking findings"; critics may still return `findings` | Either restore it with calibrated critics (3.4), or keep the change and state it in the design note with the reason |
| Slide 21: exhaustion never grants acceptance; stalling is distinct | Escalation leads to `APPROVED`; no stall rule (3.8) | Separate terminal status; add stall rule |
| Slide 24: conflicting findings stay explicit, nothing silently overwritten | ID collisions hide rows; phantom closures in the trace (3.1) | Ledger fix |
| Slide 26: repair an invalid submission at the same role | Gate failure costs a round (3.5) | Writer-only retry |
| Slides 12 and Requirement 2: limits on cost and tool permissions enforced outside the prompt | No cost ledger; Claude and Codex run with default tools | Section 6 |
| Slides 37 and 40: show live dispatch records; label rehearsals | All five evidence runs are scripted (and labelled so). No successful live run exists. Prompts and replies are stored only as hashes unless `--record` is used | After the fixes, run one live case to approval with `--record` and commit it as a sixth evidence run |
| Slides 29-32, 52: evolution runs regressions against fixed cases; held-out cases | `evolve regress` records a hand-written JSON file ([cli.py:539-553](coverageflow/coverageflow/cli.py#L539-L553)); there is no runner and no development or held-out corpus, although the design note describes "4 development cases across two companies; 2 held-out" | Build a minimal runner over recorded replay cases, or rewrite that paragraph as "designed, not built" |
| Slide 53: existing cases keep their version | The registry's active pointer is not read by dispatch; prompts always come from `prompts/<role>.md` at the current checkout. The live case's state says every card is version 1.0 while versions 1.1 and 1.3 were dispatched | Store card versions and hashes in the case at creation; refuse to dispatch a different hash |
| Slide 18: mandate and acceptance rules fixed during revision | The live case's limits were raised by grants from 4 rounds to 11 and from 60,000 to 500,000 tokens per role | Grants are recorded, which is good. Set realistic defaults so a normal case needs none |
| Requirement 3: README accurate | Says 298 tests; there are 542. Says evidence is reproducible; candidate IDs no longer match current code | Update the count; regenerate the five runs |
| Requirement 3: own contribution | Present, but says the work was "built by parallel agent streams" | Add two or three sentences on the decisions you made yourself |
| Requirement 3: no secrets in files or history | Met: I searched the full history and found none | None |
| Requirement 3: ZIP of the final state | `25210660138-final-project.zip` is dated 10 November 01:35, before 8 later commits | Rebuild after the final commit |

Slide 54 asks what has been tested and what remains to be built. The README's limitations list is honest; the design note's evolution paragraph is the one place that claims more than the code does.

## 8. Fix plan, in order

Each step is small enough to commit on its own, which also helps the "meaningful commits" requirement.

**Step 1: make convergence possible (about half a day).**
1. Ledger: unique IDs, strict close, no phantom trace events (3.1).
2. Critic cards: `verified_resolutions`, checklists with pass or fail, severity definitions; remove close-on-silence (3.3, 3.4).
3. Evidence gate failure becomes a writer-only retry; align card and gate (3.5).
4. Pin the backend per case; no stub fallback (3.7).
5. Stall rule and a separate status for escalation (3.8).

**Step 2: make the backends correct (two to three hours).**
6. Codex parser and flags; Claude token and cost accounting, standard input, isolation flags (section 6).
7. Cost limit in the budget tracker next to tokens and time.

**Step 3: cut the cost per round (about half a day).**
8. Compact, role-scoped payloads; facts as rows (4.2).
9. Replace or shrink the generator lead; merge critic reports in code.
10. Header, version and budget checks as gates; small payloads for C3 and C4.
11. Partial rebuild from the earliest affected stage (3.6).

**Step 4: fix what the critics were right about (one day).**
12. Valuation engine: single base period, real cash-flow build, discount timing, terminal reconciliation gate, segments from the mapping table (5.1).
13. Model engine checks; slicer segment and MD&A coverage (5.1, 5.2).
14. Finding owners and the `needs_system_change` stop (3.2).
15. Mandate with market price and peers; clean export without markers or process text; change-since-last-note section (5.3).

**Step 5: evidence and documents (half a day).**
16. Start a new case. Do not continue `intc-fy2026q3-20261030`: its trace mixes stub and live events and its state was edited by hand.
17. Run it live on Claude with `--record` to human approval; add it as evidence run 6. Run AMD as the held-out case.
18. Regenerate the five scripted runs; update the README test count; correct the evolution paragraph or build the minimal regression runner.
19. Remove `out/` from the repository or replace it with the approved note; rebuild the ZIP.

If time is short, steps 1 and 2 plus items 12 and 16-19 are the minimum: without step 1 no live case can finish, without step 2 neither target backend is trustworthy, and without a working valuation base period critic C2 will keep blocking for good reason.

## 9. What I am less sure of

- I did not review `ui/` beyond how it starts runs, and I did not test the launcher.
- I did not check the extracted facts against the filings line by line. The segment and "interest and other" problems came out of the critics' findings and the slices; there may be more.
- The cost and time per round on Claude are extrapolated from two roles. The figures per call are measured.
- Whether two consecutive clean reviews become reachable after steps 1-4 can only be shown by running it. If it still stalls, the next lever is to make the second review a deterministic check that the candidate hash is unchanged plus a single confirming critic, and to say so in the design note.
