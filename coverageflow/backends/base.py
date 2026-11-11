"""Backend primitives: Backend protocol, AgentResult, HealthReport, CostEstimate.

Owner: stream A3. Shapes are frozen by docs/contracts/interfaces.md section 2 -
do not change field names or semantics without a new contract version.

`RoleCall` is defined in coverageflow.dispatch (the single source of truth for
the role-call envelope); it is imported only for type checking to avoid a
circular import (dispatch imports backends).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..dispatch import RoleCall


@dataclass
class AgentResult:
    """Normalized outcome of one backend invocation (contracts interfaces s2).

    `ok=True` only when the backend produced usable output text; JSON parsing
    and schema validation happen in dispatch.py, which flips `ok` to False and
    fills `error` with repair guidance when the parsed artifact is invalid.
    """

    ok: bool
    output_text: str | None = None
    parsed: dict | None = None
    error: str | None = None
    backend: str = ""
    elapsed_seconds: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None
    raw_response_ref: str | None = None
    # AGENT_RESULT record produced by dispatch_role, carried so the
    # orchestrator can merge the prompt sha / card version into its own trace
    # instead of dropping them.
    trace: dict | None = None
    # True when the failure is not repairable by the role (e.g. its inputs are
    # absent): the orchestrator blocks the case instead of spending retries.
    blocking: bool = False
    # Reported spend in USD (claude envelope total_cost_usd); None when the
    # backend does not report cost.
    cost_usd: float | None = None
    # Portion of tokens_in served from the prompt cache (already included in
    # tokens_in for codex; summed into tokens_in for claude). None if unknown.
    tokens_cached: int | None = None
    # Model name the backend was configured with, for trace metadata.
    model: str | None = None

    def failed(self, error: str) -> "AgentResult":
        """Return a copy of this result marked failed with `error`.

        Convenience used by dispatch when strict parsing/schema validation
        fails: keeps raw output and token usage for the trace record.
        """
        return AgentResult(
            ok=False,
            output_text=self.output_text,
            parsed=None,
            error=error,
            backend=self.backend,
            elapsed_seconds=self.elapsed_seconds,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            raw_response_ref=self.raw_response_ref,
            trace=self.trace,
            cost_usd=self.cost_usd,
            tokens_cached=self.tokens_cached,
            model=self.model,
        )


@dataclass
class HealthReport:
    """Result of a backend healthcheck (cheap, no full prompt round-trip)."""

    backend: str
    ok: bool
    detail: str = ""
    checked_at: str = ""

    def __post_init__(self) -> None:
        if not self.checked_at:
            self.checked_at = _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class CostEstimate:
    """Best-effort pre-invoke cost estimate for a prompt text."""

    backend: str
    estimated_tokens: int
    method: str
    note: str | None = None


@runtime_checkable
class Backend(Protocol):
    """Every backend adapter implements exactly this (contracts interfaces s2)."""

    name: str

    def healthcheck(self) -> HealthReport:
        """Cheap availability probe; must not dispatch a full prompt."""
        ...

    def invoke(self, call: "RoleCall", assembled_prompt: str) -> AgentResult:
        """One fresh invocation; no session reuse across calls."""
        ...

    def estimate_cost(self, text: str) -> CostEstimate | None:
        """Estimate prompt tokens (headers/4 fallback); None if unknowable."""
        ...


def estimate_tokens_chars_per_four(text: str) -> int:
    """Best-effort token estimate: 1 token per ~4 characters.

    Used when a backend does not report real usage. char/4 underestimates
    CJK-heavy text and overestimates whitespace; acceptable for budget
    guardrails, exact usage is surfaced when the backend reports it.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)
