"""Backend adapters for the CoverageFlow agent layer.

Owner: stream A3. Provides the Backend protocol (base), CLI headless
adapters (cli_backends), an OpenAI-compatible API adapter (api_backend),
and record/replay backends (replay_backends).
"""

from .base import AgentResult, Backend, CostEstimate, HealthReport  # noqa: F401
