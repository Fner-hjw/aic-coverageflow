# Evidence runs (Requirement 2, bullet 3)

Five self-contained evidence runs, all **produced by actually running the
controller offline** (offline replay/stub dispatch + the real deterministic
engines) against the frozen Intel bundle (`data/intel-bundle`, 12 hashed
EDGAR filings, information cutoff **2026-10-30**). Each folder is a complete
workflow workspace (`cases/`, plus `versions/` where exercised) with the CLI
captured output (`out-*.txt`) and a `commands.sh` that reproduces the run
end-to-end.

> In stub/replay mode the four Generator specialists and four Critics return
> recorded, gate-satisfying responses that mirror the hand-computed mini-10-Q
> fixture used by the test suite; the **numbers in `model.json` and
> `valuation.json` are computed by the real deterministic engines**
> (`model_engine@1.0`, `valuation_engine@1.0`). Live backends (claude/codex/
> kimi CLI, or any OpenAI-compatible API) are selected per dispatch by
> configuration — the controller does not change. This is the same
> "scripted rehearsal" surface the web UI exposes in its demo panel.

Reproduce any run from the **repository root**:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
bash examples/run1-clean/commands.sh
```

## The five runs — what to look for

| Run | Requirement demonstrated | Key evidence |
|---|---|---|
| `run1-clean` | Clean end-to-end coverage update | full trace (`trace.jsonl`): 7 `GATE_PASS` events, 2 clean full critic reviews, `APPROVAL_REQUESTED` → `APPROVAL_RECORDED`, `result.json` terminal APPROVED, exported `dossier.pdf` |
| `run2-seeded-repair` | Seeded finding → repair loop → critic-verified closure | ledger `findings.json` (`fnd-001` closed with artifact+SHA-256 evidence), 4 × `FINDING_VERIFIED_CLOSED` in the trace, acceptance still requires 2 clean reviews |
| `run3-kill-resume` | Kill mid-run → verified resume | dangling dispatch in the trace (no `AGENT_RESULT`), torn partial moved to `discarded/`, **only the pending role re-dispatched** (see `out-evidence-check.txt`) |
| `run4-perturbation` | Cutoff / no-leakage | `demo perturb` twice: clean manifest passes; post-cutoff injection **rejected by the gate**; outputs byte-identical across the perturbation (`out-stability-check.txt`) |
| `run5-evolution` | Evolution: 3-part demo | version registry: scoped change approved (active pointer swap), over-broad proposal **refused + REJECTED** (scoped gates), authorized rollback + verification case under the restored version |

## Run details

### run1-clean — clean coverage update
`commands.sh` = `case new` (12-doc Intel manifest, trigger `intc-10q-2026q3`,
cutoff 2026-10-30) → `run` (build → 7 gates → 4 critics → merge →
2 clean rounds → `PENDING_APPROVAL`) → `approve --by "Gaetano Marco Chiantia"`
→ `gates` snapshot → dossier export
(`python -m coverageflow.export_dossier`, writes `export/dossier.tex` +
`dossier.pdf` from the verified artifacts; the 2-page PDF committed in `run1-clean` was produced before the current 5-page note template, and `commands.sh` regenerates it with the current one). The case directory contains the
complete evidence trail: `trace.jsonl` (96 events), `state.json` with the
checkpoints, `approvals/apr-0001.json`, `report/v1-cand-43f.md`,
engine artifacts per round, and `history/checkpoint-*.json`.

### run2-seeded-repair — finding → critic-verified closure
Same as run1 plus `demo seed-finding` before the run: an open finding
("gross margin driver narrative cites stale segment table") lands in the
ledger and is **carried into every critic context**. Look at:
`findings.json` — `fnd-001` closed by `critic_c1`, `verified_evidence`
= `rounds/round-001/generator/report.md` + its real SHA-256;
the trace shows `FINDING_VERIFIED_CLOSED` from all four critics
(a finding closes only on artifact-hash-matching evidence; the tampered-
evidence rejection path is unit-tested in `tests/test_orchestrator.py`).

### run3-kill-resume — kill mid-run, resume of pending work only
`kill_during_run.py` runs the real Orchestrator with a dispatch that dies
during the **extractor** role (trace keeps the dangling `req-0002`:
`REQUEST_BOUND` + `AGENT_DISPATCHED` with no `AGENT_RESULT`); the CLI demo
command `demo kill-stage --stage extractor` then writes the torn partial a
real crash leaves on disk; `resume` discards the partial to
`rounds/round-001/generator/discarded/`, re-bound + re-dispatches **only the
extractor** (`out-evidence-check.txt`: ingest/model/writer/lead each
dispatched once, extractor twice, `ALL_CHECKS_PASS: true`), and the case
still reaches acceptance + approval without repeating completed work.

### run4-perturbation — cutoff / no-leakage
Baseline case (like run1) + hashes snapshot; `demo perturb` asserts (a) the
gate engine recomputes deterministically (two gate reports byte-equal) and
(b) no manifest document is dated after the cutoff. Then a deliberately
post-cutoff 8-K (filed **2026-11-05**) is injected into the input manifest
(rehearsal only; `--restore` returns the frozen manifest byte-for-byte):
the run output `out-perturb-leak.txt` shows the leaking document flagged and
a non-zero exit code. After restore, `check_stability.py` shows **all 7
published artifacts byte-identical** before/after the perturbation — a
post-cutoff document can never reach the outputs.

### run5-evolution — approve / scope-reject / rollback
Three registry cycles over `versions/registry.json` (all CLI):
1. **5a pass+approve**: `evolve propose sys-1.1` (scoped: extractor prompt card) with recorded regression results (identical outputs on the 4 development cases) → `evolve regress` → `evolve approve --decision APPROVED` → active pointer = sys-1.1.
2. **5b scope-reject**: `sys-2.0` proposes relaxing the acceptance rule and gate thresholds — i.e. it touches **locked controller scope** and fails regression; `evolve approve --decision APPROVED` is **refused by the registry gate** (exit 1: "cannot approve a version whose fresh-critic review did not pass all regressions"); the human decision recorded on file is REJECTED and the active version stays sys-1.1. In a live loop the same record carries the actual diff hashes and case evidence.
3. **5c rollback+verify**: `sys-1.2` (scoped slicer change) is approved (active = sys-1.2), then `evolve rollback --version sys-1.1` restores the prior pointer; a **verification case** created after the rollback pins the rolled-back configuration (`state.json` `config_versions`: extractor 1.1, engines 1.0) and runs clean through acceptance.

## Size note

The five workspaces total well under 5 MB. The 12 Intel `.htm` filings
(16 MB) are **not** duplicated into the runs: every case records the frozen
bundle's document IDs + SHA-256 hashes in `inputs/manifest.json`
(`inputs-manifest.json` in each folder is the exact manifest passed at
`case new`, trigger document first).

## Live evidence

- `run6-amd-live/`: curated records of the five live Codex cases on AMD
  (see its README and `docs/live-run-hardening.md`).
- `showcase/cases/amd-fy2026q3r3-20261104/`: one finished live case kept
  whole (state, checkpoints, trace, findings ledger, every round, approval
  record, exports), so it can be opened in the UI. The AMD filings are not
  duplicated here; `python3 launch.py` copies the case into the workspace and
  restores them from `data/amd-bundle`, checking each against the case's
  `inputs/manifest.json` hash. The case was published with exceptions over
  fnd-0007 before the reasoning and non-waivable rules existed; today that
  decision would be refused.
