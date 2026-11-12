# CoverageFlow

**Long-running agent workflow for maintaining sell-side equity coverage** —
AI Coding in Finance (Fall 2026) final project.

**Author:** Gaetano Marco Chiantia — **Student ID:** 25210660138

CoverageFlow turns dated, frozen, SHA-256-hashed SEC-filing bundles plus an
information cutoff into updated, independently verified, human-approved,
versioned research notes for a covered company (demo: Intel `INTC`; AMD as a
held-out second bundle). New dated documents trigger staged rebuilds —
ingest → extraction → model → valuation → report — under a
Generator/Critic revision loop with deterministic numerical gates,
checkpointed state (kill any process, resume without repeating completed
work), budget control, a finding ledger closed only by critic-verified
repairs, a human approval gate before anything is published, and an
evolution system that regression-tests changes to the system itself before
they can become active.

| Deliverable | Where |
|---|---|
| Requirement 1 — task spec | `docs/task-spec.md`, `docs/design-decisions.md` (locked design record) |
| Requirement 2 — design of the long-running system | **`docs/design-note.md`** (graded design document, embeds `docs/workflow-diagram.png`, maps every element to the lecture concepts) |
| Requirement 2 — evidence runs | **`examples/`** — five reproduced offline runs (clean update / seeded repair / kill+resume / no-leakage perturbation / evolution) and **`examples/run6-amd-live/`**, the live Codex runs on AMD that drove the second hardening phase |
| Live-run hardening (what broke on real filings, what was changed, measured before/after) | **`docs/live-run-hardening.md`**, with the earlier loop diagnosis in `docs/diagnosis-2026-11-11.md` |
| Frozen contracts | `docs/contracts/state-and-artifacts.md`, `docs/contracts/interfaces.md` |

## Repository guide

| Path | Owner stream | What it is |
|---|---|---|
| `coverageflow/orchestrator.py` | A2 | the state machine: build/gates/critics/merge/repair loop, budgets, acceptance, approval gate (pure Python, no LLM) |
| `coverageflow/budget.py`, `statestore.py`, `ledger.py`, `resume.py`, `registry.py` | A2 | budgets; atomic JSON+JSONL writes with hashes; append-only finding ledger; verified resume planner; version registry |
| `coverageflow/gates.py`, `cli.py` | A2 | the 7 numerical gates; the controller CLI (`python -m coverageflow …`) |
| `coverageflow/dispatch.py`, `_prompt_assembly.py` | A3 | RoleCall envelope, prompt assembly (preamble + role card + pinned references + bound request), strict output parsing, budget caps |
| `coverageflow/backends/` | A3 | backend adapters: `claude`, `codex`, `kimi` headless CLIs, OpenAI-compatible `api`, `replay` recordings (+ recording wrapper) |
| `coverageflow/bundle.py`, `slicer.py`, `model_engine.py`, `valuation_engine.py`, `export_xlsx.py` | A1 | EDGAR bundle snapshot/hashing; deterministic document slicing; model engine (canonical series, ~80 fields); valuation engine (DCF + comps + sensitivity); institutional 5-tab xlsx exporter |
| `coverageflow/export_dossier.py` | **A5 (this stream)** | LaTeX/PDF dossier renderer from the verified artifacts (template: `references/dossier-template.tex`) |
| `ui/` (api, ws, service, static SPA) | A4 | FastAPI service + no-build vanilla-JS dashboard (run control, trace, findings, approval dossier, evolution, demo panel) |
| `prompts/`, `references/` | A3/A1 | versioned role cards (ingest, extractor, model, writer, critics, leads); pinned references (harness preamble, critic rubric, model schema, segment mapping v1) |
| `data/intel-bundle/`, `data/amd-bundle/` | A1 | frozen EDGAR snapshots (12 Intel docs; 10 AMD docs) with manifest + SHA-256 |
| `examples/` | **A5** | the five evidence runs, each self-contained and reproducible (`examples/README.md`) |
| `docs/` | coordinator + **A5** | task spec, locked design record, graded design note, workflow diagram, frozen contracts |
| `scripts/fetch_demo_bundles.py` | A1 | bundle builder for any new company |
| `scripts/gen_diagram.py` | **A5** | regenerates `docs/workflow-diagram.png` (pure matplotlib) |
| `tests/` | all streams | 976 tests, fully offline (replay/stub + unit fixtures) |
| `cases/`, `versions/` | — | runtime workspaces (created at run time; the evidence copies live under `examples/`) |

## Quick start (one command)

```bash
python3 launch.py
```

The launcher does everything: creates the virtualenv and installs
dependencies on first run, asks **once** which backend role dispatch should
use — **offline demo** (no account, no keys, everything runs for real
except the model calls, which are canned), an **OpenAI-compatible API**
(Mistral, OpenAI, …), or a **headless CLI** logged in on the machine
(`claude` / `codex` / `kimi` / `vibe` — the Mistral CLI) — creates a
workspace, seeds a ready-to-run Intel demo case, starts the web UI and
opens it. Answers are saved to `.coverageflow-launch.json`
(`--configure` to redo them; API credentials land in
`.coverageflow.json`, which is gitignored). Stop with `Ctrl+C`.

Flags: `--port N`, `--no-browser`, `--yes` (accept all defaults),
`--configure`.

## Setup

Python 3.11+ (stdlib-first codebase). Everything runs offline — the entire
test suite and all evidence runs need no network access.

```bash
cd coverageflow            # the repository root
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# run things through the package:
.venv/bin/python -m coverageflow --help
```

`requirements.txt`: `pytest`, `openpyxl`, `fastapi`, `uvicorn`, `httpx`,
`websockets` (+ `matplotlib`, only used by `scripts/gen_diagram.py`).
pdflatex (TeX Live 2026) is optional — it compiles `dossier.tex` into
`dossier.pdf`; without it the `.tex` is the deliverable.

## Running the workflow (CLI)

All controller actions go through the orchestrator — the CLI never writes
state files directly. Run from the repository root:

```bash
# 1) create a case from the frozen Intel bundle (cutoff = filing date)
.venv/bin/python -m coverageflow --workspace examples/run1-clean case new \
    --ticker INTC --period fy2026q3 --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --manifest examples/run1-clean/inputs-manifest.json

# 2) run the loop: build -> gates -> 4 fresh critics -> merge -> acceptance
.venv/bin/python -m coverageflow --workspace examples/run1-clean run --case intc-fy2026q3-20261030

# 3) the human gate: the only route to publication
.venv/bin/python -m coverageflow --workspace examples/run1-clean approve --case intc-fy2026q3-20261030 \
    --decision APPROVED --by "Gaetano Marco Chiantia" --reason "Quarterly update verified."

# decisions: APPROVED | REJECTED | PUBLISHED_WITH_EXCEPTIONS (ESCALATED cases) | REVISION_REQUESTED.
# REJECTED, PUBLISHED_WITH_EXCEPTIONS and REVISION_REQUESTED need --reason of at least 15
# characters. REVISION_REQUESTED sends an ESCALATED or PENDING_APPROVAL case back to RUNNING with
# your objections as human findings (OWNER[/SEVERITY]:SUMMARY, owner writer|model|extractor), 2 extra
# rounds, and at most 2 requests per case; then `run` continues the same case:
.venv/bin/python -m coverageflow --workspace examples/run1-clean approve --case intc-fy2026q3-20261030 \
    --decision REVISION_REQUESTED --by "Gaetano Marco Chiantia" \
    --reason "Valuation paragraph ignores the buyback; the critics missed it." \
    --finding "writer:Valuation ignores the Q3 buyback"

# other verbs: case list | resume | gates | demo seed-finding|kill-stage|perturb |
# evolve prepare|propose|regress|approve|rollback | backends | export-dossier:
.venv/bin/python -m coverageflow --workspace examples/run1-clean gates --case intc-fy2026q3-20261030
.venv/bin/python -m coverageflow.export_dossier --case examples/run1-clean/cases/intc-fy2026q3-20261030
```

Each case leaves a complete evidence trail: `cases/<case_id>/trace.jsonl`
(fixed event vocabulary), `state.json` + `history/checkpoint-*.json`,
`findings.json`, per-round generator/critic/numerical artifacts,
`approvals/`, `report/`, `export/`.

### The five evidence runs (walkthrough)

All five were produced by actually running the system offline (offline
replay/stub dispatch + the real deterministic engines) and are committed
with their outputs. **Start at `examples/README.md`**; reproduce any of them
with its `commands.sh`:

1. **run1 — clean end-to-end update** (`examples/run1-clean`): create → run (7 gates pass, 4 critics clean, 2 clean full reviews of the same candidate) → `PENDING_APPROVAL` → human approval → `CLOSED`, plus the LaTeX dossier export (`export/dossier.pdf`).
2. **run2 — seeded finding → critic-verified closure** (`examples/run2-seeded-repair`): `demo seed-finding` opens `fnd-001`; the critic contexts carry it; it closes only against artifact+SHA-256 evidence (`FINDING_VERIFIED_CLOSED` ×4 in the trace).
3. **run3 — kill mid-run → resume** (`examples/run3-kill-resume`): the run dies during a role dispatch (dangling request visible in the trace); `demo kill-stage` writes the torn partial a real crash leaves; `resume` discards it to `discarded/` and re-dispatches **only the pending role** (`out-evidence-check.txt`: `ALL_CHECKS_PASS: true`).
4. **run4 — cutoff / no-leakage perturbation** (`examples/run4-perturbation`): `demo perturb` proves deterministic re-runs and clean cutoff; a post-cutoff 8-K injected into the manifest is **rejected by the gate** (exit 1); outputs are byte-identical before/after.
5. **run5 — evolution, 3-part demo** (`examples/run5-evolution`): scoped change → regression → human APPROVE (active pointer swap); over-broad proposal touching locked controller scope → approve **refused** + REJECT recorded; second approved version → authorized **rollback** → and a verification case run under the rolled-back version.

## Running the web UI

```bash
python3 launch.py          # recommended: setup + workspace + server + browser
# or, manually (demo store, no real workspace):
.venv/bin/python -m uvicorn ui.api:app --port 8765
# or, in controller mode against a specific workspace:
COVERAGEFLOW_WORKSPACE=examples/run1-clean \
    .venv/bin/python -m uvicorn ui.api:app --port 8765
# open http://127.0.0.1:8765  (no build step; institutional light "paper" look)
```

The SPA covers run control, live monitoring (state, rounds, findings, trace,
budgets), the approval gate with dossier, evolution management, case/artifact
inspectors with report-version diff, the EDGAR bundle builder, a demo control
panel (labeled "scripted rehearsal": seed finding / kill run / perturbation
check), run replay viewer, and the backend manager with per-role token
metering. Every UI action routes through the controller; the UI never bypasses
orchestrator gates. (`python -m coverageflow ui` is the contract verb for the
same launch; it still prints a placeholder, so use `launch.py` or the uvicorn
command above. The UI service ships with deterministic in-memory demo data and
switches to a real workspace in controller mode via `COVERAGEFLOW_WORKSPACE`,
which `launch.py` sets.) The backend picked on the Backends page is saved in
the workspace and survives a restart; deleted cases move to `deleted/` and can
be restored or erased from the Deleted cases page.

## Mandate prefill and review

Market data is fetched by code, with provenance; the mandate stays human-owned. A prefill is only a proposal until a person accepts it.

```bash
# propose: price, peers + multiples, beta/risk-free/WACC, exit multiple, own band
python -m coverageflow case prefill INTC --cutoff 2026-10-30 [--peers AMD,NVDA] [--erp 0.05]
# -> workspace/drafts/intc-2026-10-30/mandate.proposed.json (or the case dir if the case exists)
python -m coverageflow case review intc-2026-10-30         # re-print the table
python -m coverageflow case accept intc-2026-10-30 --set assumptions.exit_multiple_ebitda=15
python -m coverageflow case new --ticker INTC --trigger-doc intc-10q-2026q3 \
    --cutoff 2026-10-30 --mandate workspace/drafts/intc-2026-10-30/mandate.json
```

- Sources: Stooq daily/weekly closes (Yahoo chart API as fallback), FRED `DGS10` for the risk-free rate, SEC XBRL `companyfacts` for fundamentals (only facts `filed` on or before the cutoff). All reads go through one injectable `fetch`, so tests use recorded fixtures. Tags and fallbacks are listed in `coverageflow/market_data.py`.
- Every proposed value has a `provenance` entry (`value, source, url, as_of, sha256, basis`, plus `inputs` for derived values such as WACC). Anything that could not be fetched is `null`, listed in `missing`, and must be entered with `--set path=value` before the mandate can be accepted.
- `status: proposed` mandates are refused by `case new`, `run` and `resume`. `case accept` validates against `references/mandate-schema.json` and the live-backend rules, then writes `mandate.json` with `approval: {by, at, edits: [{path, proposed, final}]}` and keeps the provenance.
- Peers: `--peers`, else `references/peer-sets.json` (INTC, AMD seeded), else a coarse same-SIC suggestion from EDGAR. Segment growth bounds fall back to the existing defaults because companyfacts has no segment history; the basis says so.
- In the web UI the mandate form has a "Prefill from market data" button. Each field shows its source and as-of date, changed fields are marked "edited", and creating the case accepts the form and records the edits.

## SEC filing search

The source bundle search resolves ticker symbols through the SEC company ticker
index and searches the recent filings in the SEC submissions feed. It does not
include older filings stored in the SEC's historical submissions files. The
SEC index does not cover every security or historical ticker. An unavailable SEC
connection falls back to the small Intel/AMD demo index when possible; other
lookups report an error. Set an identifying user agent before launching, for
example `export COVERAGEFLOW_SEC_USER_AGENT="Your Name your@email.com"`.

Live SEC filings can be searched in either mode. Freezing them requires a
controller workspace, such as the workspace opened by `python3 launch.py`.
The demo-only server cannot freeze live filings.

## Backend configuration

Role dispatches (per fresh call) use one backend, selected per case:

- **CLI headless** — `claude -p` / `codex exec` / `kimi` / `vibe -p` (headless Mistral CLI, capped at $1.50 per call): uses the local CLI login, no API keys; auto-detection checks PATH (tests ship fake CLI fixtures).
- **OpenAI-compatible API** — set `COVERAGEFLOW_BASE_URL`, `COVERAGEFLOW_MODEL`, `COVERAGEFLOW_API_KEY` (optionally `COVERAGEFLOW_CONFIG` for a JSON config file); the launcher writes the same fields to `.coverageflow.json`. Backend selection is the `--backend` flag on `run` / UI selection / auto-detection.
- **Replay** — recorded responses matched by role + prompt hash; the whole test suite and the five evidence runs use the offline stub/replay dispatch ("scripted rehearsal" — real engines, scripted role responses).
- **Stub** — `--backend stub`: the offline role responder with no recording requirement (the launcher's offline demo mode). The UI's Backends page selection governs runs launched from the case page.

```bash
.venv/bin/python -m coverageflow backends     # report backend health/detection
```

## Dependencies & testing

```bash
.venv/bin/python -m pytest tests/            # full suite, fully offline
```

976 tests (controller state machine, gates, budget, resume, registry,
engines, slicer, dispatch/prompt assembly, backends incl. record/replay
roundtrip, xlsx and dossier exporters, FastAPI API, repair-loop economics).
Live LLM access is never required to run the tests or the five offline
evidence runs; `examples/run6-amd-live/` holds the recorded outcome of the
live Codex runs.

## Limitations / known gaps (honest list)

- **Single-writer, single-workspace**: one orchestrator per case directory; no concurrent multi-case scheduling server, no auth on the UI (local tool).
- **Live runs depend on your own CLI login**: runs 1-5 use the offline replay/stub dispatch. `examples/run6-amd-live/` is the curated record of live Codex runs on AMD (artifacts, ledger, trace); re-running it needs a logged-in `codex` CLI. The final AMD case reaches human review but is not an approved publication: see `docs/live-run-hardening.md` for what still stops it.
- **Extraction boundaries**: facts come from structured statement/segment slices via the deterministic slicer; MD&A-only disclosures are noted in `facts.json` `notes`, not modeled.
- **Live mandate requirements**: `case new` on a live backend (not the stub or replay) refuses a mandate without `assumptions.market.price` (as_of <= cutoff) or with no usable `assumptions.peer_block.peers`, and prints an example. Optional `assumptions.rating_bands` = `{"buy": 0.15, "sell": -0.10}`: upside above `buy` is Buy, below `sell` is Sell, otherwise Hold (these are the defaults).
- **Comps**: the demo mandate ships without a filled peer block, so comps-implied price is `null` and the DCF/comps tolerance cross-check is reported but not differential-priced; the gate and table renderers handle the empty case explicitly.
- **CLI `ui`/`export` verbs** are placeholders (ui: use `python3 launch.py` or `uvicorn ui.api:app`; export: use the UI's Export buttons, or `python -m coverageflow.export_dossier`). The UI exports an escalated/unfinished case as a marked **unapproved draft**.
- **Schema-light dispatch format checks**: submission validation pins the frozen wire-format keys per contract; full JSON-schema depth checks remain in the orchestrator's validation.
- **Demo figures are fixture-real**, not guidance: the scripted extractor reports a hand-computed mini 10-Q set so the real model/valuation engines can run offline; live runs extract from the actual document slices.
- Budget unit is tokens/duration/rounds; no monetary cost ledger yet.

## Documentation map

- `docs/task-spec.md` — Requirement 1 (task domain, triggers, products)
- `docs/design-decisions.md` — the locked design record (Rounds A–F)
- `docs/design-note.md` — **the graded design document** (Requirement 2): roles and handoffs, the loop, long-running operation, verification and improvement, each mapped to the lecture concepts with code references
- `docs/live-run-hardening.md` — second phase: what the live AMD runs exposed, the changes, the measurements
- `docs/diagnosis-2026-11-11.md` — the first diagnosis of the live Intel case (finding-ID collisions, owner routing, cost)
- `docs/workflow-diagram.png` — annotated architecture figure (`scripts/gen_diagram.py` regenerates it)
- `examples/README.md` — the evidence-run index
- `docs/contracts/` — frozen state/artifact schemas and interface contracts

## Contribution statement

This repository was built by parallel agent streams with contract-first
frozen interfaces, integrated by a coordinator stream:

- **A1 — runtime & data**: EDGAR bundle fetcher/hasher, deterministic slicer, model engine, valuation engine, xlsx exporter, model schema, segment mapping (`coverageflow/{bundle,slicer,model_engine, valuation_engine,export_xlsx}.py`, `references/`, bundles).
- **A2 — controller**: orchestrator state machine, gates, budgets, findings ledger, verified resume, version registry, controller CLI (`coverageflow/{orchestrator,gates,budget,statestore,ledger,resume, registry,cli}.py`).
- **A3 — agent layer**: RoleCall/dispatch, prompt assembly, backend adapters (CLI/API/replay), role cards and pinned references (`coverageflow/{dispatch,_prompt_assembly,backends/*}.py`, `prompts/`).
- **A4 — web UI**: FastAPI service, WebSocket push, no-build SPA (`ui/`).
- **A5 — docs & evidence (this stream)**: `requirements.txt`, `docs/design-note.md`, `docs/workflow-diagram.png` + `scripts/gen_diagram.py`, the five evidence runs under `examples/`, the LaTeX dossier template + renderer (`references/dossier-template.tex`, `coverageflow/export_dossier.py`, `tests/test_export_dossier.py`), and the top-level README.
- **Coordinator**: contract authoring and freeze, stream integration, cross-stream test upkeep; A6 (integration phase) wires the remaining CLI verbs and controller-mode UI.

All streams executed against the same frozen contracts; streams did not
modify each other's modules, and integration is the coordinator's job. The
author directed all streams, reviewed every change, and owns the final
work product.

## Credits

- The three supplied course Excel templates — **`Complete Quantitative Analysis_40491.xlsx`**, **`Discounted Cash Flow Template_22221.xlsx`**, and **`Stock Analysis Tool_60250.xlsx`** — provided the layout conventions adopted by this project: line-item naming and absolute-ratio row pairing (`export_xlsx.py` "Statements Model"/"Ratios" tabs), YoY/QoQ ratio conventions, the DCF tab layout (projection + PV + terminal via exit multiple and Gordon growth, EV → equity → per-share bridge, market-vs-intrinsic block), the Company Comparison tab layout (dated peer block, implied per-share values, own historical band), and the clean institutional styling (no gridlines, light header fills, thousands separators, static values).
- The lecture slides for the long-running-workflow architecture (Orchestrator/Generator/Critic, harness, loops and gates, finding ledgers, repair packets, version registries) that this system implements and the design note maps to.
- EDGAR (SEC) for the public filings snapshotted in the demo bundles.
