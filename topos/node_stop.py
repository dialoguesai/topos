"""What a stop does the moment it is asked for (BL-168).

A node told to stop used to do almost nothing until uvicorn reached the app's
lifespan shutdown, and uvicorn reaches it only after every in-flight request
has finished: with no ``timeout_graceful_shutdown`` that wait has no bound. On
10 Oct 2026 (1.5.3) the port closed at the SIGTERM, then the node ran on for 14
minutes: its pipeline worker kept running the job it held, nothing stopped the
permissions runtime's background threads (the catch-up assessment pass, the
share refresh loop, the index rebuild queue), and the control-plane client,
which ``shutdown_event`` stops last of all, stayed connected and kept taking
relayed asks it could no longer answer ("Dropping inbound request due to
saturation", ~150). It exited when that work happened to end.

Now a stop has three parts:

1. **At the signal** (``begin_stop``, a ``runtime_shutdown`` listener; the
   tray's Quit calls it too). It only schedules work and starts a timer, as a
   signal handler must. On the app loop, in this order (``stop_now``):
   close the control-plane client, so the control plane stops relaying asks
   to a node that is going away and answers the asker at once; stop any graph
   rebuild child (what a held job may be waiting on); park the
   ingestion queue (``job_runner.park_pipeline_worker``: the job a worker
   holds goes back to the queue, and no worker claims or starts again in
   this run); stop the permissions runtime's background work
   (``runtime.stop_background_work``: catch-up passes, refresh loop, rebuild
   queue, answer jobs; assessments are durable checkpoints and a pass a stop
   cuts short is planned again at the next start). Then the app's own
   background tasks.
2. **The drain is bounded.** uvicorn waits ``drain_seconds()`` for in-flight
   requests and then cancels them (``uvicorn_options``), so lifespan shutdown
   always runs; ``shutdown_event`` runs ``stop_now`` again first (it is the
   same task when the signal already started it).
3. **The process is bounded.** Cancelling a coroutine never stops the thread it
   awaits, and on Python 3.10 ``asyncio.run`` and the interpreter join every
   executor thread with no timeout. ``exit_deadline_seconds()`` after the stop
   began, a process still alive logs the threads it was waiting on (names only)
   and exits. SQLite rolls an interrupted transaction back at the next open; a
   claimed job whose requeue never landed is recovered at the next start
   (``recover_pipeline_jobs``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("topos.node_stop")

#: How long uvicorn lets in-flight requests finish before it cancels them.
DEFAULT_DRAIN_SECONDS = 10.0
#: From the stop request to a forced exit, if the process is still alive then.
DEFAULT_EXIT_DEADLINE_SECONDS = 60.0
#: The control-plane client's own stop waits up to 10 s for its close handshake
#: and 10 s for its thread; one bound over both.
CONTROL_PLANE_STOP_SECONDS = 12.0
#: Parking hands the held job back under the write gate (bounded to 2 s inside).
PARK_SECONDS = 5.0
#: The permissions runtime's stops only set events; the bound is for the gate.
PERMISSIONS_STOP_SECONDS = 5.0
BACKGROUND_REAP_SECONDS = 5.0


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


def drain_seconds() -> float:
    """uvicorn's ``timeout_graceful_shutdown``: ``TOPOS_SHUTDOWN_DRAIN_SECONDS``, default 10."""
    return _env_seconds("TOPOS_SHUTDOWN_DRAIN_SECONDS", DEFAULT_DRAIN_SECONDS)


def exit_deadline_seconds() -> float:
    """``TOPOS_SHUTDOWN_EXIT_DEADLINE_SECONDS``, default 60; 0 turns the forced exit off."""
    return _env_seconds("TOPOS_SHUTDOWN_EXIT_DEADLINE_SECONDS", DEFAULT_EXIT_DEADLINE_SECONDS)


def uvicorn_options() -> dict:
    """Keyword arguments every node uvicorn server is built with (CLI and tray)."""
    return {"timeout_graceful_shutdown": max(1, int(round(drain_seconds())))}


_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_task: Optional[asyncio.Task] = None
_begun = False


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """App startup: the loop ``begin_stop`` schedules on. A new run starts with no stop begun."""
    global _loop, _task, _begun
    with _lock:
        _loop = loop
        _task = None
        _begun = False


def begin_stop(reason: str = "signal") -> bool:
    """Start the stop now. Safe inside a signal handler and from any thread.

    Blocks on no lock the interrupted main thread could be holding: this
    module's ``_lock`` is only tried (a busy lock means a stop is being begun or
    joined already); ``current_generation`` reads without a lock; the
    generation's own lock is reentrant; ``Event.set`` and
    ``call_soon_threadsafe`` take only locks no main-thread code here holds
    across a call out. Idempotent per run. Returns True when this call began it."""
    global _begun
    from .runtime_shutdown import current_generation

    if not _lock.acquire(blocking=False):
        return False
    try:
        if _begun:
            return False
        _begun = True
        loop = _loop
    finally:
        _lock.release()
    generation = current_generation()
    if not generation.is_stop_requested():
        # The tray's Quit is no signal; the run is stopping all the same.
        generation.request_stop(reason)
    _arm_exit_deadline()
    if loop is not None and not loop.is_closed():
        try:
            loop.call_soon_threadsafe(_spawn_on_loop, generation)
        except RuntimeError:  # the loop closed between the check and the call
            pass
    return True


def _spawn_on_loop(generation: Any) -> None:
    global _task
    with _lock:
        if _task is None:
            _task = asyncio.get_running_loop().create_task(_stop_steps(generation), name="node-stop")


async def stop_now(generation: Any = None) -> None:
    """Run the stop's steps once per run (``shutdown_event``; the signal's task when it already began)."""
    global _task, _begun
    from .runtime_shutdown import current_generation

    with _lock:
        _begun = True
        task = _task
        if task is None or task.get_loop() is not asyncio.get_running_loop():
            task = asyncio.get_running_loop().create_task(
                _stop_steps(generation if generation is not None else current_generation()), name="node-stop"
            )
            _task = task
    await asyncio.shield(task)


async def _bounded(name: str, step: Callable[[], Any], seconds: float) -> None:
    started = time.monotonic()
    try:
        await asyncio.wait_for(step(), timeout=seconds)
    except asyncio.TimeoutError:
        logger.warning("node stop: %s did not finish within %.0fs; going on", name, seconds)
    except Exception as exc:  # noqa: BLE001 — a stop step never stops the stop
        logger.warning("node stop: %s failed (%s)", name, type(exc).__name__)
    else:
        logger.info("node stop: %s done in %.1fs", name, time.monotonic() - started)


async def _stop_steps(generation: Any) -> None:
    from .core import state
    from .pipeline.job_runner import park_pipeline_worker

    client = getattr(state, "control_plane_client", None)
    if client is not None:
        await _bounded("control-plane client", client.stop, CONTROL_PLANE_STOP_SECONDS)
    # Before the tasks and workers it unblocks: cancelling a task never stops the thread it awaits, and a job
    # waiting on a rebuild child waits until the child dies (the signal listener has usually stopped it already).
    await _bounded("graph rebuild children", _stop_rebuild_children, REBUILD_STOP_SECONDS)
    await _bounded("ingestion queue", lambda: park_pipeline_worker(generation), PARK_SECONDS)

    def _permissions() -> Any:
        from .permissions_v2.runtime import stop_background_work

        return asyncio.to_thread(stop_background_work)

    await _bounded("permissions background work", _permissions, PERMISSIONS_STOP_SECONDS)
    reap = _background_reaper
    if reap is not None:
        await _bounded("background tasks", reap, BACKGROUND_REAP_SECONDS)


REBUILD_STOP_SECONDS = 5.0


def _stop_rebuild_children() -> Any:
    from .features.entities import rebuild_subprocess

    return asyncio.to_thread(rebuild_subprocess.stop_rebuild_children, 2.0)


#: ``app._reap_background_tasks``, set at startup (this module never imports the app).
_background_reaper: Optional[Callable[[], Any]] = None


def set_background_reaper(reap: Optional[Callable[[], Any]]) -> None:
    global _background_reaper
    _background_reaper = reap


_stop_begun = threading.Event()
_deadline_thread: Optional[threading.Thread] = None


def enable_exit_deadline() -> bool:
    """Serving entry points only (``topos-node`` and its tray), before uvicorn starts: arm the forced exit.

    The timer thread starts here, not at the stop: starting a thread inside a signal handler can deadlock on
    ``threading``'s own lock when the signal lands while the main thread is starting one (asyncio's executor
    does, on the loop thread). A process that never calls this (tests, CLI commands) never force-exits."""
    global _deadline_thread
    seconds = exit_deadline_seconds()
    if seconds <= 0:
        return False
    with _lock:
        if _deadline_thread is not None and _deadline_thread.is_alive():
            return True
        _deadline_thread = threading.Thread(
            target=_exit_after_stop, args=(seconds,), name="topos-exit-deadline", daemon=True
        )
        _deadline_thread.start()
    return True


def _arm_exit_deadline() -> None:
    _stop_begun.set()


def _exit_after_stop(seconds: float) -> None:
    _stop_begun.wait()
    _exit_after(seconds)


def _still_running() -> list[str]:
    me = threading.current_thread()
    return sorted(
        t.name for t in threading.enumerate() if t is not me and t.is_alive() and t is not threading.main_thread()
    )


def _exit_after(seconds: float, *, exit: Callable[[int], Any] = os._exit, sleep: Callable[[float], Any] = time.sleep) -> None:
    sleep(seconds)
    names = _still_running()
    logger.warning(
        "node stop: still running %.0fs after the stop was asked for; exiting now (%d thread(s): %s)",
        seconds,
        len(names),
        ", ".join(names[:16]) or "-",
    )
    for handler in logging.getLogger().handlers + logger.handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass
    exit(0)
