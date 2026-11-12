"""Budget accounting for a coverage case (owner: stream A2).

Locked budget rules (docs/design-decisions.md §3):
  - 4 revision rounds, 3 retries per specialist dispatch, 45-minute case
    clock, per-role token cap (mandate-configurable).
  - Budgets are checked at EVERY dispatch; a breach transitions the case to
    EXHAUSTED with a precise machine-readable reason.
  - Exhaustion NEVER grants acceptance.
  - The case clock pauses while the case is PENDING_APPROVAL (human review
    can take up to 48h; the clock measures active run time only).

Determinism: the clock is an injected callable returning epoch seconds;
tests inject fakes instead of sleeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_LIMITS = {
    "rounds_max": 4,
    "retries_max": 3,
    # Measured on the live Intel case (Codex, 2026-11-12): one full round is
    # ~25 minutes (ingest ~1, extraction ~13, model ~1, writer, four critics)
    # and approval needs at least two reviewed rounds. 45 minutes could not
    # hold two rounds, so every live case would have needed a budget grant.
    "case_minutes": 120,
    # A revision round re-dispatches every role, so a 4-round case dispatches
    # each role up to 4 times (plus repairs). Measured cost per dispatch is
    # ~20k tokens for the heavy roles, so a 60k cap guaranteed EXHAUSTED on
    # any case that needed a second round — the cap has to afford the rounds
    # budget it sits next to, or it is not a runaway guard, it is a trap.
    # Live Intel case (Codex, 2026-11-12): one writer call reads ~67k tokens,
    # and a round can dispatch the writer up to 1 + retries_max times. Four
    # rounds of that is ~1.07M; 250k was spent by four writer attempts in
    # round 1. The cost cap (USD) is the spending guard; this is a runaway
    # guard and must afford the rounds and retries it sits next to.
    "role_tokens_max": 1200000,
    "approval_hours": 48,
    # stop after this many consecutive reviewed rounds in which the set of
    # open blocking findings did not shrink
    "stall_rounds": 2,
}

# Per-role wall-clock ceilings for one backend invocation, in seconds.
# A single flat timeout starves the heavy roles: the extractor reads every
# statement slice and emits the full facts set, so it needs materially more
# time than ingest. Mandates override any entry via limits.role_timeouts.
DEFAULT_ROLE_TIMEOUTS = {
    "default": 240.0,
    # ingest is the only tool-using role: it opens every filing in the bundle
    # and cuts the statement slices, so it needs materially more wall clock
    # than a single-shot role. At 240s it hit the CLI's own deadline mid-cut.
    "ingest": 420.0,
    # One filing's statements took 379 s on Codex; the extraction scope is
    # the trigger plus the annual anchor, so two filings need ~760 s.
    "extractor": 1200.0,
    "model": 300.0,
    "writer": 600.0,
    # The four critics are dispatched together, so each one contends with the
    # other three for the backend. A single critic took 281s under that load
    # and hit a 300s ceiling; the ceiling has to cover the contention, not the
    # solo latency.
    "critic": 480.0,
}


# Per-role reasoning effort. Roles not listed use the backend's own default
# (Luna "high" for codex, "medium" for claude). The extractor is a transcription
# job, not a judgement one, and the measured cost of "high" was output time.
# Mandates override any entry via limits.role_efforts; a null/"default" value
# restores the backend default for that role.
DEFAULT_ROLE_EFFORT = {"extractor": "medium"}


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


@dataclass
class Limits:
    """Immutable per-case budget limits (from mandate.limits, with defaults)."""
    rounds_max: int = 4
    retries_max: int = 3
    case_minutes: float = 120.0
    role_tokens_max: int = 60000
    approval_hours: float = 48.0
    stall_rounds: int = 2
    # USD caps; None = unlimited. Per-call cap travels in RoleCall.budget
    # ["cost_usd_max"]; per-case cap is checked against used["cost_usd"].
    call_cost_usd_max: float | None = None
    case_cost_usd_max: float | None = None
    role_timeouts: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_ROLE_TIMEOUTS))
    role_efforts: dict[str, str | None] = field(
        default_factory=lambda: dict(DEFAULT_ROLE_EFFORT))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Limits":
        d = d or {}
        timeouts = dict(DEFAULT_ROLE_TIMEOUTS)
        for role, seconds in (d.get("role_timeouts") or {}).items():
            timeouts[str(role)] = float(seconds)
        efforts: dict[str, str | None] = dict(DEFAULT_ROLE_EFFORT)
        for role, effort in (d.get("role_efforts") or {}).items():
            efforts[str(role)] = effort
        return cls(
            rounds_max=int(d.get("rounds_max", DEFAULT_LIMITS["rounds_max"])),
            retries_max=int(d.get("retries_max", DEFAULT_LIMITS["retries_max"])),
            case_minutes=float(d.get("case_minutes", DEFAULT_LIMITS["case_minutes"])),
            role_tokens_max=int(d.get("role_tokens_max", DEFAULT_LIMITS["role_tokens_max"])),
            approval_hours=float(d.get("approval_hours", DEFAULT_LIMITS["approval_hours"])),
            stall_rounds=int(d.get("stall_rounds", DEFAULT_LIMITS["stall_rounds"])),
            call_cost_usd_max=_opt_float(
                d.get("call_cost_usd_max", d.get("cost_usd_max"))),
            case_cost_usd_max=_opt_float(d.get("case_cost_usd_max")),
            role_timeouts=timeouts,
            role_efforts=efforts,
        )

    def effort_for(self, role: str) -> str | None:
        """Reasoning effort to request for *role*; None = backend default.

        Exact role match wins, then the ``critic`` family. ``None``, "" and
        "default" all mean "let the backend decide".
        """
        value = self.role_efforts.get(role)
        if value is None and role.startswith("critic"):
            value = self.role_efforts.get("critic")
        value = str(value).strip() if value is not None else ""
        return None if value.lower() in ("", "default", "none") else value

    def timeout_for(self, role: str) -> float:
        """Wall-clock ceiling for one invocation of *role*.

        Exact role match wins, then the ``critic`` family, then ``default``.
        """
        if role in self.role_timeouts:
            return float(self.role_timeouts[role])
        if role.startswith("critic") and "critic" in self.role_timeouts:
            return float(self.role_timeouts["critic"])
        return float(self.role_timeouts.get("default", 240.0))


@dataclass
class BudgetReason:
    """Machine-readable breach record written into state.reason / result.json."""
    code: str          # rounds | case_clock | role_tokens | retries
    message: str       # precise human-readable reason
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "detail": dict(self.detail)}

    def __getitem__(self, key: str):
        if key == "code":
            return self.code
        if key == "message":
            return self.message
        return self.detail[key]


class BudgetTracker:
    """Per-case budget accounting.

    ``used`` keeps the contract shape:
      {"rounds": int, "elapsed_minutes": float, "tokens_by_role": {role: int}}
    """

    def __init__(self, limits: dict[str, Any] | None, used: dict[str, Any] | None = None,
                 clock_started: float | None = None,
                 clock: Callable[[], float] | None = None):
        self.limits = Limits.from_dict(limits or {})
        self.clock = clock or time.time
        used = used or {}
        self.used: dict[str, Any] = {
            "rounds": int(used.get("rounds", 0)),
            "elapsed_minutes": float(used.get("elapsed_minutes", 0.0)),
            "tokens_by_role": dict(used.get("tokens_by_role", {})),
            # carried across state round-trips: dropping it reset every retry
            # count on each _sync_budget -> _budget cycle, so check_retry
            # always saw zero and the retries budget could never fire.
            "retries_by_role": dict(used.get("retries_by_role", {})),
            # audit trail of explicit human budget grants (see grant())
            "grants": list(used.get("grants") or []),
            # roles whose artifact was carried forward instead of re-dispatched.
            # They spend no tokens, which reads as a broken ledger unless the
            # reuse is recorded: a critic saw "zero spend for ingest and
            # engines" and could not complete the per-role cap check.
            "reused_roles": dict(used.get("reused_roles") or {}),
            "cost_usd": float(used.get("cost_usd") or 0.0),
        }
        self.clock_started: float | None = clock_started

    # ---------------------------------------------------------------- state
    @classmethod
    def from_state(cls, state: dict, clock: Callable[[], float] | None = None) -> "BudgetTracker":
        limits = (state.get("budget") or {}).copy()
        limits.pop("used", None)
        started_iso = (state.get("pending") or {}).get("case_clock_started")
        started = None
        if started_iso:
            from datetime import datetime, timezone
            started = datetime.fromisoformat(started_iso.replace("Z", "+00:00")).timestamp()
        return cls(
            limits=limits,
            used=(state.get("budget") or {}).get("used"),
            clock_started=started,
            clock=clock,
        )

    def used_state(self) -> dict:
        return {
            "rounds": self.used["rounds"],
            "elapsed_minutes": round(self.used["elapsed_minutes"], 3),
            "tokens_by_role": dict(self.used["tokens_by_role"]),
            "retries_by_role": dict(self.used.get("retries_by_role") or {}),
            "grants": list(self.used.get("grants") or []),
            "reused_roles": dict(self.used.get("reused_roles") or {}),
            "cost_usd": round(float(self.used.get("cost_usd") or 0.0), 6),
        }

    def limits_state(self) -> dict:
        return {
            "rounds_max": self.limits.rounds_max,
            "retries_max": self.limits.retries_max,
            "case_minutes": self.limits.case_minutes,
            "role_tokens_max": self.limits.role_tokens_max,
            "approval_hours": self.limits.approval_hours,
            "stall_rounds": self.limits.stall_rounds,
            "role_timeouts": dict(self.limits.role_timeouts),
            "role_efforts": dict(self.limits.role_efforts),
            "call_cost_usd_max": self.limits.call_cost_usd_max,
            "case_cost_usd_max": self.limits.case_cost_usd_max,
        }

    # ------------------------------------------------------------- clocking
    def start_clock(self) -> None:
        self.clock_started = self.clock()

    def resume_clock(self) -> None:
        if self.clock_started is None:
            self.clock_started = self.clock()

    def pause_clock(self) -> None:
        """Accumulate active time and stop the clock (entering PENDING_APPROVAL)."""
        if self.clock_started is None:
            return
        self.used["elapsed_minutes"] += (self.clock() - self.clock_started) / 60.0
        self.clock_started = None

    def elapsed_minutes(self) -> float:
        acc = self.used["elapsed_minutes"]
        if self.clock_started is not None:
            acc += (self.clock() - self.clock_started) / 60.0
        return acc

    def approval_deadline_epoch(self, granted_at_epoch: float) -> float:
        return granted_at_epoch + self.limits.approval_hours * 3600.0

    # ------------------------------------------------------------- accounting
    def add_tokens(self, role: str, tokens_in: int | None, tokens_out: int | None) -> None:
        t = self.used["tokens_by_role"]
        t[role] = t.get(role, 0) + int(tokens_in or 0) + int(tokens_out or 0)

    def add_cost(self, cost_usd: float | None) -> None:
        """Accumulate reported USD spend (None = backend did not report)."""
        if cost_usd is not None:
            self.used["cost_usd"] = float(self.used.get("cost_usd") or 0.0) + float(cost_usd)

    def used_cost_usd(self) -> float:
        return float(self.used.get("cost_usd") or 0.0)

    def _cost_breach(self) -> BudgetReason | None:
        cap = self.limits.case_cost_usd_max
        spent = self.used_cost_usd()
        if cap is not None and spent >= cap:
            return self._breach(
                "case_cost",
                f"case cost cap: ${spent:.4f} of ${cap:.4f} used",
                {"cost_usd": round(spent, 6), "cap_usd": cap},
            )
        return None

    def note_reuse(self, role: str) -> None:
        """Record that *role* was carried forward rather than re-dispatched."""
        re_ = self.used.setdefault("reused_roles", {})
        re_[role] = re_.get(role, 0) + 1

    def used_tokens(self, role: str) -> int:
        return int(self.used["tokens_by_role"].get(role, 0))

    def note_round(self) -> None:
        self.used["rounds"] += 1

    def used_rounds(self) -> int:
        return int(self.used["rounds"])

    def note_retry(self, role: str) -> int:
        re = self.used.setdefault("retries_by_role", {})
        re[role] = re.get(role, 0) + 1
        return re[role]

    def reset_retries(self, role: str) -> None:
        """Clear the repair count for *role* after a human cleared a block.

        A block is an instruction to fix the cause and re-run; the repair the
        human just made must get a full retry budget, otherwise the case is
        blocked again on the first hiccup.
        """
        by_role = self.used.get("retries_by_role") or {}
        for key in [k for k in by_role if k == role or k.startswith(f"{role}:")]:
            by_role.pop(key, None)

    def reset_tokens(self, role: str) -> None:
        """Clear the token count for *role* after a human cleared a block.

        Same reasoning as reset_retries: the tokens the failed attempt burned
        were spent on inputs that were wrong. Charging the repaired attempt for
        them means the case dies of a budget breach before the fix is ever
        tried, which is how a repairable case turns into a dead one.
        """
        self.used["tokens_by_role"].pop(role, None)

    def grant(self, role: str | None = None, tokens: int | None = None,
              minutes: float | None = None, rounds: int | None = None,
              by: str = "human") -> dict:
        """Grant extra budget to an EXHAUSTED case and record the grant.

        Exhaustion is a budget decision, not a verdict on the work: a human may
        explicitly buy more fuel. Every dimension grows by one more full
        allowance — rounds, clock, and tokens — because a case that ran out of
        one is about to run out of the others. The grant is appended to
        ``used.grants`` so
        the audit trail shows the case only continued because someone said so,
        and the per-role counters restart from zero so the grant is actually
        spendable. Acceptance is still never granted on a breach (see
        check_acceptance) — this only reopens the run.
        """
        tokens = int(tokens if tokens is not None
                     else self.limits.role_tokens_max)
        minutes = float(minutes if minutes is not None
                        else self.limits.case_minutes)
        rounds = int(rounds if rounds is not None else self.limits.rounds_max)
        record = {
            "role": role,
            "by": by,
            "tokens_at_grant": dict(self.used["tokens_by_role"]),
            "role_tokens_max_before": self.limits.role_tokens_max,
            "role_tokens_max_after": self.limits.role_tokens_max + tokens,
            "case_minutes_before": self.limits.case_minutes,
            "case_minutes_after": self.limits.case_minutes + minutes,
            "rounds_max_before": self.limits.rounds_max,
            "rounds_max_after": self.limits.rounds_max + rounds,
        }
        self.limits.role_tokens_max += tokens
        self.limits.case_minutes += minutes
        self.limits.rounds_max += rounds
        if self.limits.case_cost_usd_max is not None:
            # one more full USD allowance, like tokens and minutes
            self.limits.case_cost_usd_max += self.limits.case_cost_usd_max
            record["case_cost_usd_max_after"] = self.limits.case_cost_usd_max
        # The grant is one more full allowance for the CASE, not just for the
        # role that happened to breach first: the re-run re-dispatches every
        # role from the resume point, and a role sitting just under its old cap
        # would breach on its first dispatch and waste the grant.
        self.used["tokens_by_role"] = {}
        # Retries are spent the same way. A case that exhausted on retries
        # would otherwise reopen and stop on its very next dispatch, which is
        # the "grant did nothing" failure.
        self.used["retries_by_role"] = {}
        self.used.setdefault("grants", []).append(record)
        return record

    # ---------------------------------------------------------------- checks
    def _breach(self, code: str, message: str, detail: dict) -> BudgetReason:
        return BudgetReason(code, message, detail)

    def check_dispatch(self, role: str) -> tuple[bool, BudgetReason | None]:
        """Check every budget before dispatching *role* to an agent.

        Order: case clock first (design: checked at EVERY dispatch), then the
        per-role token cap. Each breach carries a precise reason.
        """
        elapsed = self.elapsed_minutes()
        if elapsed >= self.limits.case_minutes:
            return False, self._breach(
                "case_clock",
                f"case clock exceeded: {elapsed:.1f} of {self.limits.case_minutes:g} minutes elapsed",
                {"elapsed_minutes": round(elapsed, 3), "cap_minutes": self.limits.case_minutes},
            )
        tokens = self.used_tokens(role)
        if tokens >= self.limits.role_tokens_max:
            return False, self._breach(
                "role_tokens",
                f"role token cap for {role}: {tokens} of {self.limits.role_tokens_max} tokens used",
                {"role": role, "tokens": tokens, "cap": self.limits.role_tokens_max},
            )
        cost = self._cost_breach()
        if cost is not None:
            return False, cost
        return True, None

    def check_new_round(self) -> tuple[bool, BudgetReason | None]:
        """Check whether a further revision round fits the rounds budget."""
        rounds = self.used_rounds()
        if rounds >= self.limits.rounds_max:
            return False, self._breach(
                "rounds",
                f"rounds budget exhausted: {rounds} of {self.limits.rounds_max} rounds used",
                {"rounds": rounds, "max": self.limits.rounds_max},
            )
        return True, None

    def check_retry(self, role: str) -> tuple[bool, BudgetReason | None]:
        """Check the retries budget for an invalid-submission repair at *role*."""
        used = int((self.used.get("retries_by_role") or {}).get(role, 0))
        if used > self.limits.retries_max:
            return False, self._breach(
                "retries",
                f"retry budget for {role}: {used} invalid submissions exceeds {self.limits.retries_max}",
                {"role": role, "retries": used, "max": self.limits.retries_max},
            )
        return True, None

    def is_exhausted(self) -> bool:
        ok_clock, _ = self._check_clock_only()
        ok_rounds, _ = self.check_new_round()
        return not (ok_clock and ok_rounds)

    def _check_clock_only(self) -> tuple[bool, BudgetReason | None]:
        elapsed = self.elapsed_minutes()
        if elapsed >= self.limits.case_minutes:
            return False, self._breach(
                "case_clock",
                f"case clock exceeded: {elapsed:.1f} of {self.limits.case_minutes:g} minutes elapsed",
                {"elapsed_minutes": round(elapsed, 3), "cap_minutes": self.limits.case_minutes},
            )
        return True, None

    def check_acceptance(self) -> tuple[bool, BudgetReason | None]:
        """Acceptance can NEVER be granted on exhausted budgets."""
        ok, reason = self._check_clock_only()
        if not ok:
            return False, reason
        # Acceptance is evaluated AFTER the final round is consumed, so a case
        # that converges on its last allowed round has rounds == rounds_max.
        # check_new_round() refuses at >= rounds_max and would turn every such
        # convergence into EXHAUSTED; only an actual overrun is a breach.
        rounds = self.used_rounds()
        if rounds > self.limits.rounds_max:
            return False, self._breach(
                "rounds",
                f"rounds budget overrun: {rounds} of {self.limits.rounds_max} rounds used",
                {"rounds": rounds, "max": self.limits.rounds_max},
            )
        cost = self._cost_breach()
        if cost is not None:
            return False, cost
        # per-role caps: any role at cap means the case ran out of fuel
        for role, tokens in self.used["tokens_by_role"].items():
            if tokens >= self.limits.role_tokens_max:
                return False, self._breach(
                    "role_tokens",
                    f"role token cap for {role}: {tokens} of {self.limits.role_tokens_max} tokens used",
                    {"role": role, "tokens": tokens, "cap": self.limits.role_tokens_max},
                )
        return True, None
