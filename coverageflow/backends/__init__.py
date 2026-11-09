"""Backend adapters for the CoverageFlow agent layer.

Owner: stream A3. Provides the Backend protocol (base), CLI headless adapters
(cli_backends), an OpenAI-compatible API adapter (api_backend), record/replay
backends (replay_backends), and small factory/auto-detection helpers used by
the controller CLI's ``--backend`` / ``backends|health`` commands (A2).
"""

from __future__ import annotations

from typing import Any

from .base import AgentResult, Backend, CostEstimate, HealthReport  # noqa: F401

_KNOWN_BACKENDS = ("claude", "codex", "kimi", "api", "replay")


def make_backend(name: str, **kwargs: Any):
    """Instantiate a backend by name (contracts interfaces s2 "Backend select").

    ``replay`` requires ``replay_dir``; ``api`` accepts the constructor
    options of ApiBackend; CLI backends accept ``cli_path``. Unknown names
    raise ValueError - the CLI turns that into the interactive selection
    prompt.
    """
    if name == "claude":
        from .cli_backends import ClaudeBackend

        return ClaudeBackend(**kwargs)
    if name == "codex":
        from .cli_backends import CodexBackend

        return CodexBackend(**kwargs)
    if name == "kimi":
        from .cli_backends import KimiBackend

        return KimiBackend(**kwargs)
    if name == "api":
        from .api_backend import ApiBackend

        return ApiBackend(**kwargs)
    if name == "replay":
        from .replay_backends import ReplayBackend

        return ReplayBackend(**kwargs)
    raise ValueError(
        f"unknown backend {name!r}; known: {', '.join(_KNOWN_BACKENDS)}"
    )


def detect_available_backends() -> list[str]:
    """Non-interactive auto-detection for ``--backend auto`` (A2 uses this).

    Returns the names of backends that appear usable in this process
    environment: CLI binaries present on PATH that pass healthcheck, plus
    'api' when configured. Never launches an interactive session.
    """
    available: list[str] = []
    for name in ("claude", "codex", "kimi"):
        backend = make_backend(name)
        if backend.healthcheck().ok:
            available.append(name)
    api = make_backend("api")
    if api.healthcheck().ok:
        available.append("api")
    return available
