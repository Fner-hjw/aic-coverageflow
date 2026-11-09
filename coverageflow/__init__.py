"""CoverageFlow — long-running agent workflow for sell-side equity coverage.

Package init (owned by the coordinator; module ownership per stream):
- runtime engines, EDGAR bundles, slicer, xlsx export — runtime & data stream
- orchestrator, statestore, gates, ledger, budget, resume, registry, cli — controller stream
- backends, dispatch, prompt assembly, prompts/, references/ — agent-layer stream
- ui/ service + SPA — web UI stream

Frozen contracts: docs/contracts/state-and-artifacts.md and docs/contracts/interfaces.md.
"""

__version__ = "0.1.0"
ENGINES_VERSION = "1.0"
SCHEMA_VERSION = "1.0"
