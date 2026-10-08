"""N3: the index work an owner change asks for runs after the change is answered, off the write gate, one share at a time.

Why. Until N3 an applied grant change (activate, change, revoke) swept and rebuilt EVERY share's index while it held the
node's write gate, after its acknowledgement had already been signed with a 120 s life; an owner review change did the
same. With one owner sharing with many people the acknowledgement could expire before it was returned, and every writer
on the node, ingestion included, waited for the whole rebuild.

What changed. The change is applied and acknowledged exactly as before, under the gate, with the same checks in the
same order and the same signed bytes. Still under the gate, it only asks this queue for the index work and returns:
for a mutation a few list operations; for a review change one ledger read of the grants and their question counts, after
the drop described below. Nothing is built there. One thread per runtime then does the work, one share at a time,
through ``SearchIndexService.rebuild``, which takes the gate only for its two brief steps (freeze the owner's decisions;
re-check and publish) and builds on an ungated read snapshot in between. The acknowledgement therefore leaves within the
change's own gate hold, and a writer waits at most for one of those brief steps, never for a build.

What each change asks for.

- An applied grant mutation: that grant only. Nothing another share's index is judged by moves: an index basis binds its
  own grant's generations and policy hash, the protection revision and clock, the Off-limits closure and, for a direct
  grant, the review digest (``search_index.basis_of`` and ``_current``), never the node epoch a mutation advances. Ending
  a share drops its index and rotates its record-id key: ``rebuild`` of an inactive grant forgets it. A change of only
  the fields no build reads (the daily number; A2A-4's answers mode once the grammar carries it) re-stamps the share's
  index basis instead of rebuilding it (``SearchIndexService.restamp``, A2A-4 Q5); anything else is a full build.
- An owner review change (a review recorded or revoked, a deselection or its undo): every search grant, most-read first.
  A direct grant's basis (p2c-v2, p2c-v3) binds the node-wide review digest, which every review change moves, so the
  guard itself makes every direct index stale; which p2c-v1 permitted sets a change touches cannot be decided without
  qualifying the fact under every grant. So all of them, off the gate and one at a time. The one index the guard cannot
  see a review change in, p2c-v1's, is dropped first, in the change's own critical section
  (``search_index.drop_unguarded``).

Correctness. The request path is unchanged: a search is served from an index only while its basis is exactly valid
(``check_own``, ``load``, then the gated member re-check), else refused uniformly. So while a share's index is being
rebuilt, a search of that share is refused, unless its previous index is still exactly valid (a share the change did not
touch). Nothing here decides what a recipient receives.

Order. A request for every grant is built most-read first: questions over the days the ledger keeps
(``PolicyLedger.question_counts``), ties by grant id, so the shares people use come back first. A grant already queued is
not queued twice; a grant asked for while its own build runs is built once more after it, since that build may have
frozen the state before the change. Builds take ``search_index.BUILD_SLOT``, which the refresh loop's restore takes too,
so the node's two automatic rebuilders never build at once and the later change's build always publishes last.

BL-148 (1.5.1). A share's mode or scope change is a new policy and a full build (the control plane signs a new
`validity.starts_at` with each change, so it is never a light change): the index the old policy was built under is
refused at once and dropped by the next sweep, and the share is dark until this queue publishes the new one. When that
build did not publish (it ended `stale` because something it is judged by moved while it ran, or it failed), the share
waited for the refresh loop's restore, which runs passes at least ``min_interval`` (300 s) apart: measured live, a
share was empty for about seven minutes after a mode change. Now:

- a build an owner change asked for that does not publish is tried again by this queue, promptly and a bounded number
  of times (``RETRY_DELAYS``), and stays owed meanwhile, so the restore leaves it to the queue. Each try is a whole
  ``rebuild`` with every check it makes; nothing is published that the guard would refuse, and the old index is never
  served under the new policy (its basis binds the old policy hash). When the tries are spent, the restore takes over,
  as before;
- what is owed (queued, running, or waiting for its next try) is kept on disk beside the indexes (``OWED_FILE``), so a
  node quit inside the window asks for those builds again at its next start (``request_owed``, from the start-up
  restore), whether or not the old index file is still there.

The queue itself lives in memory. At start the node also asks it for every active search share that has no index at all
(``request_missing``, from ``refresh_loop.start_at_startup``), the one case the restore never sees because nothing was
ever published. Logs carry counts and class names only, never a grant id, record or reason text.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable

from .canonical import PolicyError
from .opaque_ids import private_file
from .search_contract import RELEASABLE_SEARCH_CAPABILITIES

_log = logging.getLogger(__name__)

#: BL-148: what the queue owes, kept beside the indexes so a restart asks for it again. Grant ids only.
OWED_FILE = "index-rebuilds-owed.json"
#: The states a build ends in that leave nothing more to do for it.
SETTLED = frozenset({"ready", "over_cap", "removed", "restamped"})


def most_read_first(grant_ids: Iterable[str], counts: dict) -> list[str]:
    """`grant_ids` by questions asked over the retained days, most first; ties (no questions included) by grant id."""
    return sorted(dict.fromkeys(grant_ids), key=lambda grant_id: (-int(counts.get(grant_id, 0)), grant_id))


class IndexRebuilds:
    """One per runtime. ``request`` is cheap and may be called under the write gate; the builds run on its thread."""

    #: BL-148: seconds before each further try of a build that did not publish (``stale`` or ``failed``).
    RETRY_DELAYS: tuple = (5.0, 20.0, 60.0)

    def __init__(self, *, ledger, root: Path, index: Callable[[], object],
                 sync_protection: Callable[[], bool] | None = None, clock: Callable[[], float] = time.time):
        self.ledger = ledger
        self.owner_id = ledger.identity.owner_id
        self.root = Path(root)
        self._index = index                      # the runtime's current SearchIndexService, asked at build time
        self._sync_protection = sync_protection  # refresh_loop.protection_sync(protocol) on a node
        self.clock = clock
        self._cond = threading.Condition()
        self._queue: list[str] = []
        self._again: set[str] = set()
        self._running: str | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._tries: dict[str, int] = {}         # BL-148: further tries taken of a build that did not publish
        self._not_before: dict[str, float] = {}  # BL-148: a queued try's earliest start (time.monotonic)
        self._persisted: list[str] | None = None
        # The last builds: (state, member count, seconds). Content-free, for diagnosis and the measurement.
        self.results: deque = deque(maxlen=64)

    # -- asking ---------------------------------------------------------------

    def search_grants(self, now: int) -> list[str]:
        """Every grant a review change can touch, most-read first: each active search grant, and each inactive grant
        that still has an index file here (its build forgets it, as ``rebuild_all`` did)."""
        from .search_index import index_path
        found = []
        with self.ledger._transaction() as db:
            for row in db.execute("SELECT grant_id FROM p2a_grants").fetchall():
                grant_id = row["grant_id"]
                try:
                    _authority, policy = self.ledger._authority(db, grant_id, now)
                except PolicyError:
                    if index_path(self.root, grant_id).exists():
                        found.append(grant_id)
                    continue
                if policy.versions.capability in RELEASABLE_SEARCH_CAPABILITIES:
                    found.append(grant_id)
        return most_read_first(found, self.ledger.question_counts(now=now))

    def request(self, grant_ids: Iterable[str] | None = None) -> list[str]:
        """Queue a rebuild of each grant in `grant_ids`, in that order, or of every search grant (``search_grants``)
        when None. Returns the grants asked for. Starts the thread on first use; never builds on the caller's thread."""
        grant_ids = self.search_grants(int(self.clock())) if grant_ids is None else list(dict.fromkeys(grant_ids))
        with self._cond:
            if self._closed:
                return []
            for grant_id in grant_ids:
                # A new ask is a new change: built as soon as its turn comes, with its tries afresh.
                self._tries.pop(grant_id, None)
                self._not_before.pop(grant_id, None)
                if grant_id == self._running:
                    self._again.add(grant_id)
                elif grant_id not in self._queue:
                    self._queue.append(grant_id)
            if self._queue and self._thread is None:
                self._thread = threading.Thread(target=self._run, name="p2c-index-rebuilds", daemon=True)
                self._thread.start()
            self._persist_owed()
            self._cond.notify_all()
        return grant_ids

    def request_owed(self) -> list[str]:
        """BL-148: at start, every build a previous run of the node owed (``OWED_FILE``) and did not finish, for each
        grant that is still an active search grant, whether or not an older index file is still there. Returns the
        grants asked for. An unreadable file asks for nothing (the start-up restore's own checks still run)."""
        try:
            owed = json.loads((self.root / OWED_FILE).read_text("utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(owed, list):
            return []
        wanted = set(grant for grant in owed if isinstance(grant, str))
        if not wanted:
            return []
        now = int(self.clock())
        active = []
        with self.ledger._transaction() as db:
            for grant_id in sorted(wanted):
                try:
                    _authority, policy = self.ledger._authority(db, grant_id, now)
                except PolicyError:
                    continue
                if policy.versions.capability in RELEASABLE_SEARCH_CAPABILITIES:
                    active.append(grant_id)
        with self._cond:
            fresh = [grant_id for grant_id in active if grant_id not in self._queue and grant_id != self._running]
        if not fresh:
            return []
        return self.request(most_read_first(fresh, self.ledger.question_counts(now=now)))

    def request_missing(self) -> list[str]:
        """Every active search grant with no index file here, most-read first: after a start, the builds a restart may
        have lost between an owner change's acknowledgement and its build. Returns the grants asked for."""
        from .search_index import index_path
        now = int(self.clock())
        missing = []
        with self.ledger._transaction() as db:
            for row in db.execute("SELECT grant_id FROM p2a_grants").fetchall():
                try:
                    _authority, policy = self.ledger._authority(db, row["grant_id"], now)
                except PolicyError:
                    continue
                if (policy.versions.capability in RELEASABLE_SEARCH_CAPABILITIES
                        and not index_path(self.root, row["grant_id"]).exists()):
                    missing.append(row["grant_id"])
        if not missing:
            return []
        return self.request(most_read_first(missing, self.ledger.question_counts(now=now)))

    def owed(self) -> frozenset:
        """The grants queued (a further try waiting included) or being built now."""
        with self._cond:
            return frozenset(self._queue) | (frozenset({self._running}) if self._running is not None else frozenset())

    def _persist_owed(self) -> None:
        """Keep ``owed`` on disk (BL-148). Called with the condition held; only when it changed. Never raises: a file
        that cannot be written costs the restart case only, and the start-up restore's own checks still run."""
        owed = sorted(set(self._queue) | ({self._running} if self._running is not None else set()))
        if owed == self._persisted:
            return
        path = self.root / OWED_FILE
        temporary = path.with_name("." + OWED_FILE + "." + os.urandom(6).hex())
        try:
            if not self.root.is_dir():
                return
            private_file(temporary)
            temporary.write_text(json.dumps(owed), "utf-8")
            os.replace(temporary, path)
            self._persisted = owed
        except Exception as exc:  # noqa: BLE001 -- class name only
            temporary.unlink(missing_ok=True)
            _log.warning("owed index builds not kept (%s)", type(exc).__name__)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Until nothing is queued or running (True), or `timeout` seconds (False)."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and self._running is None, timeout)

    def close(self) -> None:
        """No further builds start; one running finishes on its own (daemon thread)."""
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._again.clear()
            self._not_before.clear()
            self._cond.notify_all()

    # -- building -------------------------------------------------------------

    def _next(self) -> str | None:
        """The first queued grant whose try is due, waiting for one (condition held); None once closed."""
        while True:
            if self._closed:
                return None
            now = time.monotonic()
            for grant_id in self._queue:
                if self._not_before.get(grant_id, 0.0) <= now:
                    self._queue.remove(grant_id)
                    self._not_before.pop(grant_id, None)
                    return grant_id
            waits = [self._not_before.get(grant_id, 0.0) - now for grant_id in self._queue]
            self._cond.wait(max(0.0, min(waits)) if waits else None)

    def _run(self) -> None:
        while True:
            with self._cond:
                grant_id = self._next()
                if grant_id is None:
                    return
                self._running = grant_id
            result = self._build(grant_id)
            with self._cond:
                self._running = None
                if grant_id in self._again:
                    self._again.discard(grant_id)
                    if not self._closed and grant_id not in self._queue:
                        self._queue.append(grant_id)
                elif result[0] in SETTLED:
                    self._tries.pop(grant_id, None)
                elif not self._closed and grant_id not in self._queue:
                    # BL-148: an owner change asked for this build and it did not publish. Try again soon, a bounded
                    # number of times, and stay owed meanwhile; then the refresh loop's restore takes over.
                    tries = self._tries.get(grant_id, 0)
                    if tries < len(self.RETRY_DELAYS):
                        self._tries[grant_id] = tries + 1
                        self._not_before[grant_id] = time.monotonic() + self.RETRY_DELAYS[tries]
                        self._queue.append(grant_id)
                    else:
                        self._tries.pop(grant_id, None)
                self.results.append(result)
                self._persist_owed()
                self._cond.notify_all()

    def _build(self, grant_id: str) -> tuple:
        """One grant: the protection sync recipient admission makes, then a re-stamp when the change was only of the
        fields no build reads, else the owner hooks' own rebuild, as the node's own process for its owner. A failed
        build leaves no index for that grant, as ``rebuild_all`` did."""
        from .refresh_loop import node_principal
        from .search_index import BUILD_SLOT, purge
        started = time.monotonic()
        state, count = "failed", 0
        with BUILD_SLOT:
            if self._sync_protection is not None:
                try:
                    self._sync_protection()
                except Exception as exc:  # noqa: BLE001 -- as the restore: the build then decides (`stale` if moved)
                    _log.warning("protection sync before an owner-change rebuild failed (%s)", type(exc).__name__)
            try:
                service = self._index()
                now = int(self.clock())
                with node_principal(self.owner_id):
                    built = self._restamp(service, grant_id, now)
                    if built is not None:
                        state = "restamped"
                    else:
                        built = service.rebuild(grant_id, now=now)
                        state = built["state"]
                count = built["member_count"]
            except Exception as exc:  # noqa: BLE001 -- class name only; never a grant id or content
                _log.warning("search index rebuild after an owner change failed (%s)", type(exc).__name__)
                try:
                    purge(self.root, grant_id)
                except Exception:  # noqa: BLE001
                    pass
        return state, count, time.monotonic() - started

    @staticmethod
    def _restamp(service, grant_id: str, now: int):
        """The re-stamp a change of only the light fields allows (A2A-4 Q5), or None: then a build. A re-stamp that
        cannot run is a build too, never a dropped index."""
        restamp = getattr(service, "restamp", None)
        if restamp is None:
            return None
        try:
            return restamp(grant_id, now=now)
        except Exception as exc:  # noqa: BLE001 -- class name only
            _log.warning("search index re-stamp failed; building instead (%s)", type(exc).__name__)
            return None
