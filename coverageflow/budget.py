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
    "case_minutes": 45,
    "role_tokens_max": 60000,
    "approval_hours": 48,
}


@dataclass
class Limits:
    """Immutable per-case budget limits (from mandate.limits, with defaults)."""
    rounds_max: int = 4
    retries_max: int = 3
    case_minutes: float = 45.0
    role_tokens_max: int = 60000
    approval_hours: float = 48.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Limits":
        d = d or {}
        return cls(
            rounds_max=int(d.get("rounds_max", DEFAULT_LIMITS["rounds_max"])),
            retries_max=int(d.get("retries_max", DEFAULT_LIMITS["retries_max"])),
            case_minutes=float(d.get("case_minutes", DEFAULT_LIMITS["case_minutes"])),
            role_tokens_max=int(d.get("role_tokens_max", DEFAULT_LIMITS["role_tokens_max"])),
            approval_hours=float(d.get("approval_hours", DEFAULT_LIMITS["approval_hours"])),
        )


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
        }

    def limits_state(self) -> dict:
        return {
            "rounds_max": self.limits.rounds_max,
            "retries_max": self.limits.retries_max,
            "case_minutes": self.limits.case_minutes,
            "role_tokens_max": self.limits.role_tokens_max,
            "approval_hours": self.limits.approval_hours,
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
        ok, reason = self.check_new_round()
        if not ok:
            return False, reason
        # per-role caps: any role at cap means the case ran out of fuel
        for role, tokens in self.used["tokens_by_role"].items():
            if tokens >= self.limits.role_tokens_max:
                return False, self._breach(
                    "role_tokens",
                    f"role token cap for {role}: {tokens} of {self.limits.role_tokens_max} tokens used",
                    {"role": role, "tokens": tokens, "cap": self.limits.role_tokens_max},
                )
        return True, None
