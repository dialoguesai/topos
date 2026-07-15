from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import json
import logging
import ssl
import threading
from typing import Any, Awaitable, Callable, Dict

import certifi
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config.settings import settings
from .core.connection_resilience import (
    ConnectionSnapshot,
    ConnectionState,
    ExponentialBackoff,
    FailureCategory,
    ResilienceConfig,
    classify_connection_error,
    is_fatal_connection_category,
    utc_now_iso,
)

logger = logging.getLogger("topos.control_plane_client")

# Lightweight CP RPCs that must stay responsive while ingest/enrichment runs.
# UI-critical reads and scheduler ops only — no ingest/enrichment writes.
_FAST_INBOUND_MESSAGE_TYPES = frozenset(
    {
        "healthcheck",
        "connection_info",
        "check_inbox_write",
        "list_waiting_routine_runs",
        "list_due_routines",
        "routine_has_active_run",
        "update_routine_run",
        "get_routine",
        "advance_routine_next_run_at",
        # UI reads (routines, home chat, temporal graph)
        "list_routines",
        "list_routine_runs",
        "get_routine_run",
        "list_home_chat_sessions",
        "get_home_chat_session",
        "signal_entity_graph",
        "signal_list_entities",
        "signal_get_entity",
    }
)


class ControlPlaneClient:
    """Maintains a WS connection to the control plane and dispatches incoming requests."""

    def __init__(
        self,
        control_plane_url: str,
        api_key: str,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]],
        verify_ssl: bool = True,
    ):
        self.control_plane_url = control_plane_url
        self.api_key = api_key
        self.handler = handler
        self.verify_ssl = verify_ssl
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._ws = None
        self._state: ConnectionState = "idle"
        self._state_changed_at: str | None = utc_now_iso()
        self._last_connected_at: str | None = None
        self._last_disconnected_at: str | None = None
        self._last_failure_category: FailureCategory = "none"
        self._last_failure_reason: str = ""
        self._attempt = 0
        self._consecutive_failures = 0
        self._ready = asyncio.Event()

        self._inbound_concurrency_limit = max(1, int(settings.control_plane_inbound_concurrency_limit))
        self._inbound_max_pending = max(
            self._inbound_concurrency_limit,
            int(settings.control_plane_inbound_max_pending),
        )
        self._inbound_semaphore = asyncio.Semaphore(self._inbound_concurrency_limit)
        self._inbound_tasks: set[asyncio.Task] = set()
        self._inbound_lock = asyncio.Lock()

        self._presence_outbox_size = max(1, int(settings.control_plane_presence_outbox_size))
        self._presence_outbox: deque[dict[str, Any]] = deque(maxlen=self._presence_outbox_size)
        self._outbox_lock = asyncio.Lock()
        # Thread-safe outbox for sync callers (Engine.run in worker threads via to_thread).
        self._sync_outbox: deque[dict[str, Any]] = deque(maxlen=self._presence_outbox_size)
        self._sync_outbox_lock = threading.Lock()

        self._backoff = ExponentialBackoff(
            ResilienceConfig(
                initial_backoff_s=max(0.1, float(settings.connection_retry_initial_seconds)),
                max_backoff_s=max(1.0, float(settings.connection_retry_max_seconds)),
                jitter_ratio=max(0.0, float(settings.connection_retry_jitter_ratio)),
            )
        )

    def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        self._state_changed_at = utc_now_iso()

    def get_connection_status(self) -> dict[str, Any]:
        snapshot = ConnectionSnapshot(
            state=self._state,
            connected=bool(self._ws),
            attempt=self._attempt,
            consecutive_failures=self._consecutive_failures,
            last_failure_category=self._last_failure_category,
            last_failure_reason=self._last_failure_reason,
            last_state_change_at=self._state_changed_at,
            last_connected_at=self._last_connected_at,
            last_disconnected_at=self._last_disconnected_at,
            outbox_depth=len(self._presence_outbox),
        )
        return snapshot.to_dict()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("Control plane client starting: %s", self.control_plane_url)

    def _restart_if_task_stopped(self) -> None:
        # Self-heal if the reconnect loop exited unexpectedly while the app is still running.
        if self._stop.is_set() or not self._task or not self._task.done():
            return
        reason = "unknown"
        try:
            exc = self._task.exception()
            if exc is not None:
                reason = repr(exc)
        except asyncio.CancelledError:
            reason = "cancelled"
        logger.warning(
            "Control plane background task stopped unexpectedly; restarting endpoint=%s reason=%s",
            self.control_plane_url,
            reason,
        )
        self.start()

    async def wait_until_connected(self, timeout_s: float | None = None) -> bool:
        timeout = float(timeout_s) if timeout_s is not None else float(settings.connection_readiness_timeout_seconds)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=max(0.1, timeout))
            return True
        except (TimeoutError, asyncio.TimeoutError):
            # Py3.10: asyncio.TimeoutError is not builtins.TimeoutError.
            return False

    async def stop(self) -> None:
        self._set_state("stopping")
        self._stop.set()
        if self._ws:
            try:
                await self._ws.close(code=1000)
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._cancel_inbound_tasks()
        self._ws = None
        self._ready.clear()
        self._set_state("idle")

    async def _cancel_inbound_tasks(self) -> None:
        async with self._inbound_lock:
            pending = list(self._inbound_tasks)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        ssl_context = None
        if self.control_plane_url.startswith("wss://"):
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            if not self.verify_ssl:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
        while not self._stop.is_set():
            self._set_state("connecting")
            self._attempt += 1
            try:
                async with connect(
                    self.control_plane_url,
                    additional_headers=headers,
                    ssl=ssl_context,
                    # Transform-heavy requests can block the event loop in the handler path long enough
                    # to miss pong deadlines; disable pong timeout-driven disconnects.
                    ping_timeout=None,
                ) as ws:
                    self._ws = ws
                    self._set_state("connected")
                    self._ready.set()
                    self._backoff.reset()
                    self._last_failure_category = "none"
                    self._last_failure_reason = ""
                    self._consecutive_failures = 0
                    self._last_connected_at = utc_now_iso()
                    logger.info("Control plane client connected")
                    await self._flush_presence_outbox()
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            data = json.loads(raw)
                        except Exception:
                            logger.warning("Relay message is not valid JSON: %s", raw)
                            continue
                        await self._schedule_inbound_message(ws, data)
            except ConnectionClosed as exc:
                self._record_failure(exc)
            except Exception as exc:  # noqa: BLE001
                self._record_failure(exc)
            finally:
                self._ws = None
                self._ready.clear()
                if not self._stop.is_set() and self._state != "stopping":
                    self._last_disconnected_at = utc_now_iso()
            if self._stop.is_set():
                break
            delay = self._backoff.next_delay()
            self._set_state("degraded" if is_fatal_connection_category(self._last_failure_category) else "backing_off")
            logger.warning(
                "Control plane reconnect scheduled endpoint=%s state=%s attempt=%d failures=%d category=%s delay_s=%.2f reason=%s",
                self.control_plane_url,
                self._state,
                self._attempt,
                self._consecutive_failures,
                self._last_failure_category,
                delay,
                self._last_failure_reason,
            )
            await self._wait_for_stop_or_timeout(delay)
        self._set_state("idle")

    def _record_failure(self, exc: BaseException) -> None:
        category, reason = classify_connection_error(exc)
        self._last_failure_category = category
        self._last_failure_reason = reason
        self._consecutive_failures += 1
        logger.warning(
            "Control plane connectivity event endpoint=%s event=connection_failed category=%s failures=%d reason=%s",
            self.control_plane_url,
            category,
            self._consecutive_failures,
            reason,
        )

    async def _wait_for_stop_or_timeout(self, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            # Expected path: backoff elapsed. Must not escape — on Py3.10
            # asyncio.TimeoutError is distinct from builtins.TimeoutError and
            # would otherwise kill the reconnect loop task.
            return

    async def _schedule_inbound_message(self, ws, data: Dict[str, Any]) -> None:
        msg_type = str(data.get("type") or "")
        if msg_type in _FAST_INBOUND_MESSAGE_TYPES:
            task = asyncio.create_task(self._handle_message(ws, data))
            self._inbound_tasks.add(task)
            task.add_done_callback(self._on_inbound_task_done)
            return
        async with self._inbound_lock:
            pending_count = len(self._inbound_tasks)
            if pending_count >= self._inbound_max_pending:
                logger.warning("Dropping inbound request due to saturation pending=%d", pending_count)
                request_id = data.get("id")
                if request_id:
                    await self._send_ws_json(
                        ws,
                        {"id": request_id, "status": "error", "error": "Engine is busy. Retry shortly."},
                    )
                return
            task = asyncio.create_task(self._handle_message_guarded(ws, data))
            self._inbound_tasks.add(task)
            task.add_done_callback(self._on_inbound_task_done)

    def _on_inbound_task_done(self, task: asyncio.Task) -> None:
        self._inbound_tasks.discard(task)

    async def _handle_message_guarded(self, ws, data: Dict[str, Any]) -> None:
        async with self._inbound_semaphore:
            await self._handle_message(ws, data)

    async def _flush_presence_outbox(self) -> None:
        await self._drain_sync_outbox_into_presence()
        async with self._outbox_lock:
            pending = list(self._presence_outbox)
            self._presence_outbox.clear()
        if not pending:
            return
        for message in pending:
            success = await self._send_ws_json(self._ws, message)
            if not success:
                await self._enqueue_presence_message(message)
                break

    async def _drain_sync_outbox_into_presence(self) -> None:
        with self._sync_outbox_lock:
            pending = list(self._sync_outbox)
            self._sync_outbox.clear()
        for message in pending:
            await self._enqueue_presence_message(message)

    async def _enqueue_presence_message(self, message: Dict[str, Any]) -> None:
        async with self._outbox_lock:
            at_capacity = len(self._presence_outbox) >= self._presence_outbox_size
            if at_capacity:
                dropped = self._presence_outbox.popleft()
                logger.warning(
                    "Presence outbox full; dropping oldest message type=%s",
                    dropped.get("type"),
                )
            self._presence_outbox.append(dict(message))

    def enqueue_unsolicited_message_threadsafe(self, message: Dict[str, Any]) -> None:
        """Queue an unsolicited WS message from a sync/worker thread.

        Enrichment runs Engine.run via asyncio.to_thread; those threads cannot safely
        touch the websockets connection. Queue here and flush on the client's loop.
        """
        with self._sync_outbox_lock:
            at_capacity = len(self._sync_outbox) >= self._presence_outbox_size
            if at_capacity:
                dropped = self._sync_outbox.popleft()
                logger.warning(
                    "Sync outbox full; dropping oldest message type=%s",
                    dropped.get("type"),
                )
            self._sync_outbox.append(dict(message))

        client_task = self._task
        if client_task is None:
            return
        try:
            loop = client_task.get_loop()
        except Exception:
            return
        if not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._flush_presence_outbox(), loop)
        except Exception:
            logger.debug("Failed to schedule sync outbox flush", exc_info=True)

    async def _send_ws_json(self, ws, payload: Dict[str, Any]) -> bool:
        if not ws:
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send message to control plane: %s", exc)
            return False

    async def _handle_message(self, ws, data: Dict[str, Any]) -> None:
        msg_type = str(data.get("type") or "").strip().lower()
        if msg_type == "ping":
            pong: Dict[str, Any] = {"type": "pong"}
            ping_id = data.get("id")
            if ping_id is not None:
                pong["id"] = ping_id
            await self._send_ws_json(ws, pong)
            return
        try:
            resp = await self.handler(data)
        except Exception as exc:  # noqa: BLE001
            logger.error("Handler raised exception: %s", exc, exc_info=True)
            resp = {"id": data.get("id"), "status": "error", "error": str(exc)}
        if resp is None:
            return  # e.g. connection_info or message without id; CP has no pending request to match
        await self._send_ws_json(ws, resp)

    async def send_message(self, message: Dict[str, Any]) -> None:
        """Send an unsolicited message to the control plane (e.g., progress updates)."""
        if not self._ws:
            self._restart_if_task_stopped()
            await self._enqueue_presence_message(message)
            logger.warning("Queued presence message; control plane currently disconnected")
            return
        sent = await self._send_ws_json(self._ws, message)
        if not sent:
            await self._enqueue_presence_message(message)
