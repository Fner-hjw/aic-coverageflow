"""FastAPI application factory for the CoverageFlow web UI (stream A4).

PURPOSE: implements EVERY endpoint of docs/contracts/interfaces.md section 5
plus the static SPA mount.  Endpoints only translate HTTP <-> the
ui.service boundary: they contain no state or orchestration logic (the
coordinator swaps service stubs for real controller calls here and above).

No auth anywhere: this is a local tool (asserted by tests).  No 500s for
expected failures: service errors map to 404/409/422 with the error payload.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, Response as FResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import service
from .ws import ws_router, _startup, _shutdown

MAX_BODY_BYTES = 1_000_000
_STATIC_DIR = Path(__file__).resolve().parent / "static"


class _RevalidatedStatic(StaticFiles):
    """Static files the browser must revalidate on every load.

    A local desk tool changes its UI between restarts; with default caching a
    browser kept an old stylesheet next to new markup. ``no-cache`` still
    allows a 304 from the ETag, so reloads stay cheap.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Wire the service event hook + periodic snapshot loop while serving."""
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


def _err(status: int, detail: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=detail)


def _handle_service_error(exc: service.ServiceError) -> JSONResponse:
    return _err(exc.status, exc.detail)


def create_app() -> FastAPI:
    """Build the FastAPI app: JSON API + /ws/events + static SPA."""
    app = FastAPI(title="CoverageFlow UI", version="a4", lifespan=_lifespan,
                  docs_url="/api/openapi.json", openapi_url="/api/openapi.json",
                  redoc_url=None)

    # ---- global error shaping (JSON, never HTML tracebacks) --------------
    @app.exception_handler(service.ServiceError)
    async def _svc_handler(_req: Request, exc: service.ServiceError):
        return _handle_service_error(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_req: Request, exc: StarletteHTTPException):
        return _err(exc.status_code, {"error": str(exc.detail)})

    @app.exception_handler(RequestValidationError)
    async def _req_handler(_req: Request, exc: RequestValidationError):
        return _err(422, {"error": "request validation failed",
                          "errors": exc.errors()})

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):
        """Refuse oversized request bodies before any parsing (input hygiene)."""
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = MAX_BODY_BYTES + 1
        if size > MAX_BODY_BYTES:
            return _err(413, {"error": f"request body too large (max {MAX_BODY_BYTES} bytes)"})
        return await call_next(request)

    # ---- health & backends ------------------------------------------------
    @app.get("/api/health")
    def health() -> dict:
        return service.health()

    @app.get("/api/backends")
    def backends() -> dict:
        return service.get_backends()

    @app.post("/api/backends/select")
    def backend_select(payload: dict) -> dict:
        return service.select_backend(str(payload.get("backend") or ""))

    @app.post("/api/backends/api/configure")
    def backend_api_configure(payload: dict) -> dict:
        return service.configure_api(payload)

    # ---- mandate prefill (proposal only; submitting the form accepts it) ----
    @app.post("/api/mandate/prefill")
    def mandate_prefill(payload: dict) -> dict:
        peers = payload.get("peers")
        if isinstance(peers, str):
            peers = [p for p in peers.replace(" ", "").split(",") if p]
        return service.start_prefill_job(
            str(payload.get("ticker") or ""), str(payload.get("cutoff_date") or ""),
            list(peers) if isinstance(peers, list) else None, payload.get("erp"))

    @app.get("/api/mandate/prefill/{job_id}")
    def mandate_prefill_status(job_id: str) -> dict:
        return service.prefill_job_status(job_id)

    # ---- cases ------------------------------------------------------------
    @app.get("/api/cases")
    def cases() -> dict:
        return service.list_cases()

    @app.post("/api/cases")
    def case_create(payload: dict) -> dict:
        return service.create_case(payload)

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str) -> dict:
        return service.get_case(case_id)

    @app.delete("/api/cases/{case_id}")
    def case_delete(case_id: str, confirm: str = "") -> dict:
        return service.delete_case(case_id, confirm)

    @app.get("/api/deleted-cases")
    def deleted_cases() -> dict:
        return service.list_deleted_cases()

    @app.post("/api/deleted-cases/{name}/restore")
    def deleted_case_restore(name: str) -> dict:
        return service.restore_deleted_case(name)

    @app.delete("/api/deleted-cases/{name}")
    def deleted_case_purge(name: str, confirm: str = "") -> dict:
        return service.purge_deleted_case(name, confirm)

    @app.get("/api/cases/{case_id}/trace")
    def case_trace(case_id: str, request: Request):
        frm = _int_query(request, "from", 0)
        return service.get_trace(case_id, frm)

    @app.get("/api/cases/{case_id}/artifacts/{path:path}")
    def artifact(case_id: str, path: str, raw: bool = False) -> Response:
        data = service.read_artifact(case_id, path)
        if raw:
            text = data["content"]["text"] if isinstance(
                data["content"], dict) and "text" in data["content"] else (
                _dumps(data["content"]))
            return FResponse(content=text, media_type="text/plain")
        return JSONResponse(content=data)

    @app.post("/api/cases/{case_id}/run")
    def case_run(case_id: str, payload: dict | None = None) -> dict:
        p = payload or {}
        return service.run_case(
            case_id, p.get("backend", "auto"), bool(p.get("replay", False)),
            bool(p.get("extend", False)))

    @app.post("/api/cases/{case_id}/resume")
    def case_resume(case_id: str) -> dict:
        return service.resume_case(case_id)

    @app.post("/api/cases/{case_id}/approval")
    def case_approval(case_id: str, payload: dict) -> dict:
        ack = payload.get("acknowledge_findings") or []
        if not isinstance(ack, list):
            raise service.ValidationError(
                "acknowledge_findings must be a list of finding ids")
        return service.record_approval(
            case_id, str(payload.get("decision") or ""),
            str(payload.get("by") or ""), str(payload.get("reasoning") or ""),
            acknowledge_findings=[str(a) for a in ack])

    @app.post("/api/cases/{case_id}/demo/{action}")
    def case_demo(case_id: str, action: str, payload: dict | None = None) -> dict:
        p = payload or {}
        return service.demo_action(case_id, action,
                                   p.get("stage") if isinstance(
                                       p.get("stage"), str) else None)

    @app.get("/api/cases/{case_id}/report")
    def case_report(case_id: str, version: str | None = None) -> dict:
        return service.get_report(case_id, version)

    @app.post("/api/cases/{case_id}/export")
    def case_export(case_id: str, payload: dict) -> dict:
        return service.export_case(case_id, str(payload.get("format") or ""))

    @app.get("/api/cases/{case_id}/export-file/{filename}")
    def export_file(case_id: str, filename: str):
        data = service.export_download(case_id, filename)
        return FResponse(content=data["bytes"],
                         media_type=data["media_type"],
                         headers={"Content-Disposition":
                                  f'attachment; filename="{filename}"'})

    @app.get("/api/cases/{case_id}/replay")
    def replay_list(case_id: str) -> dict:
        return service.list_replays(case_id)

    @app.get("/api/cases/{case_id}/replay/{request_id}")
    def replay_one(case_id: str, request_id: str) -> dict:
        return service.read_replay(case_id, request_id)

    # ---- evolution --------------------------------------------------------
    @app.get("/api/versions")
    def versions() -> dict:
        return service.get_versions()

    @app.post("/api/evolution/prepare")
    def evolution_prepare(payload: dict) -> dict:
        return service.evolution_prepare(payload)

    @app.post("/api/evolution/regress")
    def evolution_regress(payload: dict) -> dict:
        return service.evolution_regress(str(payload.get("version") or ""))

    @app.post("/api/evolution/approve")
    def evolution_approve(payload: dict) -> dict:
        return service.evolution_approve(
            str(payload.get("version") or ""),
            str(payload.get("decision") or ""),
            str(payload.get("by") or ""),
            str(payload.get("reasoning") or ""))

    @app.post("/api/evolution/rollback")
    def evolution_rollback(payload: dict) -> dict:
        return service.evolution_rollback(str(payload.get("to_version") or ""))

    # ---- EDGAR + bundles --------------------------------------------------
    @app.get("/api/edgar/search")
    def edgar_search(request: Request):
        ticker = str(request.query_params.get("ticker") or "")
        if not ticker:
            return _err(422, {"error": "ticker query parameter is required"})
        return service.edgar_search(
            ticker, request.query_params.get("forms"),
            request.query_params.get("window"))

    @app.get("/api/bundles")
    def bundles() -> dict:
        return service.list_bundles()

    @app.post("/api/bundles")
    def bundle_create(payload: dict) -> dict:
        docs = payload.get("docs")
        return service.build_bundle(
            str(payload.get("cik") or ""),
            list(docs) if isinstance(docs, list) else [],
            str(payload.get("name") or ""))

    @app.post("/api/bundles/{bundle_id}/extend")
    def bundle_extend(bundle_id: str, payload: dict) -> dict:
        docs = payload.get("docs")
        return service.extend_bundle(
            bundle_id, list(docs) if isinstance(docs, list) else [],
            str(payload.get("name") or ""))

    @app.delete("/api/bundles/{bundle_id}")
    def bundle_delete(bundle_id: str) -> dict:
        return service.delete_bundle(bundle_id)

    @app.patch("/api/bundles/{bundle_id}")
    def bundle_rename(bundle_id: str, payload: dict) -> dict:
        return service.rename_bundle(bundle_id, str(payload.get("name") or ""))

    # ---- WebSocket push ----------------------------------------------------
    app.include_router(ws_router)
    app.mount("/static", _RevalidatedStatic(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"),
                            headers={"Cache-Control": "no-cache"})

    return app


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, indent=2, default=str)


def _int_query(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


app = create_app()
