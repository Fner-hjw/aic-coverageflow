"""The CoverageFlow controller state machine (owner: stream A2).

Implements the locked loop design (docs/design-decisions.md section 3):

  states   CREATED -> RUNNING -> PENDING_APPROVAL -> APPROVED/REJECTED -> CLOSED
           terminal: BLOCKED, EXHAUSTED, STOPPED
  rounds   build (ingest -> extractor -> model(+engines) -> writer ->
           generator lead manifest) -> numerical gates -> critic fan-out
           (C1..C4, fresh each round) -> merge + coverage -> repair or accept
  rules
    - state-first transitions: validate -> write state + checkpoint atomically
      -> append trace event; only the Orchestrator writes control state
      (through statestore).
    - request binding: every dispatch gets a fresh request_id written to
      request.json; submissions echoing a different request_id are stale and
      rejected.
    - invalid submissions: repair at the SAME role with a new request WITHOUT
      consuming a round; second invalid submission at the same role ->
      BLOCKED.
    - budgets are checked at EVERY dispatch (case clock, per-role tokens) and
      before every new build round; breach -> EXHAUSTED with a precise
      machine-readable reason. EXHAUSTED never grants acceptance.
    - candidate_id changes on ANY candidate artifact change; any candidate
      change resets the clean-review streak; acceptance needs 2 consecutive
      clean full reviews of the SAME candidate + all gates passing + carried
      findings closed only by critic-verified evidence.
    - approval deadline (mandate-configurable, default 48h): expiry ->
      BLOCKED(APPROVAL_EXPIRED); re-approval restarts acceptance from zero.
    - repair packets per contracts section 6: open findings + required
      evidence + unchanged criteria + delta scope.
    - every terminal state writes result.json with a precise,
      machine-readable reason.

Dispatch seam: the injected *dispatch* object is called as
``dispatch(RoleCall) -> AgentResult`` (contracts interfaces sections 1-2).
Stream A3's dispatch.py implements it at integration; this module never
imports A3/A1 code. Engines enter via an injected gates.GateEngine whose
engine callables follow the frozen signatures
build_model(facts, mapping_version) / value_company(model, forecast,
assumptions) of contracts interfaces section 4; the deterministic engine
artifacts are computed by the orchestrator (never by an LLM).

Additive state fields beyond the frozen schema (flagged for the coordinator):
pending.stage, candidate.last_review_candidate, candidate.gates_passed,
budget.used.retries_by_role.
"""

from __future__ import annotations

import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import budget as budget_mod
from . import bundle as bundle_mod
from . import gates, ledger as ledger_mod
from . import resume as resume_mod
from . import statestore
from ._dispatch_test_stub import RoleCall  # contract envelope (mirrors A3's)
from .dispatch import output_schema_for

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

STATUSES = ("CREATED", "RUNNING", "PENDING_APPROVAL", "APPROVED",
            "REJECTED", "BLOCKED", "EXHAUSTED", "STOPPED", "CLOSED")
TERMINAL = ("APPROVED", "REJECTED", "BLOCKED", "EXHAUSTED", "STOPPED")

# sub-stage order inside a round; the "engine" artifacts are recomputed as
# part of the model stage (idempotent deterministic engines)
STAGE_ORDER = ("ingest", "extractor", "model", "writer", "candidate",
               "gates", "critics", "merge")
STAGE_ROLE = {"ingest": "ingest", "extractor": "extractor", "model": "model",
              "writer": "writer", "candidate": "generator_lead",
              "critics": "critics", "merge": "critic_lead"}
STAGE_ARTIFACT = {"ingest": "ingest-report.json", "extractor": "facts.json",
                  "model": "forecast_assumptions.json", "writer": "report.md",
                  "candidate": "candidate-manifest.json"}
CRITIC_ROLES = tuple(f"critic_c{i}" for i in range(1, 5))
CANDIDATE_FILES = ["ingest-report.json", "facts.json",
                   "forecast_assumptions.json", "report.md",
                   "model.json", "valuation.json"]
CANDIDATE_ROLES = ("ingest", "extractor", "model", "writer", "engines", "engines")

# The note's header carries the candidate id, which is a digest over the note.
# Blanking the value before hashing breaks that cycle the same way the JSON
# artifacts exclude their own `candidate_id` key.
_CANDIDATE_HEADER = re.compile(r"(\*\*Candidate:\*\*)[^·\n]*")
_REQUEST_HEADER = re.compile(r"(\*\*Request:\*\*)[^·\n]*")

# Keys excluded from the candidate identity digest. The id digests what the
# note and the artifacts SAY; a fresh request id every round must not make the
# same candidate look like a new one, or the clean-review streak resets forever
# and acceptance is unreachable. The engine input hashes are excluded because
# they restate another artifact's FILE hash, which carries that request id --
# the artifact's own content is already digested directly.
_IDENTITY_EXCLUDED = frozenset({"candidate_id", "request_id",
                                "input_facts_sha256", "input_forecast_sha256"})


def _blank_candidate(text: str) -> str:
    """Blank the per-round identifiers in a note's provenance header."""
    text = _CANDIDATE_HEADER.sub(r"\1 ", text, count=1)
    return _REQUEST_HEADER.sub(r"\1 ", text, count=1)

# Records the input-manifest hash an ingest report was produced against, so a
# later round can prove the report still describes the same inputs.
INGEST_INPUTS_SIDECAR = "ingest-inputs.sha256"

# What each critic is contracted to read (see the --read line of its card).
# A critic handed only sha256 digests cannot verify anything and reports
# "unverifiable" findings every round, so the candidate never converges.
# Every critic gets every candidate artifact: the header and provenance checks
# span all six, and a digest is not evidence.
CRITIC_ARTIFACTS = {
    "critic_c1": ("ingest-report.json", "facts.json", "report.md",
                  "candidate-manifest.json", "forecast_assumptions.json",
                  "model.json", "valuation.json"),
    "critic_c2": ("ingest-report.json", "facts.json",
                  "forecast_assumptions.json", "model.json", "valuation.json",
                  "report.md", "candidate-manifest.json"),
    "critic_c3": ("ingest-report.json", "candidate-manifest.json", "report.md",
                  "facts.json", "forecast_assumptions.json", "model.json",
                  "valuation.json"),
    "critic_c4": ("ingest-report.json", "candidate-manifest.json", "report.md",
                  "facts.json", "forecast_assumptions.json", "model.json",
                  "valuation.json"),
}

DEFAULT_CONFIG_VERSIONS = {
    "prompt_cards": {"ingest": "1.0", "extractor": "1.0", "model": "1.0",
                     "writer": "1.0", "critics": ["1.0"]},
    "mapping_tables": "1.0",
    "engines": "1.0",
    "schema": "1.0",
}


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_from_iso(iso: str) -> float:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def _dotted_paths(obj: Any, prefix: str = "") -> list[str]:
    """Every addressable path in a nested artifact, as dotted keys.

    ``{"dcf": {"base": {"ebitda_margin": 0.21}}}`` yields ``dcf``,
    ``dcf.base`` and ``dcf.base.ebitda_margin`` so a note can cite the leaf
    it actually states.
    """
    out: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.append(path)
            out.extend(_dotted_paths(value, path))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            out.extend(_dotted_paths(value, f"{prefix}.{i}"))
    return out


# A critic reviewing the candidate sometimes reports on the harness's own
# budget accounting ("per-role token cap breached"). No rewrite of report.md or
# model.json can close that: the tokens are already spent. Left in the ledger it
# reopens every round and the repair loop can never converge, so it is dropped
# at ingestion. The budget tracker already records the breach and the run
# transitions to EXHAUSTED on its own; a critic finding adds nothing but a
# permanent blocker.
_SELF_REFERENTIAL_FINDING = re.compile(
    r"per-role\s+token\s+cap|token\s+cap\s+breach|budget\s+cap\s+breach",
    re.IGNORECASE)


def _is_self_referential_finding(finding: dict) -> bool:
    """Does this finding describe the run's budget rather than the work?"""
    haystack = " ".join(str(finding.get(k) or "") for k in
                        ("dedup_key", "summary", "required_evidence"))
    return bool(_SELF_REFERENTIAL_FINDING.search(haystack))


def _growth_aliases(model: dict) -> list[str]:
    """The names a report uses for growth, mapped onto the nested series.

    Growth lives under the series it measures
    (``income_statement.revenue.yoy.FY2025``), but a writer cites it by the
    name the prose uses: ``derived.revenue_yoy``. Without the alias the marker
    was rejected as an unknown key and the writer had no way to discover the
    nested path, so the same finding reopened every round.
    """
    out: list[str] = []
    for key, entry in (model.get("series") or {}).items():
        if not isinstance(entry, dict):
            continue
        short = key.rsplit(".", 1)[-1]
        for span in ("yoy", "qoq"):
            if isinstance(entry.get(span), dict) and entry[span]:
                out.append(f"derived.{short}_{span}")
    return out


def new_case_id(ticker: str, period: str, cutoff_date: str) -> str:
    """case_id per contracts: <ticker-lower>-<period>-<yyyymmdd>."""
    digits = str(cutoff_date).replace("-", "")
    return f"{ticker.lower()}-{period}-{digits}"


def initial_state(case_id: str, config_versions: dict | None = None) -> dict:
    """Fresh state.json content (contracts section 1) with additive fields."""
    return {
        "case_id": case_id,
        "status": "CREATED",
        "pending": {
            "role": "generator",
            "request_id": None,
            "round": 0,
            "retry_count": 0,
            "case_clock_started": None,
            "deadline": None,
            "stage": None,
        },
        "candidate": {
            "candidate_id": None,
            "clean_review_rounds": 0,
            "built_round": 0,
            "gates_passed": False,
            "last_review_candidate": None,
        },
        "budget": {
            **budget_mod.BudgetTracker({}).limits_state(),
            "used": {"rounds": 0, "elapsed_minutes": 0.0, "tokens_by_role": {}},
        },
        "inputs_manifest_ref": "inputs/manifest.json",
        "inputs_manifest_sha256": None,
        "config_versions": config_versions or dict(DEFAULT_CONFIG_VERSIONS),
        "checkpoints": [],
        "reason": None,
        "updated_at": None,
    }


class StopLoop(RuntimeError):
    """Internal control-flow signal: a terminal state was written mid-loop."""


class InputVerificationError(RuntimeError):
    """A case cannot start: its manifest references inputs that are absent.

    Raised at case creation so a case never enters RUNNING with an inputs
    directory that cannot satisfy the roles that read it. Without this, the
    first role that needs a file is dispatched anyway and burns its whole
    timeout discovering the file is not there.
    """


def missing_input_documents(case_dir: Path | str, manifest: dict) -> list[str]:
    """Case-relative document paths declared by *manifest* that are absent/empty."""
    missing: list[str] = []
    for doc in (manifest or {}).get("documents") or []:
        rel = (doc or {}).get("path")
        if not rel:
            continue
        path = Path(case_dir) / rel
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(str(rel))
    return missing


@dataclass
class Arts:
    """Lazy cache of verified artifacts for the active round."""
    round: int
    files: dict[str, str] = field(default_factory=dict)
    content: dict[str, Any] = field(default_factory=dict)

    def rel(self, team: str, name: str) -> str:
        return f"rounds/round-{self.round:03d}/{team}/{name}"


class Orchestrator:
    """Drives a single coverage case. Pure Python; contains no LLM."""

    def __init__(self, case_dir: Path | str, *, dispatch,
                 gate_engine: gates.GateEngine,
                 clock: Callable[[], float] | None = None,
                 now: Callable[[], str] | None = None):
        if gate_engine is None:
            raise TypeError("gate_engine is required (inject engine callables "
                           "per contracts interfaces section 4)")
        # absolute: the case dir is handed to backends as their working
        # directory, and a relative path resolves against the backend's own
        # cwd (which IS that directory) — the CLI then reports it missing
        self.case_dir = Path(case_dir).resolve()
        self.dispatch = dispatch          # RoleCall -> AgentResult (A3 seam)
        self.gate_engine = gate_engine
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self.now = now or statestore.utc_now_iso
        self._verified: dict[str, str] = resume_mod.load_verified_map(self.case_dir)

    # ---------------------------------------------------------- io helpers
    def _trace(self, **event) -> None:
        statestore.append_trace(self.case_dir, event, now=self.now)

    def _budget(self, state: dict) -> budget_mod.BudgetTracker:
        return budget_mod.BudgetTracker.from_state(state, clock=self.clock)

    def _sync_budget(self, state: dict, budget: budget_mod.BudgetTracker) -> None:
        state["budget"].update(budget.limits_state())
        state["budget"]["used"] = budget.used_state()

    def _transition(self, state: dict, frm: str, to: str,
                    verified: str | None = None, detail: dict | None = None) -> None:
        """State-first: write state + checkpoint atomically, THEN trace."""
        statestore.write_state(self.case_dir, state, transition=(frm, to),
                               verified=verified,
                               verified_artifacts=self._verified, now=self.now)
        trace_detail = {"from": frm, "to": to}
        if detail:
            trace_detail.update(detail)
        self._trace(event="STATE_TRANSITION",
                    candidate_id=state["candidate"]["candidate_id"],
                    round=state["pending"]["round"],
                    request_id=state["pending"]["request_id"],
                    detail=trace_detail)

    def _persist(self, state: dict) -> None:
        statestore.write_state(self.case_dir, state, now=self.now)

    # ------------------------------------------------------------ case setup
    @classmethod
    def create_case(cls, workspace: Path | str, case_id: str, mandate: dict,
                    manifest: dict, *, dispatch, gate_engine: gates.GateEngine,
                    clock: Callable[[], float] | None = None,
                    now: Callable[[], str] | None = None,
                    config_versions: dict | None = None,
                    overwrite: bool = False,
                    bundle_dir: Path | str | None = None) -> "Orchestrator":
        """Create a case directory (mandate + inputs are IMMUTABLE per case).

        When ``bundle_dir`` names the frozen bundle the manifest came from,
        every declared document is copied into the case inputs directory and
        hash-verified, so the case is runnable the moment it is created. A
        case created without a bundle starts with an empty inputs directory
        and ``run`` blocks it until the filings are fetched.

        An existing case is NEVER silently re-created: that would reset its
        state, budget and trace while leaving the old artifacts on disk.
        Pass ``overwrite=True`` to deliberately start over.
        """
        case_dir = Path(workspace) / "cases" / case_id
        if (case_dir / "state.json").exists() and not overwrite:
            raise FileExistsError(
                f"case {case_id!r} already exists at {case_dir}; refusing to "
                "overwrite its state, budget and trace. Use a new case id, or "
                "pass overwrite=True to deliberately discard the existing case."
            )
        case_dir.mkdir(parents=True, exist_ok=True)
        mandate = dict(mandate)
        mandate["case_id"] = case_id
        manifest_sha = statestore.atomic_write_json(
            case_dir / "inputs" / "manifest.json", manifest)
        if bundle_dir is not None:
            # the manifest only declares paths; the case needs its own
            # byte-identical copy of every filing before a role can read them
            bundle_mod.materialize_case_inputs(case_dir, manifest, bundle_dir)
        statestore.atomic_write_json(case_dir / "mandate.json", mandate)
        state = initial_state(case_id, config_versions)
        # per-case budget limits come from the IMMUTABLE mandate
        state["budget"].update(
            budget_mod.BudgetTracker(mandate.get("limits")).limits_state())
        state["inputs_manifest_sha256"] = manifest_sha
        orch = cls(case_dir, dispatch=dispatch, gate_engine=gate_engine,
                   clock=clock, now=now)
        orch._persist(state)
        orch._trace(event="CASE_CREATED",
                    detail={"ticker": mandate.get("ticker"),
                            "cutoff_date": mandate.get("cutoff_date")})
        return orch

    # ------------------------------------------------------------ read helpers
    def load(self) -> dict:
        state = statestore.load_state(self.case_dir)
        if state is None:
            raise RuntimeError(f"no case state at {self.case_dir}/state.json")
        return state

    def mandate(self) -> dict:
        return statestore.read_json(self.case_dir / "mandate.json")

    def input_manifest(self) -> dict:
        return statestore.read_json(self.case_dir / "inputs" / "manifest.json")

    def ledger(self) -> ledger_mod.FindingsLedger:
        return ledger_mod.FindingsLedger(self.case_dir)

    # =========================================================================
    # run
    # =========================================================================
    def run(self, extend: bool = False) -> dict:
        state = self.load()

        if state["status"] == "CLOSED":
            return self._saved_result(state, "case closed; saved result returned")

        # EXHAUSTED is a budget verdict, not a verdict on the work. A human can
        # buy more fuel explicitly; without this the only recovery is a brand
        # new case, which is how a workspace fills with dead cases that all
        # point at the same frozen bundle.
        if state["status"] == "EXHAUSTED":
            if not extend:
                return self._saved_result(
                    state, "case is EXHAUSTED; re-run with a budget grant "
                           "(run --extend) to continue")
            state = self._grant_budget(state)

        # BLOCKED is in TERMINAL, so this branch must come BEFORE the generic
        # terminal check or it is unreachable and the block reason is lost.
        if state["status"] == "BLOCKED":
            code = (state.get("reason") or {}).get("code")
            if code == "APPROVAL_EXPIRED":
                return self._summary(
                    state, "blocked (APPROVAL_EXPIRED); re-approval restarts "
                           "acceptance from zero")
            # Every other block is an instruction ("fix the cause, then
            # re-run"), not a tombstone. A re-run clears it and resumes from
            # the last verified stage; otherwise one transient backend
            # failure forces a brand-new case, and the workspace fills with
            # dead cases that all point at the same frozen bundle.
            if code == "missing_inputs":
                missing = missing_input_documents(self.case_dir,
                                                  self.input_manifest())
                if missing:
                    return self._summary(
                        state, "case blocked: inputs still missing "
                               f"{missing}; fetch them, then re-run")
            state = self._clear_block(state)

        if state["status"] in TERMINAL:
            return self._saved_result(
                state, f"case is {state['status']}; saved result returned")

        if state["status"] == "PENDING_APPROVAL":
            if self._approval_expired(state):
                self._approval_block(state)
                return self._summary(
                    state, "approval deadline expired; case blocked "
                           "(APPROVAL_EXPIRED); re-approval restarts acceptance")
            return self._summary(state, "awaiting human approval")

        if state["status"] == "CREATED":
            missing = missing_input_documents(self.case_dir, self.input_manifest())
            if missing:
                # Fail before any role is dispatched: a role handed a payload
                # that points at absent files can only burn its whole timeout
                # discovering they are not there.
                self.blocked(
                    state, "missing_inputs",
                    "case inputs are incomplete: the manifest declares "
                    f"{len(missing)} document(s) that are absent or empty under "
                    f"{self.case_dir / 'inputs'}: {missing}. Fetch the source "
                    "filings into the case inputs directory, then re-run.",
                    {"missing": missing,
                     "inputs_dir": str(self.case_dir / "inputs")})
                return self._summary(state, "case blocked: inputs missing")
            state["status"] = "RUNNING"
            state["pending"]["case_clock_started"] = self.now()
            state["pending"]["stage"] = None
            self._transition(state, "CREATED", "RUNNING",
                             verified="case start; case clock started")
        elif state["status"] == "RUNNING":
            state = self.resume_check(state)
        else:
            raise RuntimeError(f"cannot run a case in status {state['status']}")

        # One writer per case. Two orchestrators on the same case corrupt it:
        # the second one's resume discards the first one's artifacts as
        # unverified, and the round then proceeds on empty inputs.
        lock = statestore.acquire_case_lock(self.case_dir)
        try:
            return self.loop(state)
        finally:
            statestore.release_case_lock(lock)

    def _saved_result(self, state: dict, message: str) -> dict:
        result = statestore.read_json_or_none(self.case_dir / "result.json")
        return {"status": state["status"],
                "candidate_id": state["candidate"]["candidate_id"],
                "message": message, "saved_result": result, "re_ran": False}

    def _summary(self, state: dict, message: str) -> dict:
        return {"status": state["status"],
                "candidate_id": state["candidate"]["candidate_id"],
                "round": state["pending"]["round"],
                "clean_review_rounds": state["candidate"]["clean_review_rounds"],
                "reason": state.get("reason"),
                "message": message, "re_ran": True}

    # ----------------------------------------------------------- resume check
    def resume_check(self, state: dict) -> dict:
        """Inspect state + artifact hashes; discard unverified partial output.

        Completed verified stages stay on disk (their role is NOT
        re-dispatched); only the pending stage continues (resume.py plan).
        """
        plan = resume_mod.plan_resume(state, self.case_dir)
        resume_mod.apply_plan(plan, self.case_dir)
        self._verified = dict(plan.verified)
        pending_stage = state["pending"].get("stage")
        if pending_stage is not None:
            target = plan.earliest_stage
            # engine artifacts regenerate inside the model stage
            if target == "engine":
                target = "model"
            if target in resume_mod.STAGE_INDEX and (
                    pending_stage not in resume_mod.STAGE_INDEX or
                    resume_mod.STAGE_INDEX[target] <
                    resume_mod.STAGE_INDEX.get(pending_stage, 99)):
                state["pending"]["stage"] = target
                state["pending"]["role"] = "generator"
                state["pending"]["retry_count"] = 0
        budget = self._budget(state)
        if budget.clock_started is None:
            budget.resume_clock()
        if budget.clock_started is not None:
            # Preserve the ORIGINAL start: rewriting it to "now" on every
            # resume reset elapsed_minutes to zero and made the case clock
            # unable to ever fire.
            state["pending"]["case_clock_started"] = _iso_from_epoch(
                budget.clock_started)
        self._sync_budget(state, budget)
        self._persist(state)
        self._trace(event="BUDGET_CHECK",
                    detail={"check": "resume", "verified": len(plan.verified),
                            "discarded": plan.discarded,
                            "broken_verified": plan.broken_verified,
                            "stage": state["pending"]["stage"]},
                    round=state["pending"]["round"])
        return state

    # ----------------------------------------------------------------- loop
    def loop(self, state: dict) -> dict:
        budget = self._budget(state)
        arts = Arts(round=state["pending"]["round"])
        guard = 0
        try:
            while state["status"] == "RUNNING":
                guard += 1
                if guard > 5000:
                    raise RuntimeError("orchestrator loop guard tripped")
                p = state["pending"]
                if p["role"] == "generator" and p["stage"] is None:
                    ok, reason = budget.check_new_round()
                    self._trace(event="BUDGET_CHECK", detail={
                        "check": "new_round", "ok": bool(ok),
                        "rounds_used": budget.used_rounds()},
                        round=p["round"])
                    if not ok:
                        # A case that has burned its rounds on the same
                        # objections is not a failed case -- it is a case that
                        # needs a human. Escalate to review with the objections
                        # listed, provided the candidate passed every gate and
                        # was fully reviewed. Exhaustion stays the answer when
                        # the work itself is unverified: a gate failure or a
                        # missing review is not something a human can sign off.
                        if self._can_escalate(state):
                            self._escalate_to_review(state, budget, reason)
                            return self._summary(state, "escalated to review")
                        self.exhausted(state, budget, reason)
                        return self._summary(state, "budgets exhausted")
                    budget.note_round()
                    # a build always enters a FRESH round dir (round numbers
                    # are monotonic; budget rounds are counted separately)
                    latest = self._latest_round_dir()
                    if (p["round"] or 0) <= latest:
                        p["round"] = latest + 1
                    p["stage"] = "ingest"
                    p["retry_count"] = 0
                    arts = Arts(round=p["round"])
                    state["candidate"]["gates_passed"] = False
                    self._sync_budget(state, budget)
                    self._transition(state, "RUNNING", "RUNNING",
                                     verified=f"rounds/round-{p['round']:03d} build start",
                                     detail={"round_start": p["round"]})
                    continue
                budget = self._budget(state)
                arts.round = state["pending"]["round"]
                self._advance(state, budget, arts)
        except StopLoop:
            pass
        return self._summary(state, "run advanced the case")

    def _advance(self, state: dict, budget: budget_mod.BudgetTracker,
                 arts: Arts) -> None:
        stage = state["pending"]["stage"]
        if stage in STAGE_ORDER[:STAGE_ORDER.index("gates")]:
            self._run_build_stage(state, budget, arts, stage)
        elif stage == "gates":
            self._run_gates(state, budget, arts)
        elif stage == "critics":
            self._run_critics(state, budget, arts)
        elif stage == "merge":
            self._run_merge_and_decide(state, budget, arts)
        else:
            raise RuntimeError(f"unknown pending stage {stage!r}")

    # ============================================================== dispatch
    def _bind_request(self, state: dict, role: str, payload: dict,
                      candidate_id: str | None) -> str:
        req_id = f"req-{statestore.trace_request_count(self.case_dir) + 1:04d}"
        request = {
            "request_id": req_id,
            "role": role,
            "round": state["pending"]["round"],
            "candidate_id": candidate_id,
            "payload_sha256": statestore.sha256_json(payload),
            "ts": self.now(),
        }
        statestore.write_request(self.case_dir, request)
        state["pending"]["request_id"] = req_id
        statestore.write_state(self.case_dir, state, now=self.now)
        self._trace(event="REQUEST_BOUND", request_id=req_id, role=role,
                    candidate_id=candidate_id,
                    round=state["pending"]["round"],
                    artifact="request.json",
                    artifact_sha256=statestore.sha256_json(request))
        return req_id

    def _dispatch_role(self, state: dict, budget: budget_mod.BudgetTracker,
                       role: str, payload: dict, candidate_id: str | None,
                       write_scope: list[str]) -> Any:
        """Budget check at EVERY dispatch -> binding -> dispatch -> trace."""
        call = self._prepare_dispatch(state, budget, role, payload,
                                      candidate_id, write_scope)
        return self._finish_dispatch(state, budget, role, call,
                                     self.dispatch(call))

    def _dispatch_concurrently(self, calls: list) -> list:
        """Run independent dispatches at the same time.

        Each call is its own process with its own prompt and workdir, so the
        only shared state is the backend's flag cache. Serializing the four
        critics added ~6 minutes to every round for no ordering benefit.
        """
        if len(calls) <= 1:
            return [self.dispatch(c) for c in calls]
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            return list(pool.map(self.dispatch, calls))

    def _prepare_dispatch(self, state: dict, budget: budget_mod.BudgetTracker,
                          role: str, payload: dict, candidate_id: str | None,
                          write_scope: list[str]) -> Any:
        """Budget check, request binding and the dispatch trace record."""
        ok, reason = budget.check_dispatch(role)
        self._trace(event="BUDGET_CHECK",
                    detail={"check": "dispatch", "role": role, "ok": bool(ok)},
                    round=state["pending"]["round"], role=role)
        if not ok:
            self.exhausted(state, budget, reason)
            raise StopLoop()
        req_id = self._bind_request(state, role, payload, candidate_id)
        call = RoleCall(role=role, request_id=req_id, candidate_id=candidate_id,
                        # version/sha are resolved from the card on disk by
                        # dispatch, so the trace records what actually ran
                        role_card={"path": f"prompts/{role}.md", "version": None,
                                   "sha256": None},
                        # the artifact this role must emit, so dispatch can
                        # enforce its required top-level keys
                        payload=payload, output_schema=output_schema_for(role),
                        budget={"tokens_max": budget.limits.role_tokens_max,
                                "timeout_seconds": budget.limits.timeout_for(role)},
                        write_scope=write_scope,
                        workdir=str(self.case_dir))
        self._trace(event="AGENT_DISPATCHED", request_id=req_id, role=role,
                    candidate_id=candidate_id, round=state["pending"]["round"])
        return call

    def _finish_dispatch(self, state: dict, budget: budget_mod.BudgetTracker,
                         role: str, call, result) -> Any:
        """Account for a completed dispatch and record its result."""
        req_id = call.request_id
        candidate_id = call.candidate_id
        budget.add_tokens(role, getattr(result, "tokens_in", None),
                          getattr(result, "tokens_out", None))
        self._sync_budget(state, budget)
        # merge the dispatch record so the prompt sha and the real role-card
        # version reach trace.jsonl instead of being dropped at the seam
        dispatch_trace = getattr(result, "trace", None) or {}
        detail = {"ok": bool(getattr(result, "ok", False)),
                  "error": getattr(result, "error", None)}
        detail.update(dispatch_trace.get("detail") or {})
        self._trace(event="AGENT_RESULT", request_id=req_id, role=role,
                    candidate_id=candidate_id, round=state["pending"]["round"],
                    backend=getattr(result, "backend", None),
                    tokens_in=getattr(result, "tokens_in", None),
                    tokens_out=getattr(result, "tokens_out", None),
                    prompt_versions=dispatch_trace.get("prompt_versions"),
                    detail=detail)
        return result

    def _validate_submission(self, role: str, req_id: str, result) -> str | None:
        """None when valid; else a precise machine-readable rejection reason."""
        parsed = getattr(result, "parsed", None)
        if not getattr(result, "ok", False) or not isinstance(parsed, dict):
            return ("no parseable output: "
                    f"{getattr(result, 'error', None) or 'backend error'}")
        if parsed.get("request_id") != req_id:
            return (f"stale request rejected: echoed request_id "
                    f"{parsed.get('request_id')!r} != bound {req_id!r}")
        if role == "ingest":
            if not isinstance(parsed.get("classification"), dict) or \
                    not isinstance(parsed.get("slices"), list) or not parsed["slices"]:
                return "ingest submission must include classification and non-empty slices"
        elif role == "extractor":
            if not isinstance(parsed.get("facts"), list) or not parsed["facts"]:
                return "extraction submission must include a non-empty facts array"
        elif role == "model":
            if not isinstance(parsed.get("segments"), list) or not parsed["segments"] \
                    or not isinstance(parsed.get("margin_glide"), dict):
                return "model submission must include segments and margin_glide"
        elif role == "writer":
            if not isinstance(parsed.get("report_md"), str) or \
                    not parsed["report_md"].strip():
                return "writer submission must include non-empty report.md text"
        elif role == "generator_lead":
            if not isinstance(parsed.get("reports"), list) or \
                    not isinstance(parsed.get("evidence_index"), dict):
                return "lead submission must include reports and an evidence index"
        elif role.startswith("critic_c"):
            if parsed.get("verdict") not in ("clean", "findings"):
                return "critic submission must carry verdict clean|findings"
        elif role == "critic_lead":
            for k in ("clean", "coverage_ok", "reviewer_count"):
                if k not in parsed:
                    return f"merged report missing required key {k!r}"
        return None

    def _block_if_unrunnable(self, state: dict, role: str, result) -> None:
        """A failure no retry can fix (absent inputs) blocks instead of retrying."""
        if not getattr(result, "blocking", False):
            return
        error = getattr(result, "error", None)
        self.blocked(state, "missing_inputs",
                     f"role {role} cannot run: {error}",
                     {"role": role, "error": error})
        raise StopLoop()

    def _handle_invalid(self, state: dict, budget: budget_mod.BudgetTracker,
                        role: str, error: str) -> None:
        """Repair at the SAME role, new request, WITHOUT consuming a round.

        The retries budget (mandate ``retries_max``) decides how many repairs
        a role gets; exhausting it transitions to EXHAUSTED. A hard block is
        reserved for the case where the repair itself cannot be attempted.
        """
        p = state["pending"]
        used = budget.note_retry(role)
        ok, reason = budget.check_retry(role)
        if not ok:
            self.exhausted(state, budget, reason)
            raise StopLoop()
        p["retry_count"] += 1
        self._sync_budget(state, budget)
        self._persist(state)
        self._trace(event="STATE_TRANSITION",
                    detail={"repair_at_same_role": role, "error": error,
                            "retries": used, "round_consumed": False},
                    role=role, round=p["round"],
                    request_id=p["request_id"],
                    candidate_id=state["candidate"]["candidate_id"])

    # ================================================================ build
    def _artifact_intact(self, rel: str) -> bool:
        want = self._verified.get(rel)
        path = self.case_dir / rel
        return bool(want) and path.exists() and statestore.sha256_file(path) == want

    def _load_verified(self, rel: str) -> Any:
        path = self.case_dir / rel
        if rel.endswith(".md") or rel.endswith(".txt"):
            return path.read_text(encoding="utf-8")
        return statestore.read_json(path)

    def _lazy(self, arts: Arts, rel: str) -> Any:
        """Load a verified artifact from disk exactly once (no re-dispatch)."""
        if rel in arts.content:
            return arts.content[rel]
        if self._artifact_intact(rel):
            arts.content[rel] = self._load_verified(rel)
        return arts.content.get(rel)

    def _stamp_provenance(self, content: Any) -> Any:
        """Give every candidate artifact a structured cutoff + bundle header.

        The critic checklist requires each artifact to state the cutoff and the
        input manifest it was built from. Only the writer's markdown carried
        them; the JSON artifacts had no schema slot, so the extractor had to
        describe its own header in prose and the checklist could not be
        satisfied by any build. The block is constant for the whole case, so
        it does not disturb candidate identity.
        """
        if not isinstance(content, dict):
            return content
        out = dict(content)
        out["provenance"] = self._bundle_provenance()
        return out

    def _write_artifact(self, state: dict, budget: budget_mod.BudgetTracker,
                        arts: Arts, team: str, name: str, content: Any) -> str:
        rel = arts.rel(team, name)
        if team == "generator" and not isinstance(content, str):
            content = self._stamp_provenance(content)
        sha = (statestore.atomic_write_text(self.case_dir / rel, content)
               if isinstance(content, str)
               else statestore.atomic_write_json(self.case_dir / rel, content))
        arts.files[rel] = sha
        arts.content[rel] = content
        self._verified[rel] = sha
        self._sync_budget(state, budget)
        self._transition(state, "RUNNING", "RUNNING", verified=rel,
                         detail={"artifact_written": rel})
        return rel

    def _carried(self) -> list[dict]:
        """Blocking findings only: a minor is advisory, not a repair task.

        Handing the critics every open minor made each round re-raise the
        previous round's nits, which is why the ledger never drained.
        """
        return self.ledger().blocking_findings()

    def _run_build_stage(self, state: dict, budget: budget_mod.BudgetTracker,
                         arts: Arts, stage: str) -> None:
        role = STAGE_ROLE[stage]
        rel = arts.rel("generator", STAGE_ARTIFACT[stage])

        # ingest is a pure function of the frozen input manifest. Re-dispatching
        # it every round re-reads every filing and spends the whole role
        # deadline on tool calls without emitting output, so a rebuild could
        # never get past its first stage.
        if stage == "ingest" and not self._artifact_intact(rel):
            self._carry_ingest_forward(state, budget, arts)
        if stage == "extractor":
            self._carry_extractor_forward(state, budget, arts)

        # resume fast-path: verified artifact on disk -> role NOT re-dispatched
        if self._artifact_intact(rel):
            self._stage_side_effects(state, budget, arts, stage)
            self._stage_done(state, stage)
            return

        candidate_id = None
        if stage == "ingest":
            payload = {"case_id": state["case_id"], "round": arts.round,
                       "input_manifest": self.input_manifest(),
                       "mandate": self.mandate(),
                       "anchor_candidates": self._anchor_candidates(),
                       # which documents must be extracted, not just which
                       # one triggered the case
                       "extraction_scope": self._extraction_scope(),
                       "carried_findings": self._carried()}
        else:
            payload = {"case_id": state["case_id"], "round": arts.round,
                       "carried_findings": self._carried(),
                       # every role works under the frozen mandate: the writer
                       # needs the report policy and cutoff, the model needs
                       # the forecast bounds. Withholding it made the writer
                       # report the bounds as "not supplied".
                       "mandate": self.mandate(),
                       # provenance the note's header block must carry; the
                       # writer cannot invent a bundle id it was never given
                       "bundle": self._bundle_provenance(),
                       "prior_artifacts": self._stage_inputs(arts, stage)}
        if stage == "candidate":
            # the candidate id is only known once all artifacts exist:
            # stamp headers, THEN assemble the manifest payload
            candidate_id = self._candidate_id(arts)
            self._stamp_candidate_id(state, arts, candidate_id)
            payload.update({
                "candidate_id": candidate_id,
                "cutoff_date": self.mandate()["cutoff_date"],
                "input_manifest_sha256": statestore.sha256_json(
                    self.input_manifest()),
                "artifacts": self._candidate_artifact_list(arts),
                "evidence_index": self._evidence_index(arts),
                "scope_statement": ("full rebuild; stages may short-circuit internal "
                                    "work via hash-match proofs; carried findings "
                                    "must be addressed"),
            })
        while True:
            result = self._dispatch_role(state, budget, role, payload,
                                         candidate_id, [rel])
            self._block_if_unrunnable(state, role, result)
            err = self._validate_submission(role, state["pending"]["request_id"],
                                            result)
            if err is None and stage == "candidate":
                err = self._check_lead_manifest(arts, result.parsed)
                if err is None:
                    result.parsed = self._stamp_manifest_header(arts, result.parsed)
            if err is None and stage == "ingest":
                # the declared slices must exist on disk before the extractor
                # is handed their paths
                err = self._materialize_slices(result.parsed)
            if err is None:
                state["pending"]["retry_count"] = 0
                break
            self._handle_invalid(state, budget, role, err)
            payload = dict(payload)
            payload["submission_feedback"] = err    # same-role repair packet
        parsed = result.parsed
        content = parsed["report_md"] if role == "writer" else parsed
        self._write_artifact(state, budget, arts, "generator",
                             STAGE_ARTIFACT[stage], content)
        if stage == "ingest":
            # bind the report to the inputs it was produced against, so a
            # later round can prove it is still current
            statestore.atomic_write_text(
                self.case_dir / arts.rel("generator", INGEST_INPUTS_SIDECAR),
                statestore.sha256_json(self.input_manifest()) + "\n")
        self._stage_side_effects(state, budget, arts, stage)
        self._stage_done(state, stage)

    def _stage_inputs(self, arts: Arts, stage: str) -> dict:
        """Role read-scope: exactly the inputs the role card contract allows."""
        out: dict[str, Any] = {}
        if stage in ("extractor", "model", "writer", "candidate"):
            out["ingest_report"] = self._lazy(arts, arts.rel("generator",
                                                             "ingest-report.json"))
            out["slices"] = self._slice_payload(
                (out["ingest_report"] or {}).get("slices", []),
                # the extractor reads the statements themselves: inline the
                # text so its prompt is self-contained. A prompt that only
                # names paths forces the model to go read files, which turns
                # one completion into a multi-minute agentic exploration.
                inline_text=(stage == "extractor"))
        if stage in ("model", "writer", "candidate"):
            out["facts"] = self._lazy(arts, arts.rel("generator", "facts.json"))
        if stage in ("writer", "candidate"):
            out["forecast"] = self._lazy(arts, arts.rel("generator",
                                                        "forecast_assumptions.json"))
            # The writer card's read scope is "verified facts, model, valuation
            # artifacts" and it forbids a valuation stance without the computed
            # DCF/comps. Withholding them left the note able to say only that
            # the valuation artifacts were "absent from the bound request".
            out["model"] = self._lazy(arts, arts.rel("generator", "model.json"))
            out["valuation"] = self._lazy(arts, arts.rel("generator",
                                                         "valuation.json"))
        return out

    def _slice_payload(self, slices: Any, *, inline_text: bool) -> list:
        """Slice descriptors, optionally carrying the slice text itself.

        The text is read from the case directory (the slicer wrote it there)
        and only when the file still hashes to the declared sha256, so a
        tampered slice can never reach a role as if it were evidence.
        """
        out: list = []
        for entry in slices or []:
            if not isinstance(entry, dict):
                out.append(entry)
                continue
            item = dict(entry)
            if inline_text:
                rel = item.get("path")
                path = self.case_dir / rel if rel else None
                text = None
                if path is not None and path.is_file():
                    want = item.get("sha256")
                    if not want or statestore.sha256_file(path) == want:
                        text = path.read_text(encoding="utf-8")
                item["text"] = text
            out.append(item)
        return out

    def _materialize_slices(self, parsed: dict) -> str | None:
        """Produce the slice files ingest declared, verifying its sha256 claims.

        Ingest only *declares* slices; the deterministic slicer produces them.
        Nothing called the slicer before, so a real ingest left the extractor's
        payload references pointing at files that never existed. Returns None
        on success, or a repairable error for the ingest role.
        """
        from . import slicer

        declared = parsed.get("slices")
        if not isinstance(declared, list) or not declared:
            return "ingest submission must declare at least one slice"
        manifest = self.input_manifest()
        docs = {d.get("doc_id"): d for d in manifest.get("documents") or []
                if isinstance(d, dict)}
        inputs_dir = self.case_dir / "inputs"
        sliced: set[str] = set()
        for entry in declared:
            if not isinstance(entry, dict):
                return f"slice entry is not an object: {entry!r}"
            slice_id = entry.get("slice_id")
            rel = entry.get("path")
            if not isinstance(slice_id, str) or not isinstance(rel, str):
                return (f"slice entry {entry!r} must carry string slice_id and path")
            doc_id = slice_id.split("/")[0]
            doc = docs.get(doc_id)
            if doc is None:
                return (f"slice {slice_id!r} names document {doc_id!r}, which is "
                        "not in inputs/manifest.json")
            # the slicer reports slice paths relative to inputs/ ("slices/...")
            # while the case contract writes them case-relative
            # ("inputs/slices/..."); accept either rather than rejecting a
            # verbatim copy of what the request handed the role
            target = self.case_dir / rel
            if not target.is_file() and (inputs_dir / rel).is_file():
                target = inputs_dir / rel
            if not target.is_file() and doc_id not in sliced:
                doc_path = self.case_dir / str(doc.get("path") or "")
                if not doc_path.is_file():
                    return (f"slice {slice_id!r} needs {doc.get('path')!r}, which "
                            "is absent from the case inputs")
                try:
                    slicer.slice_document(doc_path, str(doc.get("form") or ""),
                                          out_dir=inputs_dir)
                except slicer.SliceError as exc:
                    return f"cannot slice {doc_id}: {exc}"
                sliced.add(doc_id)
                if not target.is_file() and (inputs_dir / rel).is_file():
                    target = inputs_dir / rel
            if not target.is_file():
                return (f"slice {slice_id!r} declares {rel}, but the slicer "
                        f"produced no such slice for {doc_id}")
            claimed = entry.get("sha256")
            actual = statestore.sha256_file(target)
            if isinstance(claimed, str) and claimed and claimed != actual:
                return (f"slice {slice_id!r} declares sha256 {claimed[:12]}, but "
                        f"{rel} hashes to {actual[:12]}")
        return None

    def _stage_side_effects(self, state: dict, budget: budget_mod.BudgetTracker,
                            arts: Arts, stage: str) -> None:
        if stage == "model":
            self._ensure_engine_artifacts(state, budget, arts)
        if stage == "candidate":
            prev = state["candidate"]["candidate_id"]
            new_id = self._candidate_id(arts)
            # candidate_id semantics: changes on ANY candidate artifact change;
            # any candidate change resets the clean-review streak
            if prev is not None and new_id != prev:
                state["candidate"]["clean_review_rounds"] = 0
                state["candidate"]["last_review_candidate"] = None
            state["candidate"]["candidate_id"] = new_id
            state["candidate"]["built_round"] = arts.round
            state["candidate"]["gates_passed"] = False

    def _stage_done(self, state: dict, stage: str) -> None:
        idx = STAGE_ORDER.index(stage)
        state["pending"]["stage"] = STAGE_ORDER[idx + 1]

    # ------------------------------------------------------------- engines
    def _ensure_engine_artifacts(self, state: dict, budget: budget_mod.BudgetTracker,
                                 arts: Arts) -> None:
        """Compute deterministic model.json/valuation.json (contract engines).

        Idempotent: engines are pure and deterministic, so recomputation only
        changes bytes when inputs changed. Verified engine artifacts on disk
        are reused (crash-safe, no LLM, no drift).
        """
        facts = self._lazy(arts, arts.rel("generator", "facts.json"))
        forecast = self._lazy(arts, arts.rel("generator",
                                             "forecast_assumptions.json"))
        if facts is None or forecast is None:
            raise RuntimeError("engine stage missing facts/forecast")
        model_rel = arts.rel("generator", "model.json")
        valuation_rel = arts.rel("generator", "valuation.json")
        if self._artifact_intact(model_rel) and self._artifact_intact(valuation_rel):
            arts.content[model_rel] = self._load_verified(model_rel)
            arts.content[valuation_rel] = self._load_verified(valuation_rel)
            arts.files[model_rel] = self._verified[model_rel]
            arts.files[valuation_rel] = self._verified[valuation_rel]
            return
        if self.gate_engine.build_model_fn is None or \
                self.gate_engine.value_fn is None:
            raise RuntimeError("engine callables not injected "
                               "(contracts interfaces section 4)")
        mandate = self.mandate()
        try:
            model = self.gate_engine.build_model_fn(
                facts, state["config_versions"]["mapping_tables"])
        except Exception as exc:
            # A deterministic engine rejecting its inputs is a fact-quality
            # problem, not a crash: block with the precise violation so the
            # case can be repaired and re-run instead of stranding RUNNING.
            self.blocked(
                state, "engine_violation",
                f"the model engine rejected the extracted facts: {exc}",
                {"stage": "model", "error": str(exc),
                 "facts_sha256": statestore.sha256_json(facts)})
            raise StopLoop()
        model = model.to_dict() if hasattr(model, "to_dict") else dict(model)
        # The declared input digest must be the digest of the artifact AS
        # WRITTEN. atomic_write_json pretty-prints, so sha256_json(obj) is the
        # canonical-compact hash and matches no file on disk -- the provenance
        # chain from facts.json into model.json then resolved to nothing.
        facts_rel = arts.rel("generator", "facts.json")
        forecast_rel = arts.rel("generator", "forecast_assumptions.json")
        model["input_facts_sha256"] = (self._verified.get(facts_rel)
                                       or statestore.sha256_json(facts))
        model["input_forecast_sha256"] = (self._verified.get(forecast_rel)
                                          or statestore.sha256_json(forecast))
        model["candidate_id"] = state["candidate"]["candidate_id"]
        model["cutoff"] = mandate["cutoff_date"]
        try:
            valuation = self.gate_engine.value_fn(
                model, forecast, mandate["assumptions"])
        except Exception as exc:
            self.blocked(
                state, "engine_violation",
                f"the valuation engine rejected the model: {exc}",
                {"stage": "valuation", "error": str(exc),
                 "facts_sha256": model["input_facts_sha256"]})
            raise StopLoop()
        valuation = (valuation.to_dict() if hasattr(valuation, "to_dict")
                     else dict(valuation))
        valuation["input_facts_sha256"] = model["input_facts_sha256"]
        valuation["candidate_id"] = state["candidate"]["candidate_id"]
        valuation["cutoff"] = mandate["cutoff_date"]
        for rel, payload in ((model_rel, model), (valuation_rel, valuation)):
            payload = self._stamp_provenance(payload)
            sha = statestore.atomic_write_json(self.case_dir / rel, payload)
            arts.files[rel] = sha
            arts.content[rel] = payload
            self._verified[rel] = sha
        # deterministic engines spend no tokens, but the per-role ledger must
        # still name the role or the cap check cannot be completed for it
        budget.add_tokens("engines", 0, 0)
        self._sync_budget(state, budget)
        self._transition(state, "RUNNING", "RUNNING", verified=model_rel,
                         detail={"engine_artifacts": [model_rel, valuation_rel]})

    # ------------------------------------------------------------- candidate
    def _candidate_artifact_list(self, arts: Arts) -> list[dict]:
        """Manifest reports entries: on-disk (stamped) artifact hashes."""
        rels = [arts.rel("generator", name) for name in CANDIDATE_FILES]
        return [{"role": role, "artifact": rel,
                 "sha256": self._verified.get(rel)}
                for role, rel in zip(CANDIDATE_ROLES, rels)]

    def _critic_artifacts(self, arts: Arts, role: str) -> dict:
        """The candidate artifacts *role* is contracted to read, with content."""
        out: dict[str, dict] = {}
        for name in CRITIC_ARTIFACTS.get(role, ()):
            rel = arts.rel("generator", name)
            content = self._lazy(arts, rel)
            if content is not None:
                out[rel] = content
        return out

    def _candidate_id(self, arts: Arts) -> str:
        """candidate_id = digest over the candidate's artifact CONTENT.

        Stamping ``candidate_id`` into artifacts (headers per contracts §6)
        happens AFTER the id is known, so the digest excludes the stamped key:
        same candidate content -> same id, regardless of stamp state (crash-
        and resume-safe).
        """
        digest_pairs = []
        for name in CANDIDATE_FILES:
            rel = arts.rel("generator", name)
            content = self._lazy(arts, rel)
            # digest the artifact NAME, not the round-scoped path: identical
            # content rebuilt in round 2 must yield the same candidate id
            if isinstance(content, dict):
                stripped = {k: v for k, v in content.items()
                            if k not in _IDENTITY_EXCLUDED}
                digest_pairs.append([name, statestore.sha256_json(stripped)])
            elif isinstance(content, str):
                # the note's header carries the id we are about to compute;
                # digest it with that value blanked or stamping changes the id
                digest_pairs.append(
                    [name, statestore.sha256_json(_blank_candidate(content))])
            else:
                path = self.case_dir / rel
                digest_pairs.append(
                    [name, statestore.sha256_file(path) if path.exists() else None])
        return "cand-" + statestore.sha256_json(digest_pairs)[:12]

    def _stamp_candidate_id(self, state: dict, arts: Arts, cand_id: str) -> None:
        """Write the candidate_id header into the candidate artifacts."""
        for name in CANDIDATE_FILES:
            rel = arts.rel("generator", name)
            content = self._lazy(arts, rel)
            if not isinstance(content, dict):
                continue
            if content.get("candidate_id") == cand_id and self._artifact_intact(rel):
                continue
            new_content = dict(content)
            new_content["candidate_id"] = cand_id
            sha = statestore.atomic_write_json(self.case_dir / rel, new_content)
            self._verified[rel] = sha
            arts.files[rel] = sha
            arts.content[rel] = new_content
        # The engines declare the digest of the facts/forecast they consumed.
        # Stamping the candidate id rewrites those files, so the declared
        # digests must be refreshed or the provenance chain points at bytes
        # that no longer exist on disk. Both keys are excluded from candidate
        # identity, so refreshing them cannot move the id.
        facts_sha = self._verified.get(arts.rel("generator", "facts.json"))
        forecast_sha = self._verified.get(
            arts.rel("generator", "forecast_assumptions.json"))
        for name, updates in (
                ("model.json", {"input_facts_sha256": facts_sha,
                                "input_forecast_sha256": forecast_sha}),
                ("valuation.json", {"input_facts_sha256": facts_sha})):
            rel = arts.rel("generator", name)
            content = self._lazy(arts, rel)
            if not isinstance(content, dict):
                continue
            new_content = dict(content)
            new_content.update({k: v for k, v in updates.items() if v})
            if new_content == content:
                continue
            sha = statestore.atomic_write_json(self.case_dir / rel, new_content)
            self._verified[rel] = sha
            arts.files[rel] = sha
            arts.content[rel] = new_content
        self._stamp_report_candidate(arts, cand_id)
        self._persist(state)

    def _stamp_report_candidate(self, arts: Arts, cand_id: str) -> None:
        """Fill the candidate id into the note's header block.

        The writer cannot know it: the id is a digest over every artifact, so
        it only exists once the note itself is written. Leaving the writer to
        guess produced "not assigned" in the header a reviewer checks.
        """
        rel = arts.rel("generator", "report.md")
        text = self._lazy(arts, rel)
        if not isinstance(text, str):
            return
        stamped, n = re.subn(r"\*\*Candidate:\*\*[^·\n]*",
                             f"**Candidate:** {cand_id} ", text, count=1)
        if not n or stamped == text:
            return
        sha = statestore.atomic_write_text(self.case_dir / rel, stamped)
        self._verified[rel] = sha
        arts.files[rel] = sha
        arts.content[rel] = stamped

    def _evidence_index(self, arts: Arts) -> dict[str, str]:
        idx: dict[str, str] = {}
        facts = self._lazy(arts, arts.rel("generator", "facts.json")) or {}
        model = self._lazy(arts, arts.rel("generator", "model.json")) or {}
        valuation = self._lazy(arts, arts.rel("generator", "valuation.json")) or {}
        facts_rel = arts.rel("generator", "facts.json")
        model_rel = arts.rel("generator", "model.json")
        valuation_rel = arts.rel("generator", "valuation.json")
        for f in facts.get("facts", []):
            if f.get("field"):
                idx[f["field"]] = facts_rel
        for key in model.get("series", {}):
            idx.setdefault(key, model_rel)
        for alias in _growth_aliases(model):
            idx.setdefault(alias, model_rel)
        # The writer cites the computed valuation by dotted path
        # (dcf.base.ebitda_margin, dcf.terminal.gordon.terminal_value,
        # cross_check_delta). Exposing only the first level left every
        # valuation number unciteable, so the note could not state a value.
        for key in _dotted_paths(valuation):
            idx.setdefault(key, valuation_rel)
        return idx

    def _check_lead_manifest(self, arts: Arts, manifest: dict) -> str | None:
        """The lead must echo the orchestrator-computed candidate manifest."""
        expected = {r["artifact"]: r["sha256"]
                    for r in self._candidate_artifact_list(arts)}
        reported = {r.get("artifact"): r.get("sha256")
                    for r in manifest.get("reports", [])}
        if reported != expected:
            return ("candidate manifest integrity mismatch: the lead did not "
                    "echo the verified artifact hashes")
        return None

    def _stamp_manifest_header(self, arts: Arts, manifest: dict) -> dict:
        """Write the provenance header the harness knows into the manifest.

        Critics check that every artifact can be tied to the cutoff, the input
        bundle, and the versions that produced it. Those are facts the harness
        holds, not things to ask a model to echo -- asking produced a manifest
        with none of them, and the same "header incomplete" finding every round.
        """
        from . import _prompt_assembly as pa
        from . import __version__ as engine_version

        inputs = self.input_manifest()
        mandate = self.mandate()
        prompt_versions: dict[str, str] = {}
        for role in sorted(set(STAGE_ROLE.values()) | set(CRITIC_ROLES)
                           | {"critic_lead"}):
            card = pa.default_root() / "prompts" / f"{role}.md"
            if card.is_file():
                prompt_versions[role] = pa.parse_role_card(card)["version"]
        model = self._lazy(arts, arts.rel("generator", "model.json")) or {}
        out = dict(manifest)
        out.update({
            "cutoff_date": mandate.get("cutoff_date"),
            "input_manifest_sha256": statestore.sha256_json(inputs),
            "bundle_id": inputs.get("bundle_id"),
            "bundle_sha256": inputs.get("bundle_sha256"),
            "prompt_versions": prompt_versions,
            "engine_versions": {
                "mapping": model.get("mapping_tables") or "1.0",
                "engine": engine_version,
            },
            "scope_statement": self._scope_statement(arts),
        })
        return out

    def _scope_statement(self, arts: Arts) -> str:
        """What this round actually did, from the artifact hashes.

        The lead used to assert "full rebuild" every round. Once ingest is
        reused across rounds that claim is false, and a critic that checks it
        finds the rebuild unevidenced. The hashes are the evidence.
        """
        prev = 0
        for n in range(arts.round - 1, 0, -1):
            if (self.case_dir / Arts(round=n).rel("generator", "facts.json")).is_file():
                prev = n
                break
        reused: list[str] = []
        rebuilt: list[str] = []
        for name in CANDIDATE_FILES:
            rel = arts.rel("generator", name)
            sha = self._verified.get(rel)
            if sha is None:
                continue
            if prev and self._verified.get(Arts(round=prev).rel("generator", name)) == sha:
                reused.append(name)
            else:
                rebuilt.append(name)
        parts = []
        if rebuilt:
            parts.append("re-emitted " + ", ".join(rebuilt))
        if reused:
            parts.append("reused byte-identical from round "
                         f"{prev:03d}: " + ", ".join(reused))
        if prev and self._ingest_carried_from(arts, prev):
            parts.append("ingest classification and slices carried from round "
                         f"{prev:03d} under a fresh request binding")
        return ("Round " + f"{arts.round:03d} build: " + "; ".join(parts) +
                ". Reuse is a hash-match proof against the previous round, not "
                "a skipped stage.")

    def _ingest_carried_from(self, arts: Arts, prev: int) -> bool:
        """True when this round's ingest report is the previous round's work."""
        here = self._lazy(arts, arts.rel("generator", "ingest-report.json"))
        there = self._lazy(Arts(round=prev),
                           Arts(round=prev).rel("generator", "ingest-report.json"))
        if not isinstance(here, dict) or not isinstance(there, dict):
            return False
        keys = ("doc_id", "classification", "slices", "superseded")
        return all(here.get(k) == there.get(k) for k in keys)

    # ================================================================ gates
    def _run_gates(self, state: dict, budget: budget_mod.BudgetTracker,
                   arts: Arts) -> None:
        ctx = self._candidate_context(state, arts)
        report = self.gate_engine.run_all(ctx)
        gates_rel = self._rel_gates(arts.round)
        payload = {
            "round": arts.round,
            "candidate_id": state["candidate"]["candidate_id"],
            "passed": report.passed,
            "results": [{"gate": o.gate, "ok": o.ok, "findings": o.findings}
                        for o in report.results],
        }
        sha = statestore.atomic_write_json(self.case_dir / gates_rel, payload)
        self._verified[gates_rel] = sha
        self._trace(event="GATE_RUN", round=arts.round,
                    candidate_id=state["candidate"]["candidate_id"],
                    artifact=gates_rel, artifact_sha256=sha,
                    detail={"gates": [o.gate for o in report.results]})
        led = self.ledger()
        reproduced: dict[str, set[str]] = {}
        for outcome in report.results:
            self._trace(event="GATE_PASS" if outcome.ok else "GATE_FAIL",
                        round=arts.round, role=outcome.gate,
                        candidate_id=state["candidate"]["candidate_id"],
                        artifact=gates_rel, artifact_sha256=sha,
                        detail={"gate": outcome.gate})
            for f in outcome.findings:
                f = dict(f)
                f["source_critic"] = f"gate:{outcome.gate}"
                reproduced.setdefault(outcome.gate, set()).add(
                    ledger_mod.compute_dedup_key(f))
                fid = led.open_finding(f, round=arts.round)
                self._trace(event="FINDING_OPENED",
                            candidate_id=state["candidate"]["candidate_id"],
                            round=arts.round, role=outcome.gate,
                            detail={"finding_id": fid, "summary": f["summary"],
                                    "gate": outcome.gate})
        # A gate finding the current run did not reproduce is closed by the
        # gate itself: it re-ran on this candidate and the defect is gone. Only
        # a critic can close a critic finding, but a gate finding whose gate
        # now passes has been verified fixed — leaving it open carries a
        # phantom defect into every later repair packet.
        ran = {o.gate for o in report.results}
        for row in led.open_findings():
            src = row.get("source_critic") or ""
            if not src.startswith("gate:"):
                continue
            gate = src.split(":", 1)[1]
            if gate not in ran or row.get("dedup_key") in reproduced.get(gate, set()):
                continue
            led.close_finding(row["finding_id"], critic=src, round=arts.round,
                              verified_evidence={"artifact": gates_rel,
                                                 "sha256": sha})
            self._trace(event="FINDING_VERIFIED_CLOSED",
                        candidate_id=state["candidate"]["candidate_id"],
                        round=arts.round, role=gate,
                        artifact=gates_rel, artifact_sha256=sha,
                        detail={"finding_id": row["finding_id"],
                                "reason": "gate no longer reproduces it"})
        state["candidate"]["gates_passed"] = report.passed
        if report.passed:
            state["pending"]["stage"] = "critics"
            self._sync_budget(state, budget)
            self._transition(state, "RUNNING", "RUNNING", verified=gates_rel,
                             detail={"all_gates_pass": True})
        else:
            self._trace(event="GATE_FAIL", round=arts.round,
                        candidate_id=state["candidate"]["candidate_id"],
                        artifact=gates_rel, artifact_sha256=sha,
                        detail={"halting_round": True,
                                "finding_count": len(report.finding_dicts)})
            self._issue_repair_packet(state, arts, led,
                                      note="numerical gate failures")

    def _rel_gates(self, round_no: int) -> str:
        return f"rounds/round-{round_no:03d}/numerical/gates.json"

    def _latest_round_dir(self) -> int:
        rounds_dir = self.case_dir / "rounds"
        if not rounds_dir.exists():
            return 0
        import re as _re
        best = 0
        for child in rounds_dir.iterdir():
            m = _re.fullmatch(r"round-(\d{3})", child.name)
            if m and child.is_dir():
                best = max(best, int(m.group(1)))
        return best

    # ------------------------------------------------------- ingest reuse
    def _bundle_provenance(self) -> dict:
        """The cutoff and input-bundle identity every artifact header cites."""
        inputs = self.input_manifest()
        return {
            "cutoff_date": self.mandate().get("cutoff_date"),
            "bundle_id": inputs.get("bundle_id"),
            "bundle_sha256": inputs.get("bundle_sha256"),
            "input_manifest_sha256": statestore.sha256_json(inputs),
        }

    def _extraction_scope(self) -> list[dict]:
        """The documents whose statements the model needs.

        A case is triggered on one filing, but a statements model needs an
        annual anchor as well: a 10-Q prints no annual income statement, so
        extracting only the trigger leaves every FY column empty and the
        workbook has no income statement at all.
        """
        cutoff = str(self.mandate().get("cutoff_date") or "")
        docs = [d for d in (self.input_manifest().get("documents") or [])
                if isinstance(d, dict)
                and str(d.get("filed") or "") <= cutoff]

        def newest(form: str) -> dict | None:
            cands = [d for d in docs if str(d.get("form") or "") == form]
            return max(cands, key=lambda d: str(d.get("filed") or ""),
                       default=None)

        trigger = newest("10-Q") or newest("10-K")
        annual = newest("10-K")
        out: list[dict] = []
        for doc, role in ((trigger, "trigger"), (annual, "annual_anchor")):
            if not doc or doc.get("doc_id") in [x["doc_id"] for x in out]:
                continue
            out.append({"doc_id": doc.get("doc_id"), "form": doc.get("form"),
                        "filed": doc.get("filed"), "role": role})
        return out

    def _anchor_candidates(self) -> dict[str, list[dict]]:
        """The slices each input document yields, computed by the slicer.

        Ingest's card says it does not read whole filings -- the request gives
        it the slicer's anchor candidates. Without them it opened all twelve
        documents with tools and spent the whole role deadline doing it.
        """
        from . import slicer

        out: dict[str, list[dict]] = {}
        for doc in self.input_manifest().get("documents") or []:
            if not isinstance(doc, dict):
                continue
            doc_id = doc.get("doc_id")
            path = self.case_dir / str(doc.get("path") or "")
            if not doc_id or not path.is_file():
                continue
            try:
                slices = slicer.candidate_slices(path, str(doc.get("form") or ""))
            except slicer.SliceError:
                continue
            # the slicer reports paths relative to inputs/ ("slices/..."), but
            # every consumer resolves them against the case dir. Hand out the
            # case-relative form so a verbatim copy is the correct one.
            entries = []
            for sl in slices:
                entry = sl.to_dict()
                rel = str(entry.get("path") or "")
                if rel and not rel.startswith("inputs/"):
                    entry["path"] = f"inputs/{rel}"
                entries.append(entry)
            out[doc_id] = entries
        return out

    def _ingest_report_reusable(self, prev: int, manifest_sha: str) -> dict | None:
        """The previous round's ingest report, if it still describes our inputs.

        Ingest classifies the frozen input manifest and declares slices; it
        reads no round state. Re-running it every round re-reads all twelve
        filings and spends the whole role deadline on tool calls without ever
        emitting output, so a hash match is proof enough to carry it forward.
        """
        prev_arts = Arts(round=prev)
        rel = prev_arts.rel("generator", "ingest-report.json")
        path = self.case_dir / rel
        if not path.is_file():
            return None
        report = statestore.read_json_or_none(path)
        if not isinstance(report, dict):
            return None
        sidecar = self.case_dir / prev_arts.rel("generator", INGEST_INPUTS_SIDECAR)
        if sidecar.is_file():
            if sidecar.read_text(encoding="utf-8").strip() != manifest_sha:
                return None
        else:
            # Rounds written before the sidecar existed: prove the report
            # against the inputs we actually hold instead of a recorded hash.
            docs = {d.get("doc_id") for d in self.input_manifest().get("documents") or []
                    if isinstance(d, dict)}
            if report.get("doc_id") not in docs:
                return None
        for entry in report.get("slices") or []:
            if not isinstance(entry, dict):
                return None
            target = self.case_dir / str(entry.get("path") or "")
            if not target.is_file():
                return None
            if entry.get("sha256") and statestore.sha256_file(target) != entry["sha256"]:
                return None
        return report

    def _carry_ingest_forward(self, state: dict, budget: budget_mod.BudgetTracker,
                              arts: Arts) -> bool:
        """Seed this round's ingest report from a verified earlier round."""
        rel = arts.rel("generator", "ingest-report.json")
        if self._artifact_intact(rel):
            return True
        manifest_sha = statestore.sha256_json(self.input_manifest())
        for prev in range(self._latest_round_dir(), 0, -1):
            if prev >= arts.round:
                continue
            report = self._ingest_report_reusable(prev, manifest_sha)
            if report is None:
                continue
            # the report answers a request; bind the one this round would have
            # issued so the artifact does not echo a stale request_id
            payload = {"case_id": state["case_id"], "round": arts.round,
                       "input_manifest": self.input_manifest(),
                       "mandate": self.mandate(),
                       "carried_findings": self._carried()}
            req_id = self._bind_request(state, "ingest", payload, None)
            report = dict(report)
            report["request_id"] = req_id
            report["candidate_id"] = None   # stamped at the candidate stage
            sha = statestore.atomic_write_json(self.case_dir / rel, report)
            statestore.atomic_write_text(
                self.case_dir / arts.rel("generator", INGEST_INPUTS_SIDECAR),
                manifest_sha + "\n")
            self._verified[rel] = sha
            arts.files[rel] = sha
            arts.content[rel] = report
            # a reused report costs nothing, but the per-role ledger must still
            # name the role or the cap check cannot be completed for it
            budget.add_tokens("ingest", 0, 0)
            budget.note_reuse("ingest")
            self._sync_budget(state, budget)
            # checkpoint the carried artifact: without this a crash before the
            # next write leaves it unverified, and the next resume discards it
            self._persist(state)
            self._trace(event="INGEST_REUSED",
                        detail={"from_round": prev, "artifact": rel,
                                "request_id": req_id,
                                "input_manifest_sha256": manifest_sha},
                        round=arts.round)
            return True
        return False

    @staticmethod
    def _ingest_content_key(report: dict) -> str:
        """Digest of what an ingest report SAYS, ignoring its request binding."""
        return statestore.sha256_json({
            "doc_id": report.get("doc_id"),
            "slices": [{k: e.get(k) for k in ("slice_id", "path", "sha256")}
                       for e in report.get("slices") or []
                       if isinstance(e, dict)],
        })

    def _carry_extractor_forward(self, state: dict,
                                 budget: budget_mod.BudgetTracker,
                                 arts: Arts) -> bool:
        """Seed this round's facts from a verified earlier round.

        The extractor reads the frozen ingest report and its slices and
        nothing else, so identical inputs mean identical facts. Re-running it
        every round cost ~150s of the round for no new information -- unless a
        carried finding asks for a change to facts.json, in which case the
        repair packet has to be honoured and the role must run.
        """
        rel = arts.rel("generator", "facts.json")
        if self._artifact_intact(rel):
            return True
        if any("facts.json" in (f.get("affected_artifacts") or [])
               for f in self._carried()):
            return False
        ingest = self._lazy(arts, arts.rel("generator", "ingest-report.json"))
        if not isinstance(ingest, dict):
            return False
        want = self._ingest_content_key(ingest)
        for prev in range(self._latest_round_dir(), 0, -1):
            if prev >= arts.round:
                continue
            prev_arts = Arts(round=prev)
            path = self.case_dir / prev_arts.rel("generator", "facts.json")
            prev_ingest = statestore.read_json_or_none(
                self.case_dir / prev_arts.rel("generator", "ingest-report.json"))
            if not path.is_file() or not isinstance(prev_ingest, dict):
                continue
            if self._ingest_content_key(prev_ingest) != want:
                continue
            facts = statestore.read_json_or_none(path)
            if not isinstance(facts, dict):
                continue
            payload = {"case_id": state["case_id"], "round": arts.round,
                       "carried_findings": self._carried(),
                       "mandate": self.mandate(),
                       "bundle": self._bundle_provenance(),
                       "prior_artifacts": self._stage_inputs(arts, "extractor")}
            req_id = self._bind_request(state, "extractor", payload, None)
            facts = dict(facts)
            facts["request_id"] = req_id
            facts["candidate_id"] = None    # stamped at the candidate stage
            sha = statestore.atomic_write_json(self.case_dir / rel, facts)
            self._verified[rel] = sha
            arts.files[rel] = sha
            arts.content[rel] = facts
            budget.add_tokens("extractor", 0, 0)
            budget.note_reuse("extractor")
            self._sync_budget(state, budget)
            self._persist(state)
            self._trace(event="EXTRACTOR_REUSED",
                        detail={"from_round": prev, "artifact": rel,
                                "request_id": req_id,
                                "ingest_content_sha256": want},
                        round=arts.round)
            return True
        return False

    def _candidate_context(self, state: dict, arts: Arts) -> gates.CandidateContext:
        return gates.CandidateContext(
            facts=self._lazy(arts, arts.rel("generator", "facts.json")),
            forecast=self._lazy(arts, arts.rel("generator",
                                               "forecast_assumptions.json")),
            report_md=self._lazy(arts, arts.rel("generator", "report.md")),
            candidate_manifest=self._lazy(arts, arts.rel("generator",
                                                          "candidate-manifest.json")),
            input_manifest=self.input_manifest(),
            mandate=self.mandate(),
            model_json=self._lazy(arts, arts.rel("generator", "model.json")),
            valuation_json=self._lazy(arts, arts.rel("generator",
                                                      "valuation.json")),
            case_dir=self.case_dir,
            mapping_version=state["config_versions"]["mapping_tables"],
            evidence_index=self._evidence_index(arts),
        )

    # ================================================================ repair
    def _issue_repair_packet(self, state: dict, arts: Arts,
                             led: ledger_mod.FindingsLedger,
                             note: str = "") -> None:
        """Repair packet per contracts section 6; the next build is a full
        re-build (rounds budget consumed at build start)."""
        round_no = arts.round if arts.round else state["pending"]["round"]
        rel = f"rounds/round-{round_no:03d}/repair-packet.json"
        mandate = self.mandate()
        # Only a blocking finding is a repair obligation. Carrying the minors
        # too fed every advisory nit back into the next round's prompts, so
        # the critics re-raised them verbatim and the ledger could never
        # drain -- the packet was manufacturing its own findings.
        # A finding about the run's own token usage is not a repair obligation
        # either: the tokens are spent, so carrying it into the packet asks for
        # work that cannot close it and the round is wasted. Close it here so a
        # ledger written before the filter existed drains too.
        for row in led.open_findings():
            if _is_self_referential_finding(row):
                led.close_finding(row["finding_id"], critic="orchestrator",
                                  round=round_no,
                                  verified_evidence={"artifact": rel,
                                                     "sha256": "budget-accounting"})
                self._trace(event="FINDING_DROPPED", round=round_no,
                            role=row.get("source_critic"),
                            candidate_id=state["candidate"]["candidate_id"],
                            detail={"finding_id": row["finding_id"],
                                    "reason": "budget accounting, not the work"})
        carried = led.blocking_findings()
        advisory = [f for f in led.open_findings()
                    if f.get("severity") not in ledger_mod.BLOCKING_SEVERITIES]
        packet = {
            "round": round_no,
            "candidate_id": state["candidate"]["candidate_id"],
            "open_findings": carried,
            "advisory_findings": advisory,
            "required_evidence": [f.get("required_evidence", "") for f in carried],
            "unchanged_criteria": {
                "acceptance": ("all numerical gates pass; 2 consecutive full "
                               "reviews of the same candidate with no blocking "
                               "finding open; human approval. Minor findings are "
                               "advisory and do not block."),
                "forecast_bounds": mandate["assumptions"]["forecast_bounds"],
                "cutoff_date": mandate["cutoff_date"],
                "rubric_version": "1.0",
                "note": note,
            },
            "delta_scope": ("full re-build of all four specialist stages; "
                            "stages may short-circuit internal work via "
                            "hash-match proofs; carried findings must be "
                            "addressed"),
        }
        sha = statestore.atomic_write_json(self.case_dir / rel, packet)
        self._verified[rel] = sha
        self._trace(event="REPAIR_PACKET_ISSUED", round=round_no,
                    candidate_id=state["candidate"]["candidate_id"],
                    artifact=rel, artifact_sha256=sha,
                    detail={"open_findings": len(carried),
                            "advisory_findings": len(advisory)})
        state["pending"]["role"] = "generator"
        state["pending"]["stage"] = None
        state["pending"]["retry_count"] = 0
        state["pending"]["round"] = round_no + 1   # fresh dir for the rebuild

    # ================================================================ critics
    def _run_critics(self, state: dict, budget: budget_mod.BudgetTracker,
                     arts: Arts) -> None:
        round_no = state["pending"]["round"]
        arts.round = round_no
        built_round = state["candidate"]["built_round"]
        build_arts = Arts(round=built_round)
        candidate_id = state["candidate"]["candidate_id"]
        candidate_artifacts = {r["artifact"]: r["sha256"]
                               for r in self._candidate_artifact_list(build_arts)}
        pending: list[tuple[str, str, dict, Any]] = []
        for role in CRITIC_ROLES:
            rel = arts.rel("critic", f"{role}.json")
            if self._artifact_intact(rel):
                arts.content[rel] = self._load_verified(rel)
                continue     # fresh-context dispatch already verified (resume)
            payload = {
                "case_id": state["case_id"], "round": round_no,
                "candidate_id": candidate_id,
                "built_round": built_round,
                "candidate_artifacts": candidate_artifacts,
                "artifacts": self._critic_artifacts(build_arts, role),
                "carried_findings": self._carried(),
                "input_manifest": self.input_manifest(),
                "mandate": self.mandate(),
                "mandate_summary": {
                    "cutoff_date": self.mandate()["cutoff_date"],
                    "report_policy": self.mandate()["report_policy"]},
                "dispatch_records": {
                    "rounds_used": budget.used_rounds(),
                    "rounds_max": budget.limits.rounds_max,
                    # the ACCUMULATED clock, not the live one: the live value
                    # changes between the recording run and the replay run and
                    # would make the assembled prompt hash differ
                    "elapsed_minutes": round(budget.used["elapsed_minutes"], 3),
                    "case_minutes": budget.limits.case_minutes,
                    "tokens_by_role": dict(budget.used["tokens_by_role"]),
                    "role_tokens_max": budget.limits.role_tokens_max,
                    "retries_by_role": dict(budget.used.get("retries_by_role") or {}),
                    "retries_max": budget.limits.retries_max,
                    # a carried-forward role spends no tokens by design; the
                    # count is here so a zero is not read as a broken ledger
                    "reused_roles": dict(budget.used.get("reused_roles") or {}),
                    # the engines are deterministic Python, not a dispatch, so
                    # their zero spend is not a reuse and not a gap
                    "deterministic_roles": ["engines"],
                    # explicit human budget grants: the effective caps above
                    # may exceed the frozen mandate's, and the critic must be
                    # able to see that the difference was authorised
                    "grants": list(budget.used.get("grants") or []),
                },
                "rubric_version": "1.0",
            }
            call = self._prepare_dispatch(state, budget, role, payload,
                                          candidate_id, [rel])
            pending.append((role, rel, payload, call))
        # The four critics are independent fresh-context reviews of the same
        # candidate, so they run together; only the bookkeeping is ordered.
        for (role, rel, payload, call), result in zip(
                pending, self._dispatch_concurrently([c for *_, c in pending])):
            result = self._finish_dispatch(state, budget, role, call, result)
            while True:
                err = self._validate_submission(role, call.request_id, result)
                if err is None:
                    state["pending"]["retry_count"] = 0
                    break
                self._handle_invalid(state, budget, role, err)
                payload = dict(payload)
                payload["submission_feedback"] = err
                result = self._dispatch_role(state, budget, role, payload,
                                             candidate_id, [rel])
            sha = statestore.atomic_write_json(self.case_dir / rel, result.parsed)
            arts.files[rel] = sha
            arts.content[rel] = result.parsed
            self._verified[rel] = sha
            self._sync_budget(state, budget)
            self._transition(state, "RUNNING", "RUNNING", verified=rel,
                             detail={"critic": role,
                                     "verdict": result.parsed.get("verdict")})
        state["pending"]["stage"] = "merge"

    def _run_merge_and_decide(self, state: dict, budget: budget_mod.BudgetTracker,
                              arts: Arts) -> None:
        round_no = state["pending"]["round"]
        arts.round = round_no
        candidate_id = state["candidate"]["candidate_id"]
        reports = [self._lazy(arts, arts.rel("critic", f"{role}.json"))
                   for role in CRITIC_ROLES]
        rel = arts.rel("critic", "merged-findings.json")
        reports = [r for r in reports if r is not None]
        # built ONCE, outside the loop: rebuilding it each iteration threw
        # away the submission_feedback the repair depends on
        payload = {
            "case_id": state["case_id"], "round": round_no,
            "candidate_id": candidate_id,
            "critic_reports": reports,
            "carried_findings": self._carried(),
            "built_round": state["candidate"]["built_round"],
        }
        while True:
            result = self._dispatch_role(state, budget, "critic_lead", payload,
                                         candidate_id, [rel])
            err = self._validate_submission("critic_lead",
                                           state["pending"]["request_id"], result)
            if err is None:
                state["pending"]["retry_count"] = 0
                break
            self._handle_invalid(state, budget, "critic_lead", err)
            payload = dict(payload)
            payload["submission_feedback"] = err
        merged = result.parsed
        sha = statestore.atomic_write_json(self.case_dir / rel, merged)
        arts.files[rel] = sha
        arts.content[rel] = merged
        self._verified[rel] = sha
        self._sync_budget(state, budget)
        self._transition(state, "RUNNING", "RUNNING", verified=rel,
                         detail={"merged": True,
                                 "reviewer_count": merged.get("reviewer_count")})

        led = self.ledger()
        self._apply_critic_closures(state, arts, reports, led)
        for raw in merged.get("findings", []):
            f = dict(raw)
            if _is_self_referential_finding(f):
                self._trace(event="FINDING_DROPPED", round=round_no,
                            role=f.get("source_critic"),
                            candidate_id=candidate_id,
                            detail={"reason": "budget accounting, not the work",
                                    "summary": f.get("summary")})
                continue
            fid = led.open_finding(f, round=round_no)
            self._trace(event="FINDING_OPENED",
                        candidate_id=candidate_id, round=round_no,
                        role=f.get("source_critic"),
                        detail={"finding_id": fid, "summary": f.get("summary")})
        # No close-on-silence: a critic that fails to re-raise a finding has
        # NOT verified it fixed. It stays open until a critic returns a
        # verified_resolution for its ledger id.
        # orchestrator-verified cleanliness: never trust the lead alone
        # reviewer_count is model-supplied: a string or null must not raise
        raw_count = merged.get("reviewer_count")
        reviewer_count = raw_count if isinstance(raw_count, int) and \
            not isinstance(raw_count, bool) else None
        # A round is acceptance-eligible when every critic actually reviewed
        # the candidate and nothing blocking is open. Requiring verdict ==
        # "clean" from all four critics made acceptance unreachable: four
        # independent LLM reviewers always find something, so the streak could
        # never reach 2 and the case could only exhaust. A minor finding is
        # advisory -- it rides along in the approval package for the human,
        # who is the real gate.
        reviewed_all = (bool(reports) and
                        all(rep.get("verdict") in ("clean", "findings")
                            for rep in reports) and
                        all((rep.get("coverage") or {}).get("areas_checked")
                            for rep in reports) and
                        bool(merged.get("coverage_ok")) and
                        reviewer_count == len(reports))
        no_open = not led.blocking_findings()
        if reviewed_all and no_open:
            cand = state["candidate"]
            if cand["last_review_candidate"] == candidate_id:
                cand["clean_review_rounds"] += 1
            else:
                cand["clean_review_rounds"] = 1
                cand["last_review_candidate"] = candidate_id
            if cand["clean_review_rounds"] >= 2 and cand["gates_passed"]:
                ok, reason = budget.check_acceptance()
                if not ok:
                    self.exhausted(state, budget, reason)
                    return
                self._enter_pending_approval(state, budget, arts)
                return
            # re-review the SAME candidate: no rebuild, NO round consumed
            state["pending"]["round"] = round_no + 1
            state["pending"]["stage"] = "critics"
            self._sync_budget(state, budget)
            self._transition(state, "RUNNING", "RUNNING", verified=rel,
                             detail={"re_review_same_candidate": candidate_id,
                                     "streak": cand["clean_review_rounds"]})
            return
        # findings remain: repair packet; loop starts the next full build
        self._issue_repair_packet(state, arts, led,
                                  note="critic findings after full review")

    def _apply_critic_closures(self, state: dict, arts: Arts,
                               reports: list[dict],
                               led: ledger_mod.FindingsLedger) -> None:
        """Close findings ONLY on critic-verified evidence (artifact + sha256)
        that matches the CURRENT candidate artifacts (tamper-proof)."""
        round_no = arts.round
        built_arts = Arts(round=state["candidate"]["built_round"])
        # ONLY the candidate under review may serve as closure evidence.
        # Merging self._verified here would also admit artifacts from other
        # rounds (critic reports, gates, superseded candidates), letting a
        # finding close against evidence that no longer describes the build.
        expected_sha = {r["artifact"]: r["sha256"]
                        for r in self._candidate_artifact_list(built_arts)}
        for rep in reports:
            role = rep.get("role") or "critic"
            for res in rep.get("verified_resolutions") or []:
                ev = (res or {}).get("verified_evidence") or {}
                fid = res.get("finding_id")
                if not fid:
                    continue
                if expected_sha.get(ev.get("artifact")) != ev.get("sha256") or \
                        not ev.get("sha256"):
                    # a rejected claim is NOT a closure: never emit
                    # FINDING_VERIFIED_CLOSED for it
                    self._trace(event="FINDING_CLOSURE_REJECTED",
                                round=round_no, role=role,
                                candidate_id=state["candidate"]["candidate_id"],
                                detail={"finding_id": fid, "rejected": True,
                                        "reason": "artifact+sha256 mismatch"})
                    continue
                try:
                    changed = led.close_finding(fid, critic=role,
                                                round=round_no,
                                                verified_evidence=ev)
                except (LookupError, ValueError):
                    continue    # unknown ledger id or already closed
                if not changed:
                    continue
                self._trace(event="FINDING_VERIFIED_CLOSED",
                            round=round_no, role=role,
                            candidate_id=state["candidate"]["candidate_id"],
                            detail={"finding_id": fid, "rejected": False})

    # ================================================================ approval
    def _enter_pending_approval(self, state: dict,
                                budget: budget_mod.BudgetTracker,
                                arts: Arts) -> None:
        deadline_epoch = budget.approval_deadline_epoch(self.clock())
        budget.pause_clock()
        state["pending"]["role"] = "approval"
        state["pending"]["stage"] = None
        state["pending"]["request_id"] = None
        state["pending"]["case_clock_started"] = None
        state["pending"]["deadline"] = _iso_from_epoch(deadline_epoch)
        state["budget"]["used"] = budget.used_state()
        state["status"] = "PENDING_APPROVAL"
        self._transition(state, "RUNNING", "PENDING_APPROVAL",
                         verified=arts.rel("critic", "merged-findings.json"),
                         detail={"approval_deadline": state["pending"]["deadline"]})
        self._trace(event="APPROVAL_REQUESTED",
                    candidate_id=state["candidate"]["candidate_id"],
                    round=arts.round,
                    detail={"deadline": state["pending"]["deadline"],
                            "clean_review_rounds":
                                state["candidate"]["clean_review_rounds"]})

    def _approval_expired(self, state: dict) -> bool:
        deadline = (state["pending"] or {}).get("deadline")
        if not deadline:
            return False
        return self.clock() > _epoch_from_iso(deadline)

    def record_approval(self, decision: str, by: str, reasoning: str = "",
                        acknowledge_findings: list[str] | None = None) -> dict:
        """Record the human decision at the approval gate.

        ``acknowledge_findings`` names the open blocking findings the approver
        has read and is signing off over. An escalated case carries objections
        the machine could not settle -- a critic's judgment call, or a claim
        the approver judges wrong -- and only a human can adjudicate those.
        Every open blocking finding must be named: a blanket approval that
        silently swallows an objection is exactly what the ledger exists to
        prevent.
        """
        if decision not in ("APPROVED", "REJECTED"):
            raise ValueError("decision must be APPROVED or REJECTED")
        state = self.load()
        if state["status"] == "EXHAUSTED":
            raise RuntimeError("exhaustion never grants acceptance: case is EXHAUSTED")
        if state["status"] in ("APPROVED", "REJECTED", "CLOSED", "STOPPED"):
            raise RuntimeError(f"cannot approve a case in status {state['status']}")
        if state["status"] == "PENDING_APPROVAL":
            if self._approval_expired(state):
                self._approval_block(state)
                raise RuntimeError("approval deadline expired: case blocked "
                                    "(APPROVAL_EXPIRED)")
            if decision == "REJECTED":
                return self._finish_rejection(state, by, reasoning)
            return self._finish_approval(state, by, reasoning,
                                         acknowledge_findings)
        code = (state.get("reason") or {}).get("code")
        if state["status"] == "BLOCKED" and code == "APPROVAL_EXPIRED" \
                and decision == "APPROVED":
            return self._restart_acceptance_from_zero(state, by, reasoning)
        raise RuntimeError(f"case is not awaiting approval (status={state['status']})")

    def _approval_record(self, state: dict, decision: str, by: str, reasoning: str,
                         extra: dict | None = None) -> dict:
        approvals = self.case_dir / "approvals"
        n = len(list(approvals.glob("apr-*.json"))) + 1
        record = {
            "decision_id": f"apr-{n:04d}",
            "case_id": state["case_id"],
            "candidate_id": state["candidate"]["candidate_id"],
            "decision": decision,
            "by": by,
            "ts": self.now(),
            "reasoning": reasoning,
            "expected_evidence_refs": [
                r["artifact"] for r in
                self._candidate_artifact_list(Arts(round=state["candidate"]["built_round"]))],
        }
        if extra:
            record.update(extra)
        statestore.atomic_write_json(approvals / f"apr-{n:04d}.json", record)
        self._trace(event="APPROVAL_RECORDED",
                    candidate_id=state["candidate"]["candidate_id"],
                    round=state["pending"]["round"],
                    detail={"decision": decision, "by": by,
                            "decision_id": record["decision_id"]})
        return record

    def _finish_approval(self, state: dict, by: str, reasoning: str,
                         acknowledge_findings: list[str] | None = None) -> dict:
        led = self.ledger()
        cand = state["candidate"]
        blocking = led.blocking_findings()
        acknowledged: list[str] = []
        if blocking:
            named = {str(f) for f in (acknowledge_findings or [])}
            outstanding = [f["finding_id"] for f in blocking
                           if f["finding_id"] not in named]
            if outstanding:
                raise RuntimeError(
                    "blocking findings remain and were not acknowledged: "
                    + ", ".join(outstanding))
            acknowledged = [f["finding_id"] for f in blocking]
        # An escalated case was reviewed as often as the budget allowed and
        # never came back clean, so the streak is 0 by construction. Requiring
        # it would make escalation a dead end; the acknowledgement above is
        # what replaces it.
        if cand["clean_review_rounds"] < 2 and not cand.get("requires_acknowledgement"):
            raise RuntimeError("acceptance requires 2 consecutive clean reviews")
        if not cand["gates_passed"]:
            raise RuntimeError("acceptance requires all numerical gates to pass")
        budget = self._budget(state)
        ok, reason = budget.check_acceptance()
        if not ok:
            self.exhausted(state, budget, reason)
            raise RuntimeError(f"exhaustion never grants acceptance: {reason.message}")
        # the report about to be published must still be the artifact the
        # gates and critics verified: a tampered or missing report.md must
        # never reach report/vN-*.md
        report_rel = self._report_rel_of(state)
        if not self._artifact_intact(report_rel):
            raise RuntimeError(
                f"cannot publish {report_rel}: it is missing or its sha256 no "
                "longer matches the verified artifact"
            )
        self._approval_record(state, "APPROVED", by, reasoning,
                              {"acknowledged_findings": acknowledged}
                              if acknowledged else None)
        # versioned report publication
        cand8 = (cand["candidate_id"] or "unknown")[:8]
        reports_dir = self.case_dir / "report"
        reports_dir.mkdir(parents=True, exist_ok=True)
        version = len(list(reports_dir.glob("v*.md")))
        out_rel = f"report/v{version + 1}-{cand8}.md"
        shutil.copyfile(self.case_dir / report_rel, self.case_dir / out_rel)
        state["status"] = "APPROVED"
        state["reason"] = {"code": "APPROVED", "message": "human approved",
                           "detail": {"by": by}}
        self._transition(state, "PENDING_APPROVAL", "APPROVED",
                         verified=out_rel, detail={"report_version": out_rel})
        message = (f"human approved; {cand['clean_review_rounds']} consecutive "
                   f"clean reviews; all gates pass")
        if acknowledged:
            message += (f"; {len(acknowledged)} blocking finding(s) "
                        f"acknowledged by the approver")
        result = self._write_result(
            state, "APPROVED", message,
            code="APPROVED",
            detail={"by": by, "acknowledged_findings": acknowledged},
            report_version=f"v{version + 1}-{cand8}")
        state["status"] = "CLOSED"
        self._transition(state, "APPROVED", "CLOSED", verified=out_rel)
        self._trace(event="CASE_CLOSED", artifact="result.json",
                    artifact_sha256=statestore.sha256_file(
                        self.case_dir / "result.json"),
                    candidate_id=cand["candidate_id"],
                    detail={"terminal": "APPROVED"})
        return {"status": "CLOSED", "terminal": "APPROVED",
                "report_version": f"v{version + 1}-{cand8}", "result": result}

    def _finish_rejection(self, state: dict, by: str, reasoning: str) -> dict:
        self._approval_record(state, "REJECTED", by, reasoning)
        state["status"] = "REJECTED"
        message = f"human rejected: {reasoning or 'no reasoning given'}"
        state["reason"] = {"code": "REJECTED", "message": message,
                           "detail": {"by": by}}
        self._transition(state, "PENDING_APPROVAL", "REJECTED",
                         verified="approvals record")
        result = self._write_result(state, "REJECTED", message,
                                    code="REJECTED", detail={"by": by})
        return {"status": "REJECTED", "terminal": "REJECTED", "result": result}

    def _restart_acceptance_from_zero(self, state: dict, by: str,
                                      reasoning: str) -> dict:
        """Re-approval after BLOCKED(APPROVAL_EXPIRED): acceptance restarts
        at zero; the case continues under the standing budgets."""
        budget = self._budget(state)
        ok, reason = budget.check_new_round()
        if not ok:
            self.exhausted(state, budget, reason)
            raise RuntimeError(f"cannot restart: {reason.message}")
        self._approval_record(state, "APPROVED", by, reasoning,
                              {"restarted_acceptance": True})
        budget.resume_clock()
        state["pending"] = {
            "role": "generator", "request_id": None,
            # round number is bumped to a fresh dir at the next build start
            "round": state["pending"]["round"],
            "retry_count": 0,
            "case_clock_started": _iso_from_epoch(self.clock()),
            "deadline": None, "stage": None,
        }
        state["candidate"]["clean_review_rounds"] = 0
        state["candidate"]["last_review_candidate"] = None
        state["status"] = "RUNNING"
        self._sync_budget(state, budget)
        self._transition(state, "BLOCKED", "RUNNING",
                         verified="acceptance restarted from zero",
                         detail={"acceptance_restarted": True})
        return {"status": "RUNNING",
                "message": "acceptance restarted from zero; run() continues",
                "clean_review_rounds": 0}

    def _approval_block(self, state: dict) -> None:
        state["status"] = "BLOCKED"
        state["reason"] = {
            "code": "APPROVAL_EXPIRED",
            "message": (f"approval deadline {state['pending'].get('deadline')} "
                        f"expired before a human decision was recorded"),
            "detail": {"deadline": state["pending"].get("deadline")},
        }
        self._transition(state, "PENDING_APPROVAL", "BLOCKED",
                         verified="approval deadline expired")
        self._write_result(state, "BLOCKED", state["reason"]["message"],
                           code="APPROVAL_EXPIRED",
                           detail={"deadline": state["pending"].get("deadline")})

    # ================================================================ terminal
    def blocked(self, state: dict, code: str, message: str, detail: dict) -> None:
        state["status"] = "BLOCKED"
        state["reason"] = {"code": code, "message": message, "detail": detail or {}}
        self._transition(state, "RUNNING", "BLOCKED", verified=code,
                         detail={"blocked": code})
        self._write_result(state, "BLOCKED", message, code=code, detail=detail)

    def _clear_block(self, state: dict) -> dict:
        """Lift a BLOCKED status so the same case can be re-run.

        The block reason stays in the trace and in the checkpoint history as
        evidence; the repair budget for the role that blocked is reset so the
        fix the human just made is actually attempted. A case that never
        started goes back to CREATED (its inputs are re-verified there); a
        case that was mid-build goes back to RUNNING and the resume planner
        decides which stage to re-dispatch.
        """
        reason = state.get("reason") or {}
        code = reason.get("code")
        role = (reason.get("detail") or {}).get("role")
        budget = self._budget(state)
        if role:
            budget.reset_retries(role)
            budget.reset_tokens(role)
            self._sync_budget(state, budget)
        state["pending"]["retry_count"] = 0
        state["reason"] = None
        target = ("RUNNING" if state["pending"].get("case_clock_started")
                  else "CREATED")
        state["status"] = target
        self._transition(state, "BLOCKED", target,
                         detail={"unblocked": code, "retries_reset_for": role})
        # result.json described a terminal state that no longer holds; the
        # block itself stays in trace.jsonl and the checkpoint history
        stale = self.case_dir / statestore.RESULT_FILE
        if stale.exists():
            stale.unlink()
        return state

    def _grant_budget(self, state: dict) -> dict:
        """Reopen an EXHAUSTED case with an explicit, recorded budget grant.

        The exhausted role's counters restart and the caps grow by one more
        full allowance, so the grant is spendable. The grant is written into
        the budget state and the trace; acceptance is still refused on a
        breach, so this reopens the run without laundering exhaustion into an
        approval.
        """
        reason = state.get("reason") or {}
        detail = reason.get("detail") or {}
        role = detail.get("role")
        budget = self._budget(state)
        record = budget.grant(role=role, by="human")
        self._sync_budget(state, budget)
        state["pending"]["retry_count"] = 0
        state["reason"] = None
        target = ("RUNNING" if state["pending"].get("case_clock_started")
                  else "CREATED")
        state["status"] = target
        self._transition(state, "EXHAUSTED", target,
                         detail={"budget_grant": record})
        stale = self.case_dir / statestore.RESULT_FILE
        if stale.exists():
            stale.unlink()
        return state

    def _can_escalate(self, state: dict) -> bool:
        """Is the candidate complete enough for a human to adjudicate?

        Escalation is not acceptance: the human still decides. It is only
        offered when the machine has nothing left to verify -- every gate
        passed on the current candidate and every critic reviewed it. A gate
        failure or a missing review means the work is unverified, and that is
        what EXHAUSTED is for.
        """
        cand = state.get("candidate") or {}
        if not cand.get("gates_passed"):
            return False
        if not cand.get("candidate_id"):
            return False
        return bool(self.ledger().blocking_findings())

    def _escalate_to_review(self, state: dict,
                            budget: budget_mod.BudgetTracker,
                            reason: budget_mod.BudgetReason) -> None:
        """Hand an unresolved case to the human instead of terminating it."""
        led = self.ledger()
        blocking = led.blocking_findings()
        arts = Arts(round=state["candidate"]["built_round"])
        self._enter_pending_approval(state, budget, arts)
        state["candidate"]["requires_acknowledgement"] = True
        state["reason"] = {
            "code": "ESCALATED",
            "message": (f"round budget exhausted ({reason.message}); the "
                        f"candidate passed every gate and was fully reviewed, "
                        f"so it is escalated for human adjudication with "
                        f"{len(blocking)} unresolved blocking finding(s)"),
            "detail": {"budget": reason.as_dict(),
                       "blocking_finding_ids": [f["finding_id"] for f in blocking]},
        }
        self._sync_budget(state, budget)
        self._persist(state)
        self._trace(event="ESCALATED", round=state["pending"]["round"],
                    candidate_id=state["candidate"]["candidate_id"],
                    detail={"blocking_findings": len(blocking),
                            "budget_code": reason.code})

    def exhausted(self, state: dict, budget: budget_mod.BudgetTracker,
                  reason: budget_mod.BudgetReason) -> None:
        """EXHAUSTED with a precise machine-readable reason; never acceptance."""
        state["status"] = "EXHAUSTED"
        self._sync_budget(state, budget)
        state["reason"] = reason.as_dict()
        self._transition(state, "RUNNING", "EXHAUSTED",
                         verified=f"budget breach: {reason.code}",
                         detail={"exhausted": reason.code})
        self._write_result(state, "EXHAUSTED", reason.message,
                           code=reason.code, detail=reason.detail)

    def stop(self, by: str, reason: str = "human stop") -> dict:
        """Human STOP: terminal STOPPED with a precise reason."""
        state = self.load()
        frm = state["status"]
        if state["status"] in ("CLOSED", *TERMINAL):
            raise RuntimeError(f"case already terminal: {state['status']}")
        budget = self._budget(state)
        budget.pause_clock()
        state["status"] = "STOPPED"
        state["reason"] = {"code": "STOPPED", "message": f"{reason} (by {by})",
                           "detail": {"by": by}}
        self._sync_budget(state, budget)
        self._transition(state, frm, "STOPPED", verified="human stop",
                         detail={"stopped_by": by})
        self._write_result(state, "STOPPED", state["reason"]["message"],
                           code="STOPPED", detail={"by": by})
        return {"status": "STOPPED", "reason": state["reason"]}

    def _write_result(self, state: dict, terminal: str, message: str, *,
                      code: str | None = None, detail: dict | None = None,
                      report_version: str | None = None) -> dict:
        """result.json for every terminal state (machine-readable reason)."""
        result = {
            "case_id": state["case_id"],
            "terminal": terminal,
            "candidate_id": state["candidate"]["candidate_id"],
            "report_version": report_version,
            "reason": {"code": code or terminal, "message": message,
                       "detail": detail or {}},
            "budget_final": dict(state["budget"]),
            "closed_ts": self.now(),
        }
        statestore.atomic_write_json(self.case_dir / "result.json", result)
        return result

    def _report_rel_of(self, state: dict) -> str:
        built = state["candidate"]["built_round"]
        return f"rounds/round-{built:03d}/generator/report.md"

    # ================================================================ public
    def gates_report(self) -> dict:
        """Re-run the numerical gates on the saved candidate (CLI `gates`)."""
        state = self.load()
        built = state["candidate"]["built_round"]
        if not built:
            # no verified candidate yet: nothing to re-run (fail closed)
            return {"case_id": state["case_id"],
                    "candidate_id": state["candidate"]["candidate_id"],
                    "passed": False,
                    "error": "no built candidate; run the case first",
                    "results": []}
        arts = Arts(round=built)
        ctx = self._candidate_context(state, arts)
        report = self.gate_engine.run_all(ctx)
        return {
            "case_id": state["case_id"],
            "candidate_id": state["candidate"]["candidate_id"],
            "passed": report.passed,
            "results": [{"gate": o.gate, "ok": o.ok, "findings": o.findings}
                        for o in report.results],
        }
