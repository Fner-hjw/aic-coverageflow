"""CoverageFlow web UI (stream A4).

FastAPI service + static vanilla-JS SPA. This package is owned by stream A4:
`ui/api.py`, `ui/ws.py`, `ui/service.py`, `ui/static/*`.

The service boundary is `ui/service.py`: every endpoint in
docs/contracts/interfaces.md section 5 calls one of its functions. Until the
controller (A2) / dispatch (A3) streams land, those functions are local stubs
backed by realistic in-memory demo data; the coordinator swaps the bodies for
real `coverageflow` package calls without touching api.py / ws.py / app.js.
"""
