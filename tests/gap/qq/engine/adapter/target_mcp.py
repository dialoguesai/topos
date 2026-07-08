"""QueryTarget over the control-plane MCP gateway. REAL — Phase 1 / plan D1.9.

The over-the-wire twin of ``target_engine.py``: the same catalog and analysis run against
a remote node through the production MCP path, so the engine↔MCP parity delta becomes a
first-class measured number instead of a sidecar pass-rate. Client pattern reused from
``scripts/run_query_eval.py::run_mcp_eval`` (streamablehttp_client + ClientSession,
Bearer auth, ``X-Topos-Client: topos-home-chat/1`` → native client identity, so the
parity lane measures the same policy surface the home screen uses).

The gateway's ``query_scope`` returns the pipeline result dict (public_result, audit,
turn_outcome, ...), so :func:`target_engine.normalize_result` applies unchanged — one
normalizer, two transports, which is the whole point of the seam.

Concurrency shape — one task owns the session's whole lifetime. The MCP SDK's context
managers are anyio task groups, which MUST be entered and exited by the same asyncio
task; splitting connect/teardown across two ``run_coroutine_threadsafe`` calls raises
"Attempted to exit cancel scope in a different task than it was entered in" at every
``with``-block exit. A single ``_session_task`` coroutine connects, parks on a shutdown
event, and unwinds its own context managers; ``query()`` submits ``call_tool`` coroutines
against the live session (request I/O is not task-bound — only enter/exit is).

v1 scope notes:
  * Owner identity only. Grantee parity needs the shared_* tool surface and UMA auth —
    a later lane, refused loudly here rather than half-measured.
  * ``latency_ms`` is client-observed round trip (transport + gateway + engine). When the
    gateway ships the versioned ``server_ms`` envelope field (plan §2.1 decision #6),
    normalize_result picks the raw payload up automatically via ``Response.raw``.
  * One session for the target's lifetime (context manager) — per-query reconnects would
    smear TLS/session setup into every case's latency.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from typing import Any, Dict
from urllib.parse import urlparse

from topos_eval.protocols.target import Request, Response

from target_engine import normalize_result

CLIENT_LABEL = "topos-home-chat/1"


class McpTarget:
    """QueryTarget backed by a live MCP gateway. Use as a context manager:

        with McpTarget("https://cp.logu3s.com", topos_key) as target:
            response = target.query(request)
    """

    def __init__(
        self,
        base_url: str,
        topos_key: str,
        *,
        timeout_s: float = 600.0,
        connect_timeout_s: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = topos_key
        self._timeout_s = timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._session_future: Any = None
        self._shutdown: asyncio.Event | None = None
        self._ready = threading.Event()
        self._connect_error: BaseException | None = None

    # -- lifecycle -------------------------------------------------------------

    def __enter__(self) -> "McpTarget":
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session_future = asyncio.run_coroutine_threadsafe(
            self._session_task(), self._loop
        )
        if not self._ready.wait(self._connect_timeout_s):
            self._connect_error = TimeoutError(
                f"MCP connect to {self._base_url} timed out after {self._connect_timeout_s}s"
            )
        if self._connect_error is not None:
            # __exit__ never runs when __enter__ raises — clean up here or leak the
            # loop thread + half-open transport on every failed connect.
            self._teardown()
            raise RuntimeError(f"MCP connect failed: {self._connect_error}")
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._loop is None:
            return
        if self._shutdown is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        if self._session_future is not None:
            try:
                self._session_future.result(timeout=10)
            except Exception:
                pass  # the task's error already surfaced via _connect_error / queries
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._session = None

    async def _session_task(self) -> None:
        """Owns connect → serve → unwind in ONE task (see module docstring)."""
        from contextlib import AsyncExitStack

        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            self._connect_error = ImportError(f"MCP SDK required for McpTarget: {exc}")
            self._ready.set()
            return

        try:
            async with AsyncExitStack() as stack:
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(
                        f"{self._base_url}/mcp",
                        headers={
                            "Authorization": f"Bearer {self._key}",
                            "X-Topos-Client": CLIENT_LABEL,
                        },
                    )
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._session = session
                self._shutdown = asyncio.Event()
                self._ready.set()
                await self._shutdown.wait()
        except BaseException as exc:
            self._connect_error = exc
            raise
        finally:
            self._session = None
            self._ready.set()

    def _submit(self, coro: Any) -> Any:
        assert self._loop is not None, "use McpTarget as a context manager"
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self._timeout_s)

    # -- QueryTarget --------------------------------------------------------------

    def query(self, request: Request) -> Response:
        """One request over the wire, normalized. A denial is a Response
        (turn_outcome='denied'), never an exception; a transport failure is an error
        Response, honestly."""
        if request.identity != "owner":
            raise ValueError(
                "McpTarget v1 measures the owner query_scope surface only; grantee parity "
                "is the shared_* lane (see module docstring)"
            )
        args: Dict[str, Any] = {
            "scope_id": request.scope,
            "access_mode": request.access_mode,
            "intent": request.text,
            "query_session_id": request.session_id or f"eval-mcp-{uuid.uuid4().hex[:8]}",
        }

        t0 = time.perf_counter()
        if self._session is None:
            return normalize_result(
                {"turn_outcome": "error", "deny_reason": "MCP session not connected"}, 0.0
            )
        try:
            raw_result = self._submit(self._session.call_tool("query_scope", args))
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            return normalize_result(
                {"turn_outcome": "error", "deny_reason": f"{type(exc).__name__}: {exc}"},
                latency_ms,
            )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        payload: Dict[str, Any] = {}
        if getattr(raw_result, "structuredContent", None):
            payload = dict(raw_result.structuredContent)
        elif getattr(raw_result, "content", None):
            text = next(
                (c.text for c in raw_result.content if getattr(c, "text", None)), ""
            )
            if text:
                try:
                    parsed = json.loads(text)
                    payload = parsed if isinstance(parsed, dict) else {
                        "turn_outcome": "error",
                        "deny_reason": f"non-object tool result: {text[:200]}",
                    }
                except json.JSONDecodeError:
                    payload = {
                        "turn_outcome": "error",
                        "deny_reason": f"non-JSON tool result: {text[:200]}",
                    }
        if getattr(raw_result, "isError", False) and not payload.get("turn_outcome"):
            payload = {"turn_outcome": "error", "deny_reason": json.dumps(payload)[:200]}

        return normalize_result(payload, latency_ms)

    @property
    def name(self) -> str:
        host = urlparse(self._base_url).netloc or self._base_url
        return f"mcp@{host}"
