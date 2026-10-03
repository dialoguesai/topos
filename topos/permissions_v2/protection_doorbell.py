"""The node rings the control plane when its protection state moves (owner decision 2, 1 Oct 2026).

Every control-plane grant envelope binds the node's `protection_revision` and `node_epoch`, and the control
plane refreshed its copy only when the owner pressed a grant's Sync. So every protection change (an
Off-limits edit, an owner-only mark, an identity attestation, a proof publication or refresh) left every
grant refusing until that click; with the owner's standing iMessage attestation refreshing after each
scheduled sync, that would be every day. The owner decided the control plane re-signs and re-sends the
owner's existing grants on its own, re-sending only policies the owner already authored, never widening.

This is the node's half: a doorbell, nothing more.
- A read-only watcher compares the node's current protection revision (`current_protection_revision`, on
  its own read-only connection, so it only ever sees committed changes from any writer) with the one it
  last rang for, every `INTERVAL_SECONDS`, and once at start.
- On a change it queues one frame for the control plane: `{"id": <unique>, "type": FRAME_TYPE, "payload":
  {}}`. The frame carries nothing: no revision, no count, no owner data. The control plane verifies
  nothing from it; it answers by asking the node's status for each active grant through the signed
  protocol it already uses for the owner's Sync (`permissions_v2_status`), and the node's status runs its
  own protection sync before it signs. So a forged frame can only cause status requests.
- Those requests carry the control plane's relay stamp as the owner's app with the client id
  `AUTO_RESYNC_CLIENT`. The node answers that client a status only (`core.handlers.permissions_v2`): it can
  never mutate a grant.

`TOPOS_PERMISSIONS_V2_AUTO_RESYNC=off` keeps the watcher from starting.

One watcher per process. It starts 60 s after the node starts when the node is already bound, or at once when the
node binds itself while it runs (A2A-1 §4.2 step 16, ``start_after_bind``). Whichever gets there first watches;
the other finds it running and stops there, so the startup path never starts a second copy after a bind.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import switches
from .canonical import PolicyError

FRAME_TYPE = "permissions_v2_protection_changed"
#: The relay stamp's client id on the control plane's automatic status requests (status only on the node).
AUTO_RESYNC_CLIENT = "permissions_v2_auto_resync"
INTERVAL_SECONDS = 10.0
_log = logging.getLogger(__name__)


def enabled() -> bool:
    return switches.on(switches.AUTO_RESYNC)


def frame() -> dict:
    return {"id": "permissions-v2-protection-" + secrets.token_hex(16), "type": FRAME_TYPE, "payload": {}}


def read_revision(canonical_database, owner_id: str) -> str:
    """The node's current protection revision, read on its own read-only connection (committed state only)."""
    from .protection_clock import current_protection_revision
    conn = sqlite3.connect(Path(canonical_database).as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("BEGIN")
        return current_protection_revision(conn, owner_id=owner_id)
    finally:
        conn.close()


def send_to_control_plane(message: dict) -> bool:
    """Queue the frame on the node's control-plane connection; False when there is none."""
    from ..core import state as engine_state
    client = getattr(engine_state, "control_plane_client", None)
    enqueue = getattr(client, "enqueue_unsolicited_message_threadsafe", None)
    if not callable(enqueue):
        return False
    enqueue(message)
    return True


class ProtectionDoorbell:
    """Rings once per protection revision it has not rung for. Never raises."""

    def __init__(self, *, read: Callable[[], str], send: Callable[[dict], bool]):
        self._read, self._send = read, send
        self._rung: Optional[str] = None

    def check(self) -> bool:
        try:
            revision = self._read()
        except (PolicyError, sqlite3.Error, OSError) as exc:
            _log.warning("protection doorbell could not read the revision (%s)", getattr(exc, "code", type(exc).__name__))
            return False
        if revision == self._rung:
            return False
        try:
            sent = bool(self._send(frame()))
        except Exception as exc:  # noqa: BLE001 -- the next check rings again
            _log.warning("protection doorbell could not ring (%s)", type(exc).__name__)
            return False
        if sent:
            self._rung = revision
        return sent


_watching = threading.Lock()      # guards the two below
_watcher: Optional[threading.Thread] = None
_generation = 0                   # moved by stop(): a start begun before a stop never watches after it
_stop = threading.Event()


def _claim(generation: int) -> bool:
    """Become this process's one watcher, unless another thread already is (or a stop came since the start)."""
    global _watcher
    with _watching:
        if _watcher is not None or generation != _generation:
            return False
        _watcher = threading.current_thread()
        return True


def running() -> bool:
    """Whether this process has its watcher."""
    with _watching:
        return _watcher is not None


def stop(timeout: float = 5.0) -> None:
    """End the watcher and let a later start begin again. A node never needs it (the thread is a daemon and
    dies with the process); tests that bind a node in-process do."""
    global _watcher, _generation
    with _watching:
        _generation += 1
        watcher = _watcher
    _stop.set()
    if watcher is not None and watcher is not threading.current_thread():
        watcher.join(timeout)
    with _watching:
        _watcher = None
    _stop.clear()


def start_at_startup(*, delay: float = 60.0, interval: float = INTERVAL_SECONDS) -> bool:
    """App startup: the watcher on a daemon thread, when the permissions beta is configured. A no-op otherwise.

    It only reads, so a daemon thread that dies with the process leaves nothing half-written."""
    if not enabled():
        _log.info("protection doorbell off (TOPOS_PERMISSIONS_V2_AUTO_RESYNC)")
        return False
    with _watching:
        generation = _generation

    def run():
        if delay:
            time.sleep(delay)
        try:
            from .runtime import get_runtime
            runtime = get_runtime()
            database, owner_id = runtime.protocol.canonical_database, runtime.protocol.ledger.identity.owner_id
        except PolicyError as exc:
            _log.info("protection doorbell not started: %s", exc.code)
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning("protection doorbell not started (%s)", type(exc).__name__)
            return
        if not _claim(generation):
            _log.debug("protection doorbell already running in this process")
            return
        bell = ProtectionDoorbell(read=lambda: read_revision(database, owner_id), send=send_to_control_plane)
        while not _stop.is_set():
            bell.check()
            _stop.wait(interval)

    threading.Thread(target=run, name="permissions-v2-protection-doorbell", daemon=True).start()
    return True


def start_after_bind(*, interval: float = INTERVAL_SECONDS) -> bool:
    """A bind just made this node bound (A2A-1 §4.2 step 16): watch now, with no start-up delay and no restart.
    Nothing new when this process already watches."""
    if running():
        return False
    return start_at_startup(delay=0, interval=interval)
