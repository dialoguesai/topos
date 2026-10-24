"""Kept questions Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §C): whether this node has caught up, as counts.

When the owner's Topos comes back online it rebuilds each share's search index (about 13 minutes for one index on the
owner's node after a restart), catches up on the owner's data, and answers waiting questions, all at once. The
control plane paces the delivery of kept questions by what this module reports (§F); until now the node said nothing
about it. The object (``build``) is sent on every heartbeat and register and answered by ``get_sharing_readiness``,
to the control plane only (DR-37): never an asker, never a share's status.

**Served now, per grant.** A grant counts as ready only while its search index has been proven usable by the code
that already decides it, never because a file is on disk, and every grant starts unserved at process start:

- served: a build or a re-stamp publishes its index, the sweep publishes a narrowed copy, the daemon sweep finds the
  index current, or the request path's own check (``SearchIndexService.check_own``) passes;
- not served: the index file is shredded (a sweep or a check found it stale, the share ended, a protection change
  dropped every index), the sweep withholds members from it until its narrowed copy publishes, or the request path's
  check refuses it. A verdict about a file counts only while that file is still the one checked, so a check of an
  older file never overrides a newer publish;
- which grants should have one (``total``): the active grants of a releasable search capability, as the daemon sweep
  and the start-up restore read them from the ledger every round anyway; a publish, or a build that did not publish,
  names its grant too, a removal drops it. Until the first such read the node does not know, and with search on it reports itself catching up.

Readiness never calls ``check_own`` and never reads SQLite or takes the write gate: ``build`` reads this module's
memory, the owner's away settings and the answer gate's owner count, all in memory.

**Catching up** is any of: ``ready < total`` (or the grants not yet known, with search on); the refresh loop's
``startup_backlog`` pass running; an embeddings or AI-tool catch-up job queued or running; ``pipeline_backlog`` (jobs
queued across the three pipeline lanes, as each lane's own claim last counted them) at or above
``CATCHING_UP_BACKLOG``. The counts are what the code already passes through; nothing is queried to build them.

Every name here is a count, a code or one of the node's own grant ids; nothing about who asked what.
"""
from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

VERSION = 1
#: ``permissions_v2_sharing_readiness`` in the node's capabilities.
CAPABILITY_VERSION = 1
#: At most this many grant ids in ``unserved_grants`` (``unserved_truncated`` says when more were left out).
UNSERVED_MAX = 64
#: ``catching_up`` while at least this many jobs are queued across the three pipeline lanes. In steady state each
#: lane holds a few at most (an hourly sync per source and its follow-up derive and topic jobs; a catch-up batch queues
#: the next only when it ends); after a restart or a long absence the inbox deliveries, the syncs that came due and
#: their derive jobs pile up (one node on 22 Oct 2026: about 26 derive jobs ahead of one catch-up batch for hours).
#: Ten is more than one cycle of steady-state work, and less than the measured backlog.
CATCHING_UP_BACKLOG = 10
#: ``owner_idle_seconds`` is at most this.
IDLE_CAP_SECONDS = 3600
#: The pipeline kinds whose queued or running jobs mean the node is catching up on the owner's own data.
CATCHUP_JOB_KINDS = frozenset({"local_sync_embeddings_catchup", "imported_ai_chat_embeddings_catchup",
                               "ai_tool_enrichment_catchup"})

_lock = threading.Lock()
_started_at = int(time.time())       # the node process's start (when its code loaded), epoch seconds
_started_monotonic = time.monotonic()
_root: str | None = None             # the index folder the verdicts are about
_expected: dict | None = None        # index file name -> grant id, for every grant that should have an index
_served: set = set()                 # index file names proven served
_backlog_pass = False                # the refresh loop's startup_backlog pass is running
_queued: dict = {}                   # pipeline lane -> {kind: jobs queued}, as its last claim counted them
_running: dict = {}                  # pipeline kind -> jobs running now


def _known_kinds() -> frozenset:
    """The catch-up kinds by their modules' own names, so a renamed kind is not silently missed."""
    try:
        from ..ingestion.ai_tool_catchup import KIND as AI_TOOL
        from ..ingestion.embeddings_catchup import CATCHUP_KINDS as EMBEDDINGS
        return frozenset((*EMBEDDINGS, AI_TOOL))
    except Exception:  # noqa: BLE001 -- the names written above
        return CATCHUP_JOB_KINDS


def _name(grant_id: str) -> str:
    from .search_index import index_path
    return index_path(Path("."), grant_id).name


def _ours(root) -> bool:
    return _root is not None and str(root) == _root


# -- grants and their indexes ---------------------------------------------------------------------------------------

def note_grants(root, grant_ids) -> None:
    """Every grant that should have an index under ``root`` now (a ledger read the caller made anyway). A new root
    starts afresh: nothing there is served until proven."""
    global _root, _expected, _served
    names = {_name(grant_id): grant_id for grant_id in grant_ids}
    with _lock:
        if not _ours(root):
            _root, _served = str(root), set()
        _expected = names
        _served &= set(names)


def note_served(root, grant_id: str, *, still: Callable[[], bool] | None = None) -> None:
    """``grant_id``'s index under ``root`` serves now (a publish or a passing check). ``still``, asked under this
    module's lock: whether the file proven is still the one there (False: the verdict is about an older file)."""
    global _root
    name = _name(grant_id)
    with _lock:
        if _root is None:
            _root = str(root)            # a publish before the first ledger read: kept for when the grants are known
        if not _ours(root) or (still is not None and not _still(still)):
            return
        if _expected is not None:
            _expected[name] = grant_id   # a publish or a passing check is of an active grant's index
        _served.add(name)


def note_unserved(root, grant_id: str | None = None, *, name: str | None = None,
                  still: Callable[[], bool] | None = None) -> None:
    """The index (by grant, or by file name) does not serve now. ``still`` as in ``note_served``."""
    name = name if name is not None else _name(grant_id)
    with _lock:
        if not _ours(root) or (still is not None and not _still(still)):
            return
        _served.discard(name)


def note_owed(root, grant_id: str) -> None:
    """A build of ``grant_id`` (an active share) did not publish: it should have an index, served or not."""
    name = _name(grant_id)
    with _lock:
        if _ours(root) and _expected is not None:
            _expected[name] = grant_id


def note_ended(root, grant_id: str) -> None:
    """The grant is no longer one that should have an index (it ended, or is not a releasable search)."""
    name = _name(grant_id)
    with _lock:
        if not _ours(root):
            return
        _served.discard(name)
        if _expected is not None:
            _expected.pop(name, None)


def note_none_served(root) -> None:
    """Every index under ``root`` went (a sweep that could not run purged them all)."""
    with _lock:
        if _ours(root):
            _served.clear()


def _still(still: Callable[[], bool]) -> bool:
    try:
        return still() is True
    except Exception:  # noqa: BLE001 -- what cannot be told is not served
        return False


# -- catch-up counters ----------------------------------------------------------------------------------------------

def backlog_pass(running: bool) -> None:
    """The refresh loop's ``startup_backlog`` pass started (True) or ended (False)."""
    global _backlog_pass
    with _lock:
        _backlog_pass = bool(running)


def note_queued(lane: str, counts: Mapping[str, int]) -> None:
    """What ``lane``'s claim just counted queued, by kind."""
    with _lock:
        _queued[lane] = {str(kind): int(number) for kind, number in counts.items() if int(number) > 0}


@contextlib.contextmanager
def job_running(kind: str):
    """A pipeline job of ``kind`` runs inside."""
    kind = str(kind)
    with _lock:
        _running[kind] = _running.get(kind, 0) + 1
    try:
        yield
    finally:
        with _lock:
            _running[kind] = max(0, _running.get(kind, 0) - 1)


# -- the object -----------------------------------------------------------------------------------------------------

def _search_on() -> bool:
    try:
        from . import switches
        return switches.on(switches.MESSAGE_SEARCH)
    except Exception:  # noqa: BLE001 -- unknown: as if it may serve
        return True


def _owner_idle_seconds():
    """``owner_idle_seconds`` (contract §C): None unless the owner's "Answer others only when …" is on and the client
    classifies the owner's frames."""
    try:
        from . import answer_gate, away_config
        if not away_config.others_when_idle() or not answer_gate.counts_owner_frames():
            return None
        return answer_gate.owner_idle_seconds(since=_started_monotonic, cap=IDLE_CAP_SECONDS)
    except Exception:  # noqa: BLE001 -- nothing said about the owner when it cannot be read
        return None


def build() -> dict:
    """The readiness object (contract §C), every field present. Memory only."""
    kinds = _known_kinds()
    with _lock:
        expected = None if _expected is None else dict(_expected)
        served = set(_served)
        backlog_running = _backlog_pass
        queued = {kind: number for lane in _queued.values() for kind, number in lane.items()}
        running = {kind: number for kind, number in _running.items() if number > 0}
    known = expected if expected is not None else {}
    ready = sum(1 for name in known if name in served)
    unserved = sorted(grant_id for name, grant_id in known.items() if name not in served)
    backlog = sum(queued.values())
    catching_up = (ready < len(known) or (expected is None and _search_on()) or backlog_running
                   or any(queued.get(kind) or running.get(kind) for kind in kinds)
                   or backlog >= CATCHING_UP_BACKLOG)
    return {"v": VERSION, "started_at": _started_at,
            "share_indexes": {"ready": ready, "total": len(known)},
            "unserved_grants": unserved[:UNSERVED_MAX], "unserved_truncated": len(unserved) > UNSERVED_MAX,
            "pipeline_backlog": backlog, "catching_up": bool(catching_up),
            "owner_idle_seconds": _owner_idle_seconds()}


def reset() -> None:
    """Forget everything (tests; a node never needs it)."""
    global _root, _expected, _served, _backlog_pass
    with _lock:
        _root, _expected, _served, _backlog_pass = None, None, set(), False
        _queued.clear()
        _running.clear()
