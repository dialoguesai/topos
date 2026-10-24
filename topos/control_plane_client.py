from __future__ import annotations

import asyncio
import contextlib
from collections import deque
import json
import logging
import ssl
import threading
import time
import weakref
from typing import Any, Awaitable, Callable, Dict

import certifi
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config.settings import settings
from .permissions_v2 import answer_gate
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
#: R-N154 M2: at a stop, how long the asks already being handled get to send their replies before the socket closes.
#: Inside node_stop's 12 s bound for the client's whole stop.
INBOUND_DRAIN_SECONDS = 5.0
#: What an ask that arrives while the node is stopping is answered with, at once.
STOPPING_ERROR = "Engine is stopping. Retry shortly."
#: NR166 R3a. How long, in seconds since this client read it off the socket, a relayed request of each type may
#: have waited when a handler slot comes free for it. Past that the control plane has stopped waiting: its relay
#: budget for the type has run out and the asker already has its "try again", so the work (a share search holds
#: the write gate for seconds, an ask starts a model job) would be done for nobody. Such a request gets the busy
#: reply a full queue already gives, once, and is not handled.
#:
#: The numbers are the control plane's relay budgets and MUST NEVER BE BELOW ITS SETTINGS: a bound under the
#: control plane's would shed requests it is still waiting for. Its settings are
#: ``permissions_beta_v2_search_relay_seconds`` (default 25; unset in production, so 25) and
#: ``permissions_beta_v2_answer_relay_seconds`` (default 10; production runs 20, read from the running container
#: on 13 Oct 2026). A batch also carries the control plane's own ``respond_by``; where that is earlier, it is used.
#: The node should not have to guess: the control plane should send its budget with every relayed request.
#:
#: A closed table. A type that is not in it is never aged out and never dropped. That leaves out, on purpose:
#: every consent write and protocol command (``permissions_v2_mutate``, ``_bind``, ``_identity_command``,
#: ``_ingest_snapshot``, the evidence and projection review records and revokes, the opt-out and opt-in,
#: ``_message_review``, ``_entailment_review``, ``_permitted_derivation``, ``_message_search_rebuild``), which are
#: applied however late they are reached; ``ping`` and the fast lane, which never wait for a slot; and every other
#: type, whose control-plane budgets run from 2 s to 300 s by caller and are not this node's to guess.
INBOUND_AGE_BOUND_SECONDS = {
    "permissions_v2_message_search": 25.0,
    "permissions_v2_message_search_batch": 25.0,
    "permissions_v2_answer_submit": 20.0,
    "permissions_v2_answer_fetch": 20.0,
}
#: At most one line in this long about requests that were not handled.
INBOUND_SHED_LOG_SECONDS = 60.0
_SHED_FIELDS = (("search", "permissions_v2_message_search"), ("search_batch", "permissions_v2_message_search_batch"),
                ("answer_submit", "permissions_v2_answer_submit"), ("answer_fetch", "permissions_v2_answer_fetch"))

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
        "list_active_routine_runs",
        "list_expired_routine_runs",
        "delete_routine_runs",
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


def _wake_lane_of(client_ref) -> Callable[[], None]:
    """OFF2N: what the owner's away settings call on a change, for the client ``client_ref`` names (weakly)."""
    def wake() -> None:
        client = client_ref()
        lane = getattr(client, "_owner_lane", None)
        loop = getattr(client, "_loop", None)
        if lane is None or loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(lane.wake)
    return wake


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
        self._healthcheck_snapshot: dict[str, Any] | None = None
        self._healthcheck_snapshot_lock = threading.Lock()

        self._inbound_concurrency_limit = max(1, int(settings.control_plane_inbound_concurrency_limit))
        self._inbound_max_pending = max(
            self._inbound_concurrency_limit,
            int(settings.control_plane_inbound_max_pending),
        )
        self._inbound_semaphore = asyncio.Semaphore(self._inbound_concurrency_limit)
        # NR183 O1: with TOPOS_PERMISSIONS_V2_OWNER_LANE on, the handler slots are this pool instead of the semaphore,
        # some of them kept for the owner's own app (permissions_v2/owner_lane.py). None, the switch off: the semaphore.
        from .permissions_v2 import owner_lane

        self._owner_lane = owner_lane.slots_for(self._inbound_concurrency_limit)
        # OFF2N: with the lane on, the owner's own frames are counted for the yield rule and owner idleness.
        answer_gate.owner_frames_counted(self._owner_lane is not None)
        if self._owner_lane is not None:
            # OFF2N: "Share evenly" or back, pushed by the control plane on another thread: the pool's waiting frames
            # are handed what the new mode frees, on the client's own loop. A client that is gone hears nothing.
            from .permissions_v2 import away_config

            away_config.on_change(_wake_lane_of(weakref.ref(self)))
        self._inbound_tasks: set[asyncio.Task] = set()
        # UI reads get a separate bounded reserve, never an unbounded bypass.
        self._fast_inbound_tasks: set[asyncio.Task] = set()
        self._fast_inbound_max_pending = 32
        self._inbound_lock = asyncio.Lock()
        # R-N154 M2: set at the start of a stop; new asks are refused while the ones in hand finish.
        self._draining = False
        self._inbound_drain_seconds = INBOUND_DRAIN_SECONDS
        # NR166 R3a: requests not handled at dequeue, ("aged" | "gone", type) -> times, since the start and
        # since the last log line; the longest wait for a slot seen since that line.
        self._inbound_shed: dict[tuple[str, str], int] = {}
        self._inbound_shed_window: dict[tuple[str, str], int] = {}
        self._inbound_oldest = 0.0
        self._inbound_shed_timer: asyncio.TimerHandle | None = None

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
        self._draining = False
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
        # A stop() that ran before this thread published `_loop` could not reach
        # this loop, so it set the Event from __init__ -- the one just replaced.
        # Without this the thread ran `_run()` against a fresh, unset Event and
        # reconnected forever after stop() had returned: 25 of 40 back-to-back
        # start()/stop() calls left one alive. stop() sets `_stop_requested`
        # before it reads `_loop`, so whichever side gets there first, this
        # sees it.
        if self._stop_requested:
            self._stop.set()
        try:
            from .permissions_v2 import loop_lag

            loop_lag.watch(loop, "client")   # NR166 R3b: attribution only; does nothing with the switch off
            from .storage.db import write_gate

            write_gate.name_event_loop(loop, "client")   # the gate's on-loop warning says which loop
        except Exception:  # noqa: BLE001
            pass
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
            self._draining = True
            await self._drain_inbound_tasks(self._inbound_drain_seconds)
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
            # Started, but the thread had not published its loop yet. It will see
            # `_stop_requested` as soon as it has, and exit -- wait for that, so
            # stop() never returns with a client thread still running.
            thread = self._thread
            if thread is not None and thread.is_alive():
                await asyncio.to_thread(thread.join, 10.0)
                self._thread = None
        self._ws = None
        self._ready.clear()
        self._set_state("idle")

    async def _shutdown_on_client_loop(self) -> None:
        """Runs on the client thread's loop; unblocks _run so the thread exits.

        Two phases (R-N154 M2). First the asks already being handled get up to ``_inbound_drain_seconds`` to send
        their replies while every new ask is refused at once with ``STOPPING_ERROR``: a search past its checkpoint
        has written its receipt and spent its share's allowance, and an admitted answer has its id, so closing under
        them lost the reply the node had already paid for. Then the socket closes and what is left is cancelled."""
        self._draining = True
        await self._drain_inbound_tasks(self._inbound_drain_seconds)
        self._stop.set()
        if self._ws:
            try:
                await self._ws.close(code=1000)
            except Exception:
                pass
        await self._cancel_inbound_tasks()

    async def _drain_inbound_tasks(self, timeout_s: float) -> None:
        async with self._inbound_lock:
            pending = [task for task in self._inbound_tasks if not task.done()]
        if pending and timeout_s > 0:
            await asyncio.wait(pending, timeout=timeout_s)

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
                            # NR181: never the frame itself (it can carry a question, an answer or shared records).
                            from .core.log_fingerprint import fingerprint
                            logger.warning("Relay message is not valid JSON: %s", fingerprint(raw))
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
        if self._draining and msg_type not in _FAST_INBOUND_MESSAGE_TYPES and msg_type != "ping":
            # R-N154 M2: the node is stopping. Refuse at once, so the asker hears now rather than at a timeout.
            request_id = data.get("id")
            if request_id:
                await self._send_ws_json(ws, {"id": request_id, "status": "error", "error": STOPPING_ERROR})
            return
        if msg_type == "ping":
            await self._handle_message(ws, data)
            return
        if msg_type in _FAST_INBOUND_MESSAGE_TYPES:
            if len(self._fast_inbound_tasks) >= self._fast_inbound_max_pending:
                if msg_type == "healthcheck":
                    await self._send_ws_json(ws, self._healthcheck_snapshot_response(data.get("id")))
                elif msg_type == "get_device_info" and self._device_info_snapshot_response(data.get("id")):
                    await self._send_ws_json(ws, self._device_info_snapshot_response(data.get("id")))
                else:
                    await self._send_busy_response(ws, data)
                return
            task = asyncio.create_task(self._handle_message(ws, data))
            self._inbound_tasks.add(task)
            self._fast_inbound_tasks.add(task)
            task.add_done_callback(self._on_inbound_task_done)
            return
        # NR183 O1: the owner lane's class for this frame; never read with the lane off.
        lane = self._owner_lane
        owner = lane is not None and lane.owner_frame(data)
        async with self._inbound_lock:
            pending_count = len(self._inbound_tasks) - len(self._fast_inbound_tasks)
            # With the lane on, each class counts its own frames in hand only (DR-04: the owner's activity never
            # makes a recipient's frame busy); counted out when its task ends, however it ends.
            if (pending_count >= self._inbound_max_pending if lane is None
                    else not lane.admit(owner, self._inbound_max_pending)):
                logger.warning("Dropping inbound request due to saturation pending=%d", pending_count)
                request_id = data.get("id")
                if request_id:
                    await self._send_busy_response(ws, data)
                return
            # R3a: when it arrived, and whether on the connection this client holds now (see _shed_at_dequeue).
            arrived = (time.monotonic(), ws is self._ws)
            # OFF2N: an owner frame's task is marked as one (nothing it runs waits for the owner: it would wait for
            # itself), and counted in flight from here until it ends, however it ends (the yield rule, idleness).
            with answer_gate.owner_frame() if owner else contextlib.nullcontext():
                task = asyncio.create_task(self._handle_message_guarded(ws, data, arrived) if lane is None
                                           else self._handle_message_in_lane(lane, owner, ws, data, arrived))
            if lane is not None:
                task.add_done_callback(lambda _done: lane.left(owner))
            if owner:
                answer_gate.owner_frame_began()
                task.add_done_callback(lambda _done: answer_gate.owner_frame_ended())
            self._inbound_tasks.add(task)
            task.add_done_callback(self._on_inbound_task_done)

    def _on_inbound_task_done(self, task: asyncio.Task) -> None:
        self._inbound_tasks.discard(task)
        self._fast_inbound_tasks.discard(task)

    async def _send_busy_response(self, ws, data: Dict[str, Any]) -> None:
        if data.get("id"):
            await self._send_ws_json(ws, {
                "id": data["id"], "type": data.get("type"), "status": "error",
                "error": "Engine is busy. Retry shortly.", "error_code": "engine_busy",
                "retry_after_seconds": 30,
            })

    async def _handle_message_guarded(self, ws, data: Dict[str, Any], arrived: tuple[float, bool] | None = None) -> None:
        async with self._inbound_semaphore:
            if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):
                return
            await self._handle_message(ws, data)

    async def _handle_message_in_lane(self, lane, owner: bool, ws, data: Dict[str, Any],
                                      arrived: tuple[float, bool] | None = None) -> None:
        """NR183 O1, the lane on: ``_handle_message_guarded`` with a slot of the lane's pool for the semaphore. R3a's
        check runs where it ran, once the slot is held; the slot is given back on every path out, an exception and
        a cancellation included (one cancelled while it waited holds none: ``InboundSlots.acquire``)."""
        slot = await lane.acquire(owner)
        try:
            if arrived is not None and await self._shed_at_dequeue(ws, data, *arrived):
                return
            await self._handle_message(ws, data)
        finally:
            lane.release(slot)

    async def _shed_at_dequeue(self, ws, data: Dict[str, Any], received: float, on_live_socket: bool) -> bool:
        """NR166 R3a, at the moment a handler slot is free: True when this request is not to be handled.

        Only the types in ``INBOUND_AGE_BOUND_SECONDS``. One whose connection closed while it waited is dropped:
        the control plane failed it when the socket closed, and its reply is never sent on a newer connection.
        One older than its type's bound gets the busy reply, once. Anything else is handled as before.
        """
        msg_type = str(data.get("type") or "")
        bound = INBOUND_AGE_BOUND_SECONDS.get(msg_type)
        if bound is None:
            return False
        waited = time.monotonic() - received
        self._inbound_oldest = max(self._inbound_oldest, waited)
        if on_live_socket and ws is not self._ws:
            why = "gone"
        elif waited > bound or self._past_respond_by(msg_type, data):
            why = "aged"
            await self._send_busy_response(ws, data)
        else:
            return False
        for counts in (self._inbound_shed, self._inbound_shed_window):
            counts[(why, msg_type)] = counts.get((why, msg_type), 0) + 1
        if self._inbound_shed_timer is None:   # the first of a window: its line is written when the window ends
            self._inbound_shed_timer = asyncio.get_running_loop().call_later(
                INBOUND_SHED_LOG_SECONDS, self._log_inbound_shed, INBOUND_SHED_LOG_SECONDS)
        return True

    @staticmethod
    def _past_respond_by(msg_type: str, data: Dict[str, Any]) -> bool:
        """The batch door's own deadline: the control plane says in the frame when it stops waiting (epoch ms).
        Advisory, as the door itself reads it: anything but a positive integer is left for the door to refuse."""
        if msg_type != "permissions_v2_message_search_batch":
            return False
        payload = data.get("payload")
        respond_by = payload.get("respond_by") if isinstance(payload, dict) else None
        return (isinstance(respond_by, int) and not isinstance(respond_by, bool) and respond_by > 0
                and time.time() * 1000 >= respond_by)

    def inbound_shed_counts(self) -> dict[tuple[str, str], int]:
        """Requests not handled at dequeue since this client was made: ("aged" | "gone", type) -> times."""
        return dict(self._inbound_shed)

    def _log_inbound_shed(self, window: float) -> None:
        """One fixed-field line for the window that began at its first shed request: so at most one line per
        window, and none while nothing is shed. Counts and one age (the longest wait for a slot in the window,
        handled or not): no request id, no payload."""
        self._inbound_shed_timer = None
        counts, self._inbound_shed_window = self._inbound_shed_window, {}
        oldest, self._inbound_oldest = self._inbound_oldest, 0.0
        if not counts:
            return
        by_type = " ".join(f"{why}_{name}={counts.get((why, kind), 0)}"
                           for why in ("aged", "gone") for name, kind in _SHED_FIELDS)
        logger.warning("control_plane_inbound_shed window_s=%d aged=%d gone=%d oldest_s=%.1f %s", round(window),
                       sum(n for (why, _), n in counts.items() if why == "aged"),
                       sum(n for (why, _), n in counts.items() if why == "gone"), oldest, by_type)

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
            # Routing fields only, never the payload: this is the one line that
            # says which request's answer died on a dead socket.
            logger.error(
                "Failed to send message to control plane: id=%s type=%s status=%s exc=%s: %s",
                payload.get("id"),
                payload.get("type"),
                payload.get("status"),
                type(exc).__name__,
                exc,
            )
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
        return {"id": request_id, "status": "ok", "type": "get_device_info", "payload": payload}

    def set_healthcheck_snapshot(self, payload: dict[str, Any] | None) -> None:
        if not isinstance(payload, dict) or not payload:
            return
        with self._healthcheck_snapshot_lock:
            self._healthcheck_snapshot = dict(payload)

    def _healthcheck_snapshot_response(self, request_id: Any) -> dict[str, Any]:
        with self._healthcheck_snapshot_lock:
            snapshot = self._healthcheck_snapshot
        payload = dict(snapshot) if snapshot else {}
        db_ok = payload.get("db_ok") if isinstance(payload.get("db_ok"), bool) else None
        return {
            "id": request_id,
            "status": "ok",
            "type": "healthcheck",
            "payload": {"ok": True, "db_ok": db_ok, "snapshot_stale": True, "accepting_writes": False},
        }

    def _echo_request_type(self, resp: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        inbound = str(data.get("type") or "").strip()
        if inbound and not str(resp.get("type") or "").strip():
            echoed = dict(resp)
            echoed["type"] = inbound
            return echoed
        return resp

    _SNAPSHOT_ANSWER_DEADLINE_S = 5.0
    _HEALTHCHECK_ANSWER_DEADLINE_S = 1.0

    async def _handle_message(self, ws, data: Dict[str, Any]) -> None:
        msg_type = str(data.get("type") or "").strip().lower()
        if msg_type == "permissions_v2_verify_claim":
            from .permissions_v2.truth_release import dispatch_verify_claim
            await dispatch_verify_claim(ws, data)
            return
        if msg_type in ("permissions_v2_verify_claim_statement", "permissions_v2_interpret_claim"):
            # verify-claim effort high: a free statement read by the pinned local model, then checked or shown.
            from .permissions_v2.truth_statement import dispatch_statement
            await dispatch_statement(ws, data)
            return
        if msg_type == "permissions_v2_message_search":
            # p2c-v1: checkpoints and signs under the node gates, then sends with them released.
            from .permissions_v2.search_transport import dispatch_message_search
            await dispatch_message_search(ws, data)
            return
        if msg_type == "permissions_v2_message_search_batch":
            # OD-36: N searches of one grant under one verification pass; answered whole or refused whole.
            from .permissions_v2.search_transport import dispatch_message_search_batch
            await dispatch_message_search_batch(ws, data)
            return
        if msg_type in ("permissions_v2_answer_submit", "permissions_v2_answer_fetch"):
            from .permissions_v2.answer_transport import dispatch_answer
            await dispatch_answer(ws, data)
            return
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
                # OFF2N: an owner frame's mark does not cross run_coroutine_threadsafe; the handler is wrapped in it.
                call = self.handler(data)
                if answer_gate.in_owner_frame():
                    call = answer_gate.within_owner_frame(call)
                handler_future = asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(call, handler_loop)
                )
                if msg_type == "get_device_info":
                    resp = await self._await_with_device_info_fallback(ws, data, handler_future)
                    if resp is None:
                        return
                elif msg_type == "healthcheck":
                    resp = await self._await_with_healthcheck_fallback(ws, data, handler_future)
                    if resp is None:
                        return
                else:
                    resp = await handler_future
            else:
                resp = await self.handler(data)
        except Exception as exc:  # noqa: BLE001
            # NR181: the error's class and where it was raised, never its text: a handler's error can quote the
            # frame it was handling (a validation error repeats its input). The frames are code locations only.
            import traceback
            logger.error("Handler raised exception: type=%s id=%s error=%s\n%s", msg_type, data.get("id"),
                         type(exc).__name__, "".join(traceback.format_tb(exc.__traceback__)).rstrip())
            resp = {"id": data.get("id"), "status": "error", "error": str(exc)}
        if resp is None:
            return  # e.g. connection_info or message without id; CP has no pending request to match
        if msg_type == "healthcheck" and resp.get("status") == "ok":
            resp = {**resp, "payload": {
                **(resp.get("payload") or {}),
                "accepting_writes": not self._draining and len(self._inbound_tasks) - len(self._fast_inbound_tasks) < self._inbound_max_pending,
            }}
        await self._send_ws_json(ws, self._echo_request_type(resp, data))

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
                logger.warning(
                    "get_device_info: empty snapshot; answering stale stub after %.1fs",
                    self._SNAPSHOT_ANSWER_DEADLINE_S,
                )
                fallback = {
                    "id": data.get("id"),
                    "status": "ok",
                    "type": "get_device_info",
                    "payload": {"snapshot_stale": True, "snapshot_empty": True},
                }
            else:
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

            # Keep late work in the bounded task set until it actually finishes.
            await _refresh_from_late_answer()
            return None

    async def _await_with_healthcheck_fallback(self, ws, data: Dict[str, Any], handler_future) -> dict[str, Any] | None:
        """Answer healthcheck from the WS client thread when the app loop is
        too stalled. A 1s miss still beats the CP 20s timeout and keeps the
        FE verdict younger than HEALTH_VERDICT_MAX_AGE_MS."""
        try:
            resp = await asyncio.wait_for(asyncio.shield(handler_future), timeout=self._HEALTHCHECK_ANSWER_DEADLINE_S)
            if isinstance(resp, dict) and resp.get("status") == "ok" and isinstance(resp.get("payload"), dict):
                self.set_healthcheck_snapshot(resp["payload"])
            return resp
        except (TimeoutError, asyncio.TimeoutError):
            fallback = self._healthcheck_snapshot_response(data.get("id"))
            logger.warning(
                "healthcheck answered from client-thread snapshot: app loop did not respond within %.1fs",
                self._HEALTHCHECK_ANSWER_DEADLINE_S,
            )
            await self._send_ws_json(ws, fallback)

            async def _refresh_from_late_answer() -> None:
                try:
                    late = await handler_future
                except Exception:  # noqa: BLE001
                    return
                if isinstance(late, dict) and late.get("status") == "ok" and isinstance(late.get("payload"), dict):
                    self.set_healthcheck_snapshot(late["payload"])

            await _refresh_from_late_answer()
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
