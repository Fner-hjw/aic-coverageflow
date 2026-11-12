# run6 - live AMD cases (Codex backend)

Curated by `scripts/curate_live_evidence.py` from the live workspace (the
workspace itself, with 2 MB filings per case, is not committed; the filings
are `data/amd-bundle`). Narrative and measurements: `docs/live-run-hardening.md`.

| Folder | What it shows |
|---|---|
| `a-before-mandate-inconsistent` | four rounds, terminal ratio stuck at 3.24, EXHAUSTED; findings routed to a role that cannot fix them |
| `b-preflight-blocks-before-round-1` | same mandate after fix 1: `needs_mandate_change` before any role runs (`evolution-input.json`, 0 tokens) |
| `c-wacc10-four-rounds-exhausted` | mandate corrected (WACC 10%); gate passes; rounds lost to writer retries and unciteable price |
| `d-escalated-after-fixes-1` | reached critic review; stalled on two writer-owned findings; ESCALATED; draft export in `export/` |
| `e-final-escalated` | all fixes in; every numerical gate passes; one blocking critic finding left; ESCALATED for human review |

Per case: `mandate.json`, `state.json`, `result.json`, `findings.json` (the
ledger with owners), `trace.jsonl`, `rounds/round-N/numerical/gates.json`,
critic reports, and `last-candidate/` (note, model, valuation, forecast,
facts, manifest). `summary.json` has the headline numbers.

Note on the AMD case after `e`: in the live workspace the case was later
closed as `PUBLISHED_WITH_EXCEPTIONS` over `fnd-0007` (a C1.2 cited-value
finding) with the reasoning "Approved.". That predates two rules: reasoning of
at least 15 characters, and non-waivable findings (C1.1-C1.3, C2.1-C2.2,
C3.1-C3.2 and gate findings). Under today's rules the publication would be
refused; the curated `e` folder here still ends at `ESCALATED` and is
unchanged. See `docs/live-run-hardening.md`.

Reproduce (needs a logged-in `codex` CLI; spends model usage):

```
python -m coverageflow case new --ticker AMD --period fy2026q3 \
  --trigger-doc amd-10q-2026q3 --cutoff 2026-11-04 \
  --bundle data/amd-bundle \
  --mandate examples/run6-amd-live/e-final-escalated/mandate.json --backend codex
python -m coverageflow run --case amd-fy2026q3-20261104
```
