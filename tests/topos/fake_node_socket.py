"""A stand-in node on a unix socket for the on-ramp CLI and MCP guards.

Serves the handful of routes CONTRACTS.md gives the `owner_agent` class and the
§2.8 owner doors, with knobs for every state the guards need: a revoked label
(401 ``agent_disconnected``), an unpaired node (409 ``not_paired``), a rejected
control-plane key, planted secrets in every body. Nothing here is the real
node: lanes L1/L2/L5 own those routes, and this fake pins only the shapes
CONTRACTS.md promises.

It also keeps a recording TCP listener so a test can prove a code path made
zero TCP connections.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

AGENT_DISCONNECTED_BODY = {
    "error": "agent_disconnected",
    "message": "This agent was disconnected from Topos.",
}
ALLOWED_TYPES = ("query", "aggregate", "get_sources")


class FakeNode:
    def __init__(self, *, expected_label: Optional[str] = None) -> None:
        self.expected_label = expected_label
        self.calls: list[dict[str, Any]] = []
        self.revoked = False
        #: Serve the 401 as FastAPI's HTTPException does before L2 registers a
        #: handler: the node's dict wrapped under ``detail``.
        self.wrap_401_in_detail = False
        self.paired = True
        self.control_plane = "connected"
        self.owner_key_persisted = True
        self.version = "9.9.9"
        self.update: dict[str, Any] = {"available": False, "installed": "9.9.9", "latest": None}
        #: Extra keys merged into /v1/shell/status (lane L7's fields, e.g. supervisor).
        self.extra_status: dict[str, Any] = {}
        #: False: the §2.8 scope door answers 404 (a node without lane L5).
        self.scope_door = True
        self.planted_secret: Optional[str] = None
        #: Seconds a dispatch sleeps before answering (progress-tick guard).
        self.delay_s = 0.0
        self.sources: list[dict[str, Any]] = [
            {"source_id": "claude_code_sessions", "display_name": "AI coding tools", "schedule": "hourly"},
        ]
        self.settings: dict[str, dict[str, Any]] = {
            "claude_code_sessions": {
                "enabled": True,
                "paused": False,
                "last_sync_at": "2026-10-15T20:00:00+00:00",
                "last_error": None,
                "rows": 42,
            }
        }
        self.dispatch_payloads: dict[str, dict[str, Any]] = {
            "query": {
                "turn_outcome": "live_query",
                "public_result": {"scope_id": "messages:read", "access_mode": "raw", "rows": []},
                "query_session_id": "qs_1",
            },
            "aggregate": {"turn_outcome": "live_query", "public_result": {"value": 3}},
            "get_sources": {"status": "ok", "sources": []},
        }
        self.door_results: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self.app = self._build()

    # ------------------------------------------------------------------ app
    def _plant(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.planted_secret:
            return {**body, "planted": self.planted_secret, "message": f"key={self.planted_secret}"}
        return body

    def _build(self) -> FastAPI:
        app = FastAPI()
        node = self

        @app.post("/api/local/agent/dispatch")
        async def dispatch(request: Request):
            body = await request.json()
            label = request.headers.get("x-topos-agent")
            node.calls.append({"route": "dispatch", "label": label, "body": body})
            if label is not None and (node.revoked or (node.expected_label and label != node.expected_label)):
                body = {"detail": AGENT_DISCONNECTED_BODY} if node.wrap_401_in_detail else AGENT_DISCONNECTED_BODY
                return JSONResponse(body, status_code=401)
            msg_type = body.get("type")
            if label is not None and msg_type not in ALLOWED_TYPES:
                return JSONResponse({"error": "owner_agent_not_allowed"}, status_code=403)
            if node.delay_s:
                await asyncio.sleep(node.delay_s)
            if msg_type == "get_sources":
                payload = {"status": "ok", "sources": list(node.sources)}
            else:
                payload = node.dispatch_payloads.get(msg_type, {})
            return {"id": "req-1", "status": "ok", "payload": node._plant(dict(payload))}

        @app.get("/v1/shell/status")
        async def shell_status():
            node.calls.append({"route": "shell_status"})
            return node._plant(
                {
                    "name": "topos-node",
                    "version": node.version,
                    "pid": os.getpid(),
                    "started_at": time.time(),
                    "log_file": None,
                    "update": dict(node.update),
                    "models": {},
                    "control_plane": node.control_plane,
                    "owner_key_persisted": node.owner_key_persisted,
                    "topos_id_present": node.paired,
                    "user_id_present": node.paired,
                    **node.extra_status,
                }
            )

        @app.get("/api/local/owner/scope")
        async def owner_scope():
            node.calls.append({"route": "scope"})
            if not node.scope_door:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            if not node.paired:
                return JSONResponse({"error": "not_paired"}, status_code=409)
            return node._plant({"user_id": "user-1", "topos_id": "topos-1", "dataset_id": "user-1:topos:topos-1"})

        @app.get("/sources")
        async def sources():
            node.calls.append({"route": "sources"})
            return {"sources": list(node.sources)}

        @app.get("/sources/{source_id}/settings")
        async def settings(source_id: str, dataset_id: str = ""):
            node.calls.append({"route": "settings", "source_id": source_id, "dataset_id": dataset_id})
            found = node.settings.get(source_id)
            if found is None:
                return {"status": "error", "error": "unknown source_id"}
            return node._plant({"status": "ok", "dataset_id": dataset_id, "source_id": source_id, **found})

        @app.post("/api/local/owner/sources/{source_id}/{action}")
        async def door(source_id: str, action: str, request: Request):
            body = await request.json() if int(request.headers.get("content-length") or 0) else {}
            node.calls.append({"route": "door", "source_id": source_id, "action": action, "body": body})
            if not node.paired:
                return JSONResponse({"error": "not_paired"}, status_code=409)
            if (source_id, action) in node.door_results:
                status, payload = node.door_results[(source_id, action)]
                return JSONResponse(node._plant(dict(payload)), status_code=status)
            if action == "preview":
                return node._plant(
                    {"sessions": 3, "bytes_text": 1200, "folders": ["alpha", "beta"],
                     "oldest": "2026-09-01T00:00:00+00:00", "newest": "2026-10-15T00:00:00+00:00"}
                )
            return node._plant({"status": "ok", "source_id": source_id, "action": action})

        return app


@contextlib.contextmanager
def serve_fake_node(node: FakeNode, socket_path: Path) -> Iterator[FakeNode]:
    """Serve ``node.app`` on ``socket_path`` for the duration of the block."""
    import uvicorn

    from topos.uds import socket_is_live

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    config = uvicorn.Config(node.app, uds=str(socket_path), lifespan="off", log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="fake-node-uds", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not socket_is_live(socket_path):
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("fake node did not come up")
        time.sleep(0.02)
    try:
        yield node
    finally:
        server.should_exit = True
        thread.join(10)
        with contextlib.suppress(OSError):
            socket_path.unlink()


def dead_socket(socket_path: Path) -> Path:
    """A socket FILE nobody listens on: connecting is refused."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(socket_path))
    s.close()
    return socket_path


class RecordingTcpListener:
    """Counts connection attempts on an ephemeral loopback port."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="fake-tcp-listener", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        with contextlib.suppress(OSError):
            self._sock.close()
