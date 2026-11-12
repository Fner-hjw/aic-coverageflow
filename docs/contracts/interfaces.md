# Frozen Contracts — Interfaces (v1.0)

Interfaces for stream A3 (agent layer), A2 (controller/CLI), A4 (UI). All Python 3.11+,
stdlib + explicitly listed dependencies only (`openpyxl`, `fastapi`, `uvicorn`, `websockets`
via fastapi, `httpx` for the API backend). No npm. Layout:

```
coverageflow/           # Python package
  __init__.py  cli.py   # typer-style argparse CLI (stdlib argparse to keep deps light)
  orchestrator.py       # state machine, gates, routing (A2)
  gates.py  ledger.py  registry.py  budget.py  statestore.py  resume.py
  dispatch.py           # role-call assembly + backend invocation + trace (A3)
 .backends/  base.py  cli_backends.py  api_backend.py  replay_backends.py
  slicer.py  model_engine.py  valuation_engine.py  export_xlsx.py  export_dossier.py
  bundle.py            # EDGAR fetch/snapshot/hash (A1)
ui/                    # FastAPI service (A4): api.py ws.py static/index.html static/app.js
```

## 1. Role-call envelope (A3 dispatch; single source of truth)

```python
@dataclass(frozen=True)
class RoleCall:
    role: str                    # ingest|extractor|model|writer|critic_c1..c4
    request_id: str
    candidate_id: str | None
    role_card: dict              # {"path":"prompts/ingest.md", "version":"1.0", "sha256":"..."}
    payload: dict                # bound request data injected into the assembled prompt
    output_schema: str           # json-schema ref; submission validated against it
    budget: dict                 # {"tokens_max": 60000, "timeout_seconds": 240}
    write_scope: list[str]        # exactly one artifact path for worker roles
```

## 2. Backend adapter interface (A3)

```python
class Backend(Protocol):
    name: str                                       # claude|codex|kimi|api|replay
    def healthcheck(self) -> HealthReport: ...
    def invoke(self, call: RoleCall, assembled_prompt: str) -> AgentResult: ...
    def estimate_cost(self, text: str) -> CostEstimate|None: ...

@dataclass
class AgentResult:
    ok: bool
    output_text: str | None        # raw model output
    parsed: dict | None            # parsed JSON if ok
    error: str | None              # error message if not ok
    backend: str                    # "claude" etc.
    elapsed_seconds: float
    tokens_in: int | None          # best-effort estimate if backend doesn't report
    tokens_out: int | None
    raw_response_ref: str | None   # path in replay/ when recording (relative to case dir)
```

- **cli_backends.py:** invoke `claude -p <prompt> --output-format json`, `codex exec <prompt> --json`, kimi equivalent; each adapter normalizes to AgentResult; reads CLI from config.
- **api_backend.py:** OpenAI-compatible `/chat/completions`; `base_url/model/api_key` from `[env: COVERAGEFLOW_*]` → config file → prompt user; enforced timeout.
- **replay_backends.py:** `ReplayBackend` loads `replay/<case>/` recordings, matches request by role+prompt-hash; `RecordingBackend` wraps any live backend and writes recordings.
- **Backend select:** `--backend` flag → config → `auto` = detect installed CLIs in PATH, interactive prompt (backend list shown, user picks); stores choice in config.

## 3. Controller CLI (A2)

```
python -m coverageflow case new --ticker INTC --trigger-doc intc-10q-2026q3 --cutoff 2026-10-30
python -m coverageflow case list
python -m coverageflow run    [--case <id>] [--backend auto] [--replay] [--record]
python -m coverageflow resume [--case <id>]          # verified resume of pending work only
python -m coverageflow approve --case <id> --decision APPROVED|REJECTED|PUBLISHED_WITH_EXCEPTIONS|REVISION_REQUESTED --by "Name" [--reason "..."] [--acknowledge <finding-id>...] [--finding "OWNER[/SEVERITY]:SUMMARY"]...
                                # --reason (>= 15 chars) required for all but APPROVED; PUBLISHED_WITH_EXCEPTIONS is refused over non-waivable findings (C1.1-C1.3, C2.1-C2.2, C3.1-C3.2, gate findings); --finding only for REVISION_REQUESTED (max 2 requests per case)
python -m coverageflow demo seed-finding|kill-stage|perturb --case <id> [--stage k]
python -m coverageflow evolve prepare|propose|regress  # evo lifecycle
python -m coverageflow export --case <id>             # xlsx + pdf + html
python -m coverageflow ui [--port 8765]               # launches FastAPI service
python -m coverageflow gates --case <id>              # rerun gates, print report
python -m coverageflow backends|health
```

## 4. Engine functions (A1) — exact signatures

```python
def slice_document(doc_path: Path, form: str) -> list[Slice]:        # in slicer.py
def build_model(facts: Facts, mapping_version: str) -> Model:        # in model_engine.py
def value_company(model: Model, forecast: Forecast, assumptions: Assumptions) -> Valuation:
def build_bundle(cik: str, doc_specs: list[DocSpec], dest: Path) -> Manifest:  # bundle.py
def export_model_xlsx(model, valuation, out: Path) -> Path          # export_xlsx.py
def export_dossier_pdf(report_md: Path, evidence: Evidence, out: Path) -> Path
```
Engines are pure (no I/O except explicit paths), deterministic, unit-tested with fixture
facts, raise typed errors (`BoundsViolation`, `MissingFactError`), and never call LLMs.

## 5. FastAPI service (A4 → calls controller functions only, never raw file manipulation)

```
GET  /api/cases                       # list + state summary
POST /api/cases                       # new case (wizard payload)
GET  /api/cases/{id}                  # state, rounds, findings, budget summary
GET  /api/cases/{id}/trace?from=NN    # trace events after cursor
GET  /api/cases/{id}/artifacts/{path} # read-only artifact (JSON/text/md)
POST /api/cases/{id}/run {backend, replay}
POST /api/cases/{id}/resume
POST /api/cases/{id}/approval {decision, by, reasoning, acknowledge_findings?, findings?}
                                      # findings: [{summary, owner: writer|model|extractor, severity?: major|critical,
                                      #   affected_artifacts?, required_evidence?}], REVISION_REQUESTED only. The call records the
                                      #   decision and returns status RUNNING; the caller then POSTs /run. Case detail carries
                                      #   revision: {used, max, remaining, rounds_per_request}.
POST /api/cases/{id}/demo/seed-finding|kill-stage|perturb
GET  /api/cases/{id}/report?version=N   # report md + rendered html + diff-with-prev
POST /api/cases/{id}/export {format}
GET  /api/versions                     # registry + active pointer
POST /api/evolution/prepare {change,...}
POST /api/evolution/regress {version}
POST /api/evolution/approve {version, decision, by}
POST /api/evolution/rollback {to_version}
GET  /api/backends + POST /api/backends/select
GET  /api/edgar/search?ticker=&forms=&window=   # bundle builder: filing list
POST /api/bundles  {cik, docs[], name}           # snapshot into new bundle dir
WS   /ws/events                       # push: {case_id, event, ts} + state snapshots
```

UI = hash-routed SPA (`static/index.html`, `static/app.js`, plain CSS; institutional/paper
aesthetic: light paper background, serif headings, mono numeric tables, one restrained
accent). WebSocket drives live updates; every UI action hits an endpoint above; no UI
logic ever writes state files directly.

## 6. Replay recording format (A3/A5)

```
replay/<case_id>/<request_id>.json:
  {"role","request_id","candidate_id","prompt_sha256","prompt_path","assembled_prompt_sha256",
   "response_text","backend","recorded_at"}
```
ReplayBackend loads the directory and answers calls with matching role+prompt_sha256;
missing match → hard error (no silent replay of wrong context).

## 7. Coding rules for ALL streams

- Python 3.11+, stdlib-first; `pytest` for tests; no external task runner.
- Type hints on public functions; module docstring per file (purpose + owner stream).
- Every state artifact write goes through `statestore.py` helpers (atomic write + hash).
- No stream modifies another stream's module; integration is the coordinator's job.
- Tests must run offline (backend=replay or plain engines) — no network in CI.
- Commits: small, meaningful messages, one feature per commit; commit to your stream's branch: `a1-runtime`, `a2-controller`, `a3-agent-layer`, `a4-ui`.
