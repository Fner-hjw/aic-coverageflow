# Live-run hardening (second phase)

The first phase built the controller and proved it offline (`examples/run1`-`run5`).
This phase ran it against **real AMD filings with a live backend (Codex CLI)**
and fixed what that exposed. Every change is a commit in the history
(`git log 1c2b632..`); the live cases are curated in `examples/run6-amd-live/`.

## The task that was run

AMD fiscal 2026 Q3 coverage update: trigger `amd-10q-2026q3` (filed
2026-11-03), annual anchor `amd-10k-fy2025`, information cutoff 2026-11-04,
frozen bundle `data/amd-bundle`. Roles ran on Codex (GPT-5-class "Luna");
engines are deterministic code. The mandate (WACC, terminal growth, exit
multiple, price, peers) is a human-owned input.

## What the live runs showed

| # | Case (in `examples/run6-amd-live/`) | Outcome | Rounds | Wall time | Model calls | Tokens |
|---|---|---|---|---|---|---|
| a | `a-before-mandate-inconsistent` | EXHAUSTED | 4 | 13.0 min* | 22 | 0.89 M |
| b | `b-preflight-blocks-before-round-1` | BLOCKED `needs_mandate_change` | 0 | 0 | 0 | 0 |
| c | `c-wacc10-four-rounds-exhausted` | EXHAUSTED | 4 | 16.5 min | 27 | 1.14 M |
| d | `d-escalated-after-fixes-1` | ESCALATED (stall) | 3 | 16.8 min | 33 | 1.45 M |
| e | `e-final-escalated` | ESCALATED | 4 | 18.5 min | 37 | 1.38 M |

\* case (a) recorded `0.0` minutes; 13.0 is reconstructed from the trace
(the clock bug is fix 7 below). All five cases ran under `rounds_max = 4`
because the reused mandate file carries an explicit `limits` block; a mandate
created by `case new` now gets 6.

Case (a) burned four rounds on one finding. The terminal-value ratio stayed at
3.24 in every round because it is set by the *mandate* (exit multiple 21.5x
against a 13.5% WACC), which no role may change; the finding was routed to the
model role. Re-running with WACC 10% (a human decision) cleared that gate in
every later run, and the cases then failed for smaller reasons, one at a time,
which is what the rest of this record is about.

## Changes, in the order the evidence forced them

Each row: the symptom in a live trace, the cause, the change, and the test.
Tests are in `tests/test_loop_economics.py` unless noted.

| # | Symptom | Cause | Change |
|---|---|---|---|
| 1 | Four rounds on a finding no role can fix (case a) | finding owned by `model`; mandate inputs set the ratio | `gates.terminal_feasibility`: from WACC, growth, tax and exit multiple, the lowest ratio any forecast can reach is known before round 1; if it exceeds the gate limit the case ends `needs_mandate_change` and names the largest exit multiple that would pass (case b, zero tokens). A finding that returns unchanged after its owner's repair round is reassigned to `mandate` (`ledger.reassign_owner`). |
| 2 | Each writer retry fixed two numbers and left new ones | a retry resamples the whole note | patch-only retries: previous draft + flagged lines in; only diff blocks that touch flagged lines are kept (header always the new one) |
| 3 | Last round had no retries left for four unmarked numbers | retry budget counted per *case* | per-round budget `writer:report_evidence_links:rN` |
| 4 | Round 2 failed three critic findings on "USD 488.78" | the price existed only in the mandate; no key to cite | `dcf.market_price`, `dcf.market_price_as_of` in `valuation.json` |
| 5 | "2.5% terminal growth" cited to terminal-value keys; forecast claims "unsupported" | the gate checked that a marker exists, not that the key holds the number; critic C1 could not read `forecast_assumptions.json` | cited-value check (flags a number when a leaf key the sentence is about holds it and the cited keys do not); `forecast.*` citable; C1 reads the forecast |
| 6 | 75 s writer calls for a few missing markers | every gap cost a full model call | `autolink_numbers`: bind a number to the one key that holds its value and whose name the sentence uses, line by line, only if every unlinked number on the line can be placed (replay over recorded notes: 25 of 44 single missing markers restored); strict `--output-schema` for writer and extractor units |
| 7 | `elapsed_minutes = 0.0` after 13 min; timer kept running on a stopped case | clock only booked on pause; UI ticked for BLOCKED/EXHAUSTED | `_settle_clock` on BLOCKED/EXHAUSTED; UI/service clock only for RUNNING |
| 8 | Two of ten extraction units re-requested (+47 s) | `?` value; same row twice | `?`/`n/a` become `missing`; identical duplicates collapse |
| 9 | 52 s / 44k tokens for the ingest report | ingest is a verbatim copy of slicer output | built in code when the slicer finds an income statement for every scoped filing (reproduces the model's report exactly on the AMD filings); role used otherwise |
| 10 | Rounds spent on mechanical gate failures | one round counter | `mechanical_rounds_max` (4): a writer-only rebuild for gate findings is not a review round; `rounds_max` default 4 -> 6 |
| 11 | Escalated case could not be inspected | export required an approved state | ESCALATED/EXHAUSTED/BLOCKED/STOPPED export as clearly marked **unapproved drafts** |
| 12 | UI restart reset the chosen backend; token table showed 547k / 250k | in-memory selection vs launcher default; hard-coded cap | selection saved in the workspace; table shows each role's heaviest case against the cap it really has |
| 13 | AMD note published with "USD 6,697mm" free cash flow cited to capex and "USD 145.52" cited to `comps.own_band` (fnd-0007) | the cited-value check of row 5 fired only when the key name shared a word with the sentence; "fcf" and "comps_price" did not | word overlap now only ranks the suggestion; with no overlap the number is still flagged when at most 3 leaf keys hold it or it is distinctive (2+ decimals or >= 1000); small alias map (fcf = free cash flow, price = per share) |

## What still stops a clean approval

Case (e), the last live run, passed every numerical gate and reached the
review stage in all four rounds, but ended `ESCALATED` with one blocking
critic finding (`fnd-0007`: a cited value does not equal the cited ledger
entry) and two minor header findings. It was **not approved**; the case is the
human gate's to adjudicate, and the draft export exists for that.

Measured, not fixed:

- The writer is still the cost: 12 of 37 calls and ~0.6 M of 1.4 M tokens,
  ~75 s per call whatever the input size. Auto-linking and the cited-value
  check removed many retries but the writer's first draft still leaves some
  numbers unlinked in most rounds.
- Wall time did not fall (18.5 min vs 13.0 min for case a) because case a
  never reached review; cases c-e include critic rounds, which cost 40-70 s each
  in parallel.
- Ingest now costs 0 tokens and the extractor second wave is gone (8 calls,
  one wave).
- Model and critic outputs have no decode-time schema yet; none failed in
  these runs.

## How a change was tested

Every behavioural change has a unit test and was replayed against the
recorded live notes before being trusted (for example the cited-value check
reproduces exactly the two terminal-assumption findings the round-3 critic
raised). The full offline suite (976 tests) and the five offline evidence
runs are rerun on a clean clone before submission.
