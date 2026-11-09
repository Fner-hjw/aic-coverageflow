"""WebSocket hub for the CoverageFlow web UI (stream A4).

PURPOSE: WS /ws/events pushes `{case_id, event, ts}` notices for every
orchestrator event plus periodic full-state snapshots, per contracts
interfaces §5.  The SPA falls back to 3s polling of /api/cases when the
socket is down, so the WS layer is strictly best-effort: a broken or empty
sink never blocks a state operation (service._emit swallows sink errors).

The service boundary stays clean: service only calls set_event_hook(); all
async/socket machinery lives here and is wired in api.create_app().
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import service

SNAPSHOT_INTERVAL_SECONDS = 5


def _ts() -> str:
    """Live UTC timestamp for every pushed message."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class EventsHub:
    """Fan-out for service events; per-connection inbox queues."""

    def __init__(self) -> None:
        self.clients: list[asyncio.Queue] = []

    # -- service hook target (called synchronously from service) -----------
    def on_service_event(self, event: dict) -> None:
        msg = {"type": "event", "case_id": event.get("case_id"),
               "event": event.get("event"), "ts": _ts(),
               "detail": event.get("detail", {})}
        self._enqueue(msg)

    def on_snapshot(self, snapshot: dict) -> None:
        self._enqueue({"type": "snapshot", "ts": _ts(),
                       "cases": snapshot})

    # -- internals -----------------------------------------------------------
    def _enqueue(self, msg: dict) -> None:
        for q in list(self.clients):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(msg)

    async def serve(self, ws: WebSocket, since_snapshot: bool = True) -> None:
        """Register the client and stream its inbox until disconnect."""
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.clients.append(q)
        try:
            if since_snapshot:
                # immediate state snapshot so a fresh tab renders instantly
                await ws.send_json({"type": "snapshot", "ts": _ts(),
                                    "cases": _summarize()})
            while True:
                msg = await q.get()
                await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if q in self.clients:
                self.clients.remove(q)


def _summarize() -> dict:
    """Periodic snapshot payload: the same data as GET /api/cases."""
    return service.list_cases()


hub = EventsHub()
ws_router = APIRouter()


@ws_router.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """WS endpoint: pushes events + a snapshot every SNAPSHOT_INTERVAL s.

    Periodic snapshots double as liveness proof for the polling fallback:
    the SPA tracks last-message time and switches to 3s polling of
    /api/cases if the socket stays quiet beyond the snapshot interval.
    """
    await hub.serve(ws)


async def snapshot_loop() -> None:
    """Background task: periodic state snapshots to every client."""
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)
        try:
            hub.on_snapshot(_summarize())
        except Exception:  # pragma: no cover - snapshot must never crash
            continue


_task: asyncio.Task | None = None


async def _startup() -> None:
    """Lifespan wiring: service events -> hub, snapshot loop running."""
    global _task
    service.set_event_hook(hub.on_service_event)
    _task = asyncio.create_task(snapshot_loop())


async def _shutdown() -> None:
    global _task
    service.set_event_hook(None)
    if _task is not None:
        _task.cancel()
        _task = None
