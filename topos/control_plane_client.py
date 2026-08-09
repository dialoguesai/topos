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
#: Keepalive. Two failure modes to stay between:
#:
#: * No deadline at all (ping_timeout=None) wedges forever on a silently dead
#:   socket — the node reports "connected" while the control plane holds
#:   nothing, and never recovers without a restart.
#: * A tight deadline kills HEALTHY connections. 20s did exactly that in
#:   production: pongs round-trip in ~0.2s, but the node could not always
#:   *process* them within 20s, so the link dropped every 50 seconds
#:   (20s to first ping + 20s deadline + 10s close handshake) and took any
#:   in-flight request — including a user's chat message — down with it.
#:
#: 90s is far beyond any observed local stall while still detecting a real
#: death inside ~2 minutes, and it lands before the control plane's own
#: eviction (30s ping + 120s timeout), so the node still notices first and
#: reconnects itself rather than being evicted.
PING_INTERVAL_SECONDS = 30.0
PING_TIMEOUT_SECONDS = 90.0

_FAST_INBOUND_MESSAGE_TYPES = frozenset(
    {
        "healthcheck",
        # Answerable from the client-thread snapshot even while the app loop
        # is frozen — behind the saturation gate it got rejected with "Engine
        # is busy" the moment 128 stalled handlers piled up, which turned into
        # 502s at the control plane for the exact request first-run setup
        # needs (the machine name).
        "get_device_info",
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
        # Shell bootstrap / upgrade banner — must not sit behind inbound saturation
        # while a version upgrade reprocess is walking sources.
        "get_runtime_bootstrap",
        "get_upgrade_status",
        # Settings → General owner display name (must not sit behind ingest saturation)
        "get_user_identity",
        "put_user_identity",
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
        # threading.Event, not asyncio.Event: readiness is signalled from the
        # client thread and awaited from the app loop.
        self._ready = threading.Event()
        # Dedicated-thread mode (production): the connection lives on its own
        # event loop so protocol pings are answered no matter how long the app
        # loop stalls. First-run model prewarm starved the app loop for
        # minutes; the node missed the control plane's pong deadline (120s)
        # and was executed as dead ~150s after registering — every time, on
        # every fresh install. The node never noticed: it was still stalled.
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handler_loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested = False
        # Last known-good device_info payload, answered from the client thread
        # when the app loop cannot. On a first run the app loop freezes for
        # minutes (a sync lock shared with model prewarm), every relayed
        # get_device_info 504s, and first-run setup cannot learn the machine
        # name — the journey that repeatedly shipped "Untitled Topos". The
        # machine name in this payload cannot go stale in any way that
        # matters.
        self._device_info_snapshot: dict[str, Any] | None = None
        self._device_info_snapshot_lock = threading.Lock()

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
        if self._thread is not None and self._thread.is_alive():
            return
        if self._task and not self._task.done():
            return
        self._stop_requested = False
        try:
            # Engine handlers run here — the loop the app lives on.
            self._handler_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._handler_loop = None
        self._thread = threading.Thread(
            target=self._thread_main, name="control-plane-client", daemon=True
        )
        self._thread.start()
        logger.info("Control plane client starting: %s", self.control_plane_url)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        # Fresh, bound to this loop: the one from __init__ may already be bound
        # to the app loop by an earlier legacy run.
        self._stop = asyncio.Event()
        try:
            loop.run_until_complete(self._run())
        except Exception:  # noqa: BLE001
            logger.exception("Control plane client thread crashed")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()
            self._loop = None

    def _restart_if_task_stopped(self) -> None:
        # Self-heal if the reconnect loop exited unexpectedly while the app is still running.
        if self._stop_requested:
            return
        if self._thread is not None:
            if self._thread.is_alive():
                return
            logger.warning(
                "Control plane client thread stopped unexpectedly; restarting endpoint=%s",
                self.control_plane_url,
            )
            self._thread = None
            self.start()
            return
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
        if self._ready.is_set():
            return True
        # threading.Event.wait in a worker so neither loop is blocked.
        return await asyncio.to_thread(self._ready.wait, max(0.1, timeout))

    async def stop(self) -> None:
        self._set_state("stopping")
        self._stop_requested = True
        loop = self._loop
        if self._thread is not None and self._thread.is_alive() and loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._shutdown_on_client_loop(), loop)
                await asyncio.wait_for(asyncio.wrap_future(fut), timeout=10.0)
            except Exception:  # noqa: BLE001
                logger.warning("Control plane client shutdown did not complete cleanly", exc_info=True)
            await asyncio.to_thread(self._thread.join, 10.0)
            self._thread = None
        else:
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

    async def _shutdown_on_client_loop(self) -> None:
        """Runs on the client thread's loop; unblocks _run so the thread exits."""
        self._stop.set()
        if self._ws:
            try:
                await self._ws.close(code=1000)
            except Exception:
                pass
        await self._cancel_inbound_tasks()

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
                    # A real pong deadline, restored once the client got its own
                    # thread and loop. It was disabled because handlers ran here
                    # and could block long enough to miss pongs — that reason is
                    # gone: handlers now marshal to the app loop, so this loop is
                    # always free to answer and to notice silence.
                    #
                    # Disabling it turned every silent connection death into a
                    # permanent wedge: `async for raw in ws` blocked forever, no
                    # exception fired, no reconnect was scheduled, and the node
                    # reported "connected" while the control plane had no socket
                    # at all. Observed live 2026-08-08 — 25 minutes wedged, with
                    # the web app correctly showing the Topos offline.
                    ping_interval=PING_INTERVAL_SECONDS,
                    ping_timeout=PING_TIMEOUT_SECONDS,
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

        loop = self._loop
        if loop is None:
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

    def set_device_info_snapshot(self, payload: dict[str, Any] | None) -> None:
        """Thread-safe; the engine seeds this at startup while the loop is
        healthy, and every relayed answer refreshes it."""
        if not isinstance(payload, dict) or not payload:
            return
        with self._device_info_snapshot_lock:
            self._device_info_snapshot = dict(payload)

    def _device_info_snapshot_response(self, request_id: Any) -> dict[str, Any] | None:
        with self._device_info_snapshot_lock:
            snapshot = self._device_info_snapshot
        if not snapshot:
            return None
        payload = dict(snapshot)
        payload["snapshot_stale"] = True
        return {"id": request_id, "status": "ok", "payload": payload}

    _SNAPSHOT_ANSWER_DEADLINE_S = 5.0

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
            handler_loop = self._handler_loop
            running = asyncio.get_running_loop()
            if handler_loop is not None and handler_loop is not running and handler_loop.is_running():
                # Engine handlers belong on the app loop. This await parks on
                # the CLIENT loop while the app loop is busy — which is the
                # entire point: a stalled app queues work but never costs the
                # connection, because pings keep being answered here.
                handler_future = asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(self.handler(data), handler_loop)
                )
                if msg_type == "get_device_info":
                    resp = await self._await_with_device_info_fallback(ws, data, handler_future)
                    if resp is None:
                        return
                else:
                    resp = await handler_future
            else:
                resp = await self.handler(data)
        except Exception as exc:  # noqa: BLE001
            logger.error("Handler raised exception: %s", exc, exc_info=True)
            resp = {"id": data.get("id"), "status": "error", "error": str(exc)}
        if resp is None:
            return  # e.g. connection_info or message without id; CP has no pending request to match
        await self._send_ws_json(ws, resp)

    async def _await_with_device_info_fallback(self, ws, data: Dict[str, Any], handler_future) -> dict[str, Any] | None:
        """Answer get_device_info from the snapshot when the app loop is too
        stalled to answer itself. Returns the response still to be sent, or
        None when the snapshot already answered (the late real answer then
        only refreshes the snapshot — the CP has stopped waiting)."""
        try:
            resp = await asyncio.wait_for(asyncio.shield(handler_future), timeout=self._SNAPSHOT_ANSWER_DEADLINE_S)
            if isinstance(resp, dict) and resp.get("status") == "ok" and isinstance(resp.get("payload"), dict):
                self.set_device_info_snapshot(resp["payload"])
            return resp
        except (TimeoutError, asyncio.TimeoutError):
            fallback = self._device_info_snapshot_response(data.get("id"))
            if fallback is None:
                # Nothing cached yet — let the real handler run to completion.
                resp = await handler_future
                if isinstance(resp, dict) and resp.get("status") == "ok" and isinstance(resp.get("payload"), dict):
                    self.set_device_info_snapshot(resp["payload"])
                return resp
            logger.warning(
                "get_device_info answered from snapshot: app loop did not respond within %.1fs",
                self._SNAPSHOT_ANSWER_DEADLINE_S,
            )
            await self._send_ws_json(ws, fallback)

            async def _refresh_from_late_answer() -> None:
                try:
                    late = await handler_future
                except Exception:  # noqa: BLE001
                    return
                if isinstance(late, dict) and late.get("status") == "ok" and isinstance(late.get("payload"), dict):
                    self.set_device_info_snapshot(late["payload"])

            asyncio.create_task(_refresh_from_late_answer())
            return None

    async def send_message(self, message: Dict[str, Any]) -> None:
        """Send an unsolicited message to the control plane (e.g., progress updates)."""
        loop = self._loop
        if loop is not None and self._thread is not None and self._thread.is_alive():
            running = asyncio.get_running_loop()
            if loop is not running:
                # The websocket lives on the client thread's loop; writing to
                # it from another loop is not safe. Marshal, then await.
                fut = asyncio.run_coroutine_threadsafe(self._send_message_on_loop(message), loop)
                await asyncio.wrap_future(fut)
                return
        await self._send_message_on_loop(message)

    async def _send_message_on_loop(self, message: Dict[str, Any]) -> None:
        if not self._ws:
            self._restart_if_task_stopped()
            await self._enqueue_presence_message(message)
            logger.warning("Queued presence message; control plane currently disconnected")
            return
        sent = await self._send_ws_json(self._ws, message)
        if not sent:
            await self._enqueue_presence_message(message)
