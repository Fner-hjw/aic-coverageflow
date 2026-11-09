"""Role-call dispatch: RoleCall envelope, prompt assembly, backend invocation,
strict output parsing, budget enforcement, and trace-record production.

Owner: stream A3. `RoleCall` is the single source of truth for the role-call
envelope (docs/contracts/interfaces.md section 1). Dispatch NEVER writes trace
or state itself - it returns a trace-record dict; only the orchestrator (A2)
appends it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleCall:
    """One bound role request dispatched to a backend (interfaces s1)."""

    role: str                    # ingest|extractor|model|writer|critic_c1..c4|generator_lead|critic_lead
    request_id: str
    candidate_id: str | None
    role_card: dict              # {"path": "prompts/ingest.md", "version": "1.0", "sha256": "..."}
    payload: dict                # bound request data injected into the assembled prompt
    output_schema: str           # json-schema ref; submission validated against it
    budget: dict                 # {"tokens_max": 60000, "timeout_seconds": 240}
    write_scope: list[str]       # exactly one artifact path for worker roles
