"""CoverageFlow controller CLI (owner: stream A2, contracts interfaces §3).

Implements every command from the frozen CLI contract EXCEPT `export` and
`ui`, which are owned by other streams and print placeholders so the
command surface stays complete.

Wiring is offline-first: the dispatch layer (coverageflow/dispatch.py,
stream A3) and the deterministic engines (coverageflow/model_engine.py /
valuation_engine.py, stream A1) are looked up dynamically; when they are
not available the CLI falls back to the scripted offline stub
(coverageflow/_dispatch_test_stub.py) so development and CI stay network-
free. All controller actions go through the Orchestrator — the CLI never
writes state files directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gates, statestore
from . import bundle as bundle_mod
from . import registry as registry_mod
from .orchestrator import (Orchestrator, missing_input_documents,
                           new_case_id)

DEFAULT_WORKSPACE = Path.cwd()
OWNED_BY_OTHER_STREAM_EXIT = 2


# ---------------------------------------------------------------------------
# wiring helpers (seams owned by other streams; offline fallback otherwise)
# ---------------------------------------------------------------------------

def _dispatch_root(workspace: Path | None = None) -> Path:
    """Repo-style root for role-card assembly (prompts/ + references/)."""
    bases = [Path(workspace) if workspace else None, DEFAULT_WORKSPACE,
             Path(__file__).resolve().parent.parent]
    for base in bases:
        if base is not None and (base / "prompts").is_dir():
            return base
    return DEFAULT_WORKSPACE


def load_dispatch(offline: bool = False, backend: str | None = None,
                  workspace: Path | None = None,
                  replay_dir: Path | None = None):
    """Return (dispatch, backend_name) — A3's real dispatch when importable.

    The dispatch callable mirrors the orchestrator seam
    ``dispatch(RoleCall) -> AgentResult``:

    - ``backend="auto"``: detect backends via
      ``coverageflow.backends.detect_available_backends``; when nothing is
      available (offline CI) we fall back to the scripted offline stub so
      development stays network-free.
    - ``backend="replay"``: the offline deterministic path —
      ``ReplayBackend(replay_dir)`` answering from recorded responses
      (missing recordings are hard errors by design, never silent replay).
    - ``backend="claude"|"codex"|"kimi"|"vibe"|"api"``: that live
      backend.
    - when the agent layer is not importable at all: offline stub.
    """
    try:
        from . import dispatch as dispatch_mod  # stream A3
        from .backends import make_backend, detect_available_backends
    except Exception:
        from . import _dispatch_test_stub
        return _dispatch_test_stub.StubDispatch(), "replay-stub (offline)"

    class RealDispatch:
        """Callable dispatch(RoleCall) over dispatch_role + a backend."""

        name = backend

        def __init__(self, be):
            self._backend = be
            self.name = be.name

        def __call__(self, call):
            result, _trace = dispatch_mod.dispatch_role(
                call, self._backend, root=_dispatch_root(workspace))
            return result

        def healthcheck(self):
            report = self._backend.healthcheck()
            return {"ok": report.ok, "backend": report.backend,
                    "detail": report.detail}

    name = str(backend or "").strip().lower()
    if not name or name in ("stub", "offline"):
        from . import _dispatch_test_stub
        return _dispatch_test_stub.StubDispatch(), "stub (offline)"
    if name == "replay":
        if replay_dir is None:
            raise ValueError("replay backend requires a replay_dir "
                             "(cases/<id>/replay)")
        b = make_backend("replay", replay_dir=str(replay_dir))
        return RealDispatch(b), "replay"
    if name == "auto":
        available = detect_available_backends()
        if not available:
            from . import _dispatch_test_stub
            return _dispatch_test_stub.StubDispatch(), "replay-stub (offline)"
        b = make_backend(available[0])
        return RealDispatch(b), b.name
    try:
        b = make_backend(name)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return RealDispatch(b), b.name



def load_gate_engine(schema_path: Path | None = None, stub_engines: bool = False,
                     workspace: Path | None = None):
    """Engine wiring per contracts interfaces section 4 (stream A1)."""
    from . import _dispatch_test_stub as stub
    build_fn = value_fn = None
    if not stub_engines:
        build_fn = _try_import_attr("coverageflow.model_engine", "build_model")
        value_fn = _try_import_attr("coverageflow.valuation_engine",
                                   "value_company")
    if build_fn is None or value_fn is None:
        build_fn, value_fn = stub.stub_build_model, stub.stub_value_company
    schema = schema_path or find_schema_path(workspace)
    return gates.GateEngine(schema_path=schema, build_model_fn=build_fn,
                            value_fn=value_fn)


def _try_import_attr(module: str, attr: str):
    try:
        import importlib
        mod = importlib.import_module(module)
        return getattr(mod, attr)
    except Exception:
        return None


def find_schema_path(workspace: Path | None = None) -> Path:
    """The JSON Schema the facts_schema gate validates against.

    ``references/facts-schema.json`` is the real schema; ``model-schema.json``
    is the field catalogue and is only a last-resort fallback (the gate fails
    closed when it finds no validation keywords).
    """
    bases = [Path(workspace) if workspace else None, DEFAULT_WORKSPACE,
             Path(__file__).resolve().parent.parent]
    for name in ("facts-schema.json", "model-schema.json"):
        for base in bases:
            if base is None:
                continue
            candidate = base / "references" / name
            if candidate.exists():
                return candidate
    return Path("references") / "facts-schema.json"


def registry_path(ws: Path) -> Path:
    return ws / "versions" / "registry.json"


def bundle_search_dirs(ws: Path) -> list[Path]:
    """Where frozen bundles live: the workspace, then the repo's data/."""
    return [ws / "data", Path(__file__).resolve().parent.parent / "data"]


def resolve_bundle_dir(ws: Path, explicit: str | None,
                       manifest: dict) -> Path | None:
    """Locate the frozen bundle a case manifest came from.

    ``explicit`` is a bundle directory or a bundle_id (``--bundle``); when it
    is omitted the manifest's own ``bundle_id`` is resolved. Returns None when
    no bundle can be found, which leaves the case to be blocked on missing
    inputs rather than failing creation.
    """
    data_dirs = bundle_search_dirs(ws)
    if explicit:
        cand = Path(explicit)
        if (cand / "manifest.json").is_file():
            return cand
        found = bundle_mod.find_bundle_dir(explicit, workspace=ws,
                                           data_dirs=data_dirs)
        if found is None:
            raise SystemExit(
                f"no frozen bundle at {explicit!r}: pass a bundle directory "
                "containing manifest.json, or a bundle_id present under "
                f"{ws / 'bundles'} or data/*-bundle")
        return found
    bundle_id = (manifest or {}).get("bundle_id")
    if not bundle_id:
        return None
    return bundle_mod.find_bundle_dir(bundle_id, workspace=ws,
                                      data_dirs=data_dirs)


def case_state_summary(case_dir: Path) -> dict:
    state = statestore.load_state(case_dir)
    if state is None:
        return {"case_id": case_dir.name, "status": "MISSING"}
    pending = state.get("pending") or {}
    return {
        "case_id": state["case_id"],
        "status": state["status"],
        "round": pending.get("round"),
        "candidate_id": state["candidate"]["candidate_id"],
        "reason": (state.get("reason") or {}).get("code")
        if state.get("reason") else None,
    }


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# command implementations
# ---------------------------------------------------------------------------

def cmd_case_new(args, ws: Path) -> int:
    """case new --ticker INTC --trigger-doc <doc-id> --cutoff YYYY-MM-DD."""
    case_id = new_case_id(args.ticker, args.period, args.cutoff)
    form = "10-K" if "10k" in args.trigger_doc.lower() else "10-Q"
    manifest = {
        "as_of_cutoff": args.cutoff,
        "documents": [{
            "doc_id": args.trigger_doc,
            "form": form,
            "filed": args.cutoff,
            "path": f"inputs/{args.trigger_doc}.htm",
            "sha256": None,
            "bytes": None,
            "source_url": None,
        }],
        "bundle_id": f"{args.ticker.lower()}-{args.cutoff[:7]}",
        "bundle_sha256": None,
    }
    if args.manifest:
        manifest = statestore.read_json(Path(args.manifest))
    mandate = default_mandate(case_id, args.ticker, args.cutoff)
    if args.mandate:
        mandate = statestore.read_json(Path(args.mandate))
    dispatch, backend = load_dispatch()
    rg = registry_mod.VersionRegistry(registry_path(ws))
    bundle_dir = resolve_bundle_dir(ws, args.bundle, manifest)
    try:
        orch = Orchestrator.create_case(
            ws, case_id, mandate, manifest, dispatch=dispatch,
            gate_engine=load_gate_engine(stub_engines=args.stub_engines,
                                         workspace=ws),
            config_versions=rg.config_versions(),
            bundle_dir=bundle_dir)
    except FileExistsError as exc:
        raise SystemExit(str(exc))
    _print({"created": case_id, "case_dir": str(orch.case_dir),
            "backend": backend,
            "bundle": str(bundle_dir) if bundle_dir else None,
            "inputs_materialized": len(
                missing_input_documents(orch.case_dir, manifest)) == 0,
            "note": "the bundle's filings are copied into the case inputs; "
                    "overrides: --manifest / --mandate / --bundle"})
    return 0


def _mandate_assumption_extras(wacc: float, terminal_growth: float,
                               as_of: str) -> dict:
    """Human-owned valuation extras required by the A1 valuation engine
    (valuation_engine.Assumptions.from_payload): market.price, own_band
    (lo, hi per multiple), sensitivity.{wacc_points,terminal_growth_points}.
    """
    return {
        "market": {"as_of": as_of, "price": None,
                   "note": "human-owned market data; fill at mandate "
                           "creation (USD per share, as-of <= cutoff)"},
        "own_band": {"as_of": as_of,
                     "ev_ebitda": [10.0, 16.0], "p_e": [12.0, 26.0],
                     "p_s": [2.5, 7.5],
                     "note": "own historical multiple band (lo, hi)"},
        "sensitivity": {
            "wacc_points": [round(wacc + d * 0.02, 6) for d in (-2, -1, 0, 1, 2)],
            "terminal_growth_points": [round(terminal_growth + d * 0.01, 6)
                                       for d in (-2, -1, 0, 1, 2)],
            "note": "WACC x terminal growth sensitivity grid points"},
    }


def default_mandate(case_id: str, ticker: str, cutoff: str) -> dict:
    """A conservative default mandate (human-owned; editable at creation)."""
    return {
        "mandate_id": f"man-{case_id}",
        "case_id": case_id,
        "ticker": ticker.upper(),
        "cutoff_date": cutoff,
        "report_policy": {
            "note_type": "quarterly_update",
            "length_words": [800, 1600],
            "sections": ["Summary", "Results", "Segment detail", "Outlook",
                         "Valuation", "Risks"],
        },
        "assumptions": {
            "as_of": cutoff,
            "wacc": 0.104, "terminal_growth": 0.025,
            "exit_multiple_ebitda": 14.0,
            "discount_convention": "year_end", "tax_rate": 0.15,
            "forecast_bounds": {
                "segments": {"CCG": [-0.10, 0.12], "DCAI": [-0.05, 0.25],
                             "NEX": [-0.05, 0.20], "Foundry": [-0.05, 0.30],
                             "Altera": [-0.10, 0.15], "Mobileye": [-0.05, 0.20]},
                "margin_floor_gross": 0.30, "margin_ceiling_gross": 0.55,
            },
"peer_block": {"as_of": cutoff,
                           "peers": [], "note": "to be filled at mandate creation"},
            "dcf_comps_tolerance": 0.25,
            **_mandate_assumption_extras(0.104, 0.025, cutoff),
        },
        "limits": {"role_tokens_max": 250000, "case_minutes": 45,
                   "rounds_max": 4, "retries_max": 3, "approval_hours": 48},
        "schema_version": "1.0",
    }


def cmd_case_list(args, ws: Path) -> int:
    cases_root = ws / "cases"
    rows = []
    if cases_root.exists():
        for case_dir in sorted(cases_root.iterdir()):
            if case_dir.is_dir() and (case_dir / "state.json").exists():
                rows.append(case_state_summary(case_dir))
    if not rows:
        print("no cases")
    _print(rows)
    return 0


def _find_case(ws: Path, case_id: str | None) -> Path:
    cases_root = ws / "cases"
    if case_id:
        case_dir = cases_root / case_id
        if not case_dir.exists():
            raise SystemExit(f"no such case: {case_id}")
        return case_dir
    # default: the most recently updated non-terminal case
    candidates = []
    if cases_root.exists():
        for case_dir in cases_root.iterdir():
            if (case_dir / "state.json").exists():
                state = statestore.load_state(case_dir)
                if state and state["status"] in ("CREATED", "RUNNING",
                                                "PENDING_APPROVAL"):
                    candidates.append(case_dir)
    if not candidates:
        raise SystemExit("no active case found; pass --case <id>")
    return sorted(candidates, key=lambda p: p.stat().st_mtime)[-1]


def _resolve_backend(args) -> str | None:
    """--replay > --backend > COVERAGEFLOW_BACKEND env (config layer).

    Unset backbone (offline-first default): the scripted offline stub.
    ``--backend auto`` runs live auto-detection (detect_available_backends);
    an offline environment with nothing detected also lands on the stub.
    """
    if getattr(args, "replay", False):
        return "replay"
    explicit = getattr(args, "backend", None)
    if explicit:
        return explicit
    import os
    return os.environ.get("COVERAGEFLOW_BACKEND", "").strip().lower() or None


def cmd_run(args, ws: Path) -> int:
    case_dir = _find_case(ws, args.case)
    backend = _resolve_backend(args)
    dispatch, backend_name = load_dispatch(
        backend=backend, workspace=ws,
        replay_dir=case_dir / "replay" if backend == "replay" else None)
    _stamp_backend(case_dir, backend_name)
    orch = Orchestrator(case_dir, dispatch=dispatch,
                        gate_engine=load_gate_engine(
                            stub_engines=args.stub_engines, workspace=ws))
    summary = orch.run(extend=getattr(args, "extend", False))
    summary["backend"] = backend_name
    _print(summary)
    return 0


def _recorded_backend(case_dir: Path) -> str | None:
    """The backend this case was last run with, if it recorded one."""
    state = statestore.load_state(case_dir) or {}
    name = state.get("backend")
    return name if isinstance(name, str) and name else None


def _stamp_backend(case_dir: Path, backend_name: str | None) -> None:
    """Record the backend on the case so a later resume cannot swap it."""
    if not backend_name:
        return
    state = statestore.load_state(case_dir)
    if not state or state.get("backend") == backend_name:
        return
    state["backend"] = backend_name
    statestore.write_state(case_dir, state)


def cmd_resume(args, ws: Path) -> int:
    """Verified resume of pending work only (see resume.py).

    The backend is inherited from the case, not defaulted. Resuming a live run
    without it silently swapped in the offline stub, which then wrote canned
    artifacts that passed every gate: the report came back as three lines of
    fixture numbers and nothing flagged it, because a stub is a valid backend.
    """
    case_dir = _find_case(ws, args.case)
    backend = _resolve_backend(args) or _recorded_backend(case_dir)
    dispatch, backend_name = load_dispatch(backend=backend, workspace=ws)
    _stamp_backend(case_dir, backend_name)
    orch = Orchestrator(case_dir, dispatch=dispatch,
                        gate_engine=load_gate_engine(
                            stub_engines=args.stub_engines, workspace=ws))
    summary = orch.run()   # run() resumes RUNNING cases via resume_check
    summary["backend"] = backend_name
    _print(summary)
    return 0


def cmd_approve(args, ws: Path) -> int:
    case_dir = _find_case(ws, args.case)
    if not args.case:
        raise SystemExit("approve requires --case <id>")
    orch = Orchestrator(case_dir, dispatch=load_dispatch()[0],
                        gate_engine=load_gate_engine(stub_engines=False,
                                                      workspace=ws))
    out = orch.record_approval(decision=args.decision, by=args.by,
                               reasoning=args.reason or "",
                               acknowledge_findings=args.acknowledge or None)
    _print(out)
    return 0


def cmd_gates(args, ws: Path) -> int:
    case_dir = _find_case(ws, args.case)
    orch = Orchestrator(case_dir, dispatch=load_dispatch()[0],
                        gate_engine=load_gate_engine(stub_engines=False,
                                                     workspace=ws))
    report = orch.gates_report()
    _print(report)
    return 0 if report["passed"] else 1


def cmd_demo(args, ws: Path) -> int:
    case_dir = _find_case(ws, args.case)
    state = statestore.load_state(case_dir)
    if args.demo_cmd == "seed-finding":
        led = _ledger(case_dir)
        fid = led.open_finding({
            "severity": "minor",
            "summary": ("[demo] seeded rehearsal finding: gross margin driver "
                        "narrative cites stale segment table"),
            "affected_artifacts": ["report.md"],
            "required_evidence": "updated segment-table citation",
            "dedup_key": "demo-seeded-finding",
        }, round=state["pending"].get("round") or 1)
        _print({"seeded_finding": fid, "case": case_dir.name})
        return 0
    if args.demo_cmd == "kill-stage":
        # simulate the kill footprint: a torn partial output for the pending
        # role (resume must discard it and re-dispatch only that role)
        stage = args.stage or state["pending"].get("stage") or "extractor"
        rel = _partial_path(case_dir, state, stage)
        if rel:
            rel_path = case_dir / rel
            rel_path.parent.mkdir(parents=True, exist_ok=True)
            rel_path.write_text('{"partial": ')
            _print({"simulated_kill": True, "partial_output": rel,
                    "resume_hint": f"resume --case {case_dir.name}"})
            return 0
        _print({"simulated_kill": False,
                "reason": "no writable partial for the current pending stage"})
        return 1
    if args.demo_cmd == "perturb":
        # cutoff/no-leakage check: gates must be deterministic re-runs and
        # every manifest document must be filed <= cutoff
        orch = Orchestrator(case_dir, dispatch=load_dispatch()[0],
                            gate_engine=load_gate_engine(stub_engines=False,
                                                          workspace=ws))
        r1 = orch.gates_report()
        r2 = orch.gates_report()
        manifest = statestore.read_json(case_dir / "inputs" / "manifest.json")
        cutoff = statestore.read_json(case_dir / "mandate.json")["cutoff_date"]
        ok, findings = gates.gate_manifest_cutoff(manifest, cutoff)
        _print({
            "perturbation": {
                "deterministic_recompute": r1 == r2,
                "no_post_cutoff_documents": ok,
                "cutoff": cutoff,
                "leaking_docs": [f["summary"] for f in findings],
            }
        })
        return 0 if (r1 == r2 and ok) else 1
    raise SystemExit(f"unknown demo command {args.demo_cmd}")


def _ledger(case_dir: Path):
    from . import ledger as ledger_mod
    return ledger_mod.FindingsLedger(case_dir)


def _partial_path(case_dir: Path, state: dict, stage: str) -> str | None:
    round_no = state["pending"].get("round") or 1
    stage_artifacts = {"ingest": "ingest-report.json", "extractor": "facts.json",
                       "model": "forecast_assumptions.json", "writer": "report.md",
                       "candidate": "candidate-manifest.json"}
    if stage not in stage_artifacts:
        return None
    return f"rounds/round-{round_no:03d}/generator/{stage_artifacts[stage]}"


def cmd_evolve(args, ws: Path) -> int:
    rg = registry_mod.VersionRegistry(registry_path(ws))
    if args.evolve_cmd == "prepare":
        entry = {
            "version": None, "parent": rg.active(), "scope": args.scope or "",
            "proposal": {"author": args.author or os_user(),
                         "diff_files": [], "diff_sha256": None},
            "tested": {},
        }
        _print({"prepared": entry,
                "next": "fill version/scope/diff; then: evolve propose"})
        return 0
    if args.evolve_cmd == "propose":
        if not args.spec:
            raise SystemExit("evolve propose requires --spec <json-file>")
        entry = statestore.read_json(Path(args.spec))
        record = rg.register(entry)
        _print({"registered": record["version"], "active": rg.active()})
        return 0
    if args.evolve_cmd == "regress":
        if not args.version:
            raise SystemExit("evolve regress requires --version")
        results = {}
        if args.results:
            results = statestore.read_json(Path(args.results))
        record = rg.record_review(
            args.version,
            fresh_critic_findings=results.get("fresh_critic_findings", []),
            regressions_all_pass=bool(results.get("regressions_all_pass", False)),
            identical_outputs_proof=results.get("identical_outputs_proof", {}))
        _print({"review_recorded": args.version,
                "regressions_all_pass": record["review"]["regressions_all_pass"]})
        return 0
    if args.evolve_cmd == "approve":
        rg.approve(args.version, by=args.by or os_user(),
                   decision=(args.decision or "APPROVED"))
        _print({"active": rg.active()})
        return 0
    if args.evolve_cmd == "rollback":
        out = rg.rollback(args.version, by=args.by or os_user(),
                          reason=args.reason or "")
        _print({"rollback": out, "active": rg.active()})
        return 0
    raise SystemExit(f"unknown evolve command {args.evolve_cmd}")


def os_user() -> str:
    import getpass
    import os
    return os.environ.get("USER") or getpass.getuser()


def cmd_backends_health(args, ws: Path) -> int:
    dispatch, backend = load_dispatch()
    health = getattr(dispatch, "healthcheck", lambda: {"ok": False})()
    _print({
        "controller_backend": backend,
        "health": health,
        "known_backends": ["claude", "codex", "kimi", "vibe", "api",
                         "replay"],
        "note": "backend management/selection is owned by stream A3 "
                "(coverageflow/backends); the controller only reports status",
    })
    return 0 if (health or {}).get("ok") else 1


def cmd_export_placeholder(args, ws: Path) -> int:
    print("export is owned by another stream (A1/A5 renderers); "
          "nothing has been run.")
    return OWNED_BY_OTHER_STREAM_EXIT


def cmd_ui_placeholder(args, ws: Path) -> int:
    print("ui is owned by another stream (A4); nothing has been launched.")
    return OWNED_BY_OTHER_STREAM_EXIT


# ---------------------------------------------------------------------------
# argument parsing (stdlib argparse; typer-style command surface)
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coverageflow",
                                description="CoverageFlow controller CLI")
    p.add_argument("--workspace", default=None,
                   help="workspace root (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, handler):
        sp = sub.add_parser(name)
        sp.set_defaults(handler=handler)
        return sp

    # case
    case = add("case", None)
    case_sub = case.add_subparsers(dest="case_cmd", required=True)
    new = case_sub.add_parser("new")
    new.add_argument("--ticker", required=True)
    new.add_argument("--period", default="fy2026q3")
    new.add_argument("--trigger-doc", required=True)
    new.add_argument("--cutoff", required=True)
    new.add_argument("--manifest", default=None)
    new.add_argument("--mandate", default=None)
    new.add_argument("--bundle", default=None,
                     help="frozen bundle directory (or bundle_id) whose "
                          "filings are copied into the case inputs; "
                          "defaults to the bundle the manifest names")
    new.add_argument("--stub-engines", action="store_true",
                     help="force offline engine double")
    new.set_defaults(handler=cmd_case_new)
    listing = case_sub.add_parser("list")
    listing.set_defaults(handler=cmd_case_list)

    # run / resume
    run = add("run", cmd_run)
    run.add_argument("--case", default=None)
    run.add_argument("--backend", default=None,
                     help="auto | replay | claude | codex | kimi | vibe | api; "
                          "omit for the offline stub (dev/test default)")
    run.add_argument("--replay", action="store_true")
    run.add_argument("--record", action="store_true")
    run.add_argument("--stub-engines", action="store_true",
                     help="force offline engine double")
    run.add_argument("--extend", action="store_true",
                     help="EXHAUSTED case: grant one more full budget "
                          "allowance and continue instead of starting a new case")
    resume = add("resume", cmd_resume)
    resume.add_argument("--case", default=None)
    resume.add_argument("--backend", default=None,
                        help="omit to inherit the backend the case was run "
                             "with; never silently falls back to the stub")
    resume.add_argument("--stub-engines", action="store_true")

    # approve / gates
    approve = add("approve", cmd_approve)
    approve.add_argument("--case", required=True)
    approve.add_argument("--decision", required=True,
                         choices=["APPROVED", "REJECTED"])
    approve.add_argument("--by", required=True)
    approve.add_argument("--reason", default="")
    approve.add_argument("--acknowledge", nargs="*", default=None,
                         metavar="FINDING_ID",
                         help="open blocking findings the approver has read and "
                              "is signing off over; required for an escalated "
                              "case, and every open blocking finding must be "
                              "named")
    gates_cmd = add("gates", cmd_gates)
    gates_cmd.add_argument("--case", default=None)

    # demo
    demo = add("demo", cmd_demo)
    demo.add_argument("demo_cmd",
                      choices=["seed-finding", "kill-stage", "perturb"])
    demo.add_argument("--case", default=None)
    demo.add_argument("--stage", default=None)

    # evolve
    evolve = add("evolve", cmd_evolve)
    evolve.add_argument("evolve_cmd",
                        choices=["prepare", "propose", "regress", "approve",
                                 "rollback"])
    evolve.add_argument("--spec", default=None)
    evolve.add_argument("--version", default=None)
    evolve.add_argument("--scope", default=None)
    evolve.add_argument("--author", default=None)
    evolve.add_argument("--decision", default=None)
    evolve.add_argument("--by", default=None)
    evolve.add_argument("--reason", default=None)
    evolve.add_argument("--results", default=None,
                        help="JSON file with regression results")

    # backends / health
    add("backends", cmd_backends_health)
    add("health", cmd_backends_health)

    # placeholders (owned elsewhere)
    export = add("export", cmd_export_placeholder)
    export.add_argument("--case", default=None)
    ui = add("ui", cmd_ui_placeholder)
    ui.add_argument("--port", type=int, default=8765)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ws = Path(args.workspace) if args.workspace else DEFAULT_WORKSPACE
    if getattr(args, "replay", False) or getattr(args, "record", False):
        print("note: --replay uses the offline ReplayBackend over the "
              "case's replay/ recordings (missing recordings are hard "
              "errors); --record wraps the selected live backend with "
              "a RecordingBackend")
    return int(args.handler(args, ws) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
