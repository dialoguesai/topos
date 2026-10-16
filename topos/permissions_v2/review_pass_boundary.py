"""NR186: one Off-limits boundary for one automatic-review pass, proven current at every row or built again.

**Why.** `automatic_message_review.prepare` (census-pinned) asks the resolver for the boundary of its read
(`EvidenceResolver.entity_boundary`). Each row is its own read, so each row built a whole `EntityBoundary`: on a copy of
the owner's database about 0.55 s CPU, nearly all of it normalising some 43k names (`_close_identities`), against about
4 ms for the row's own reads with a boundary already built. The worker prepares every row it scans, current or not,
under the write gate, so a 90-day pass was about 55 CPU-minutes and held the gate the owner's chat waits behind.

**What.** Behind `TOPOS_PERMISSIONS_V2_REVIEW_PASS_BOUNDARY` (off, bound and unbound). With it on, the worker's pass
(`AutomaticReviewWorker._process`: the owner's pass and the refresh loop's catch-up slices alike) opens one
`review_pass`; each row's `prepare`, and `publish`'s own `prepare`, runs inside its `row()`. A resolver read opened
inside a row gets the pass's boundary, rebound to that read (`EntityBoundary.rebind`: empty per-record caches), only
when it is proven to be what `EntityBoundary(conn)` would build on that read's snapshot. Otherwise the read builds a
fresh one, exactly as without the switch, and the pass keeps that one instead. Nothing else is touched: every read
outside a row (the worker's page reads, the owner pass's index refresh, every share door) builds its own as before.

**Freshness: the proof at every row.** The pass keeps one template (never handed out, reading no connection), the
canonical token read before the snapshot it was last proven on was established, and a fingerprint of every read its
construction made. For each read inside a row:
1. `opened`, called by `EvidenceResolver._read` before the read's first statement, reads the canonical token (N3a's
   `SearchVerification.canonical_token`: `PRAGMA data_version` on one probe connection kept for the pass, which moves
   at every commit of any other connection, plus the stat state of the database file and its WAL and journal).
2. When the read asks for its boundary, a statement first pins its snapshot, then the token is read again. Equal to
   the template's token: no commit landed between the read that proved the template and this read's snapshot, so
   both read the same rows (the N3a argument; a token never repeats once a commit lands).
3. Otherwise every read of the template's construction is made again on this read's snapshot and compared by its
   fingerprint (`entity_boundary.unchanged_on`): the Off-limits list, the entity spine with its merges, the contacts
   and their identifiers, the protected mentions, and their schema. All equal: a construction on this snapshot would
   read the same and so build the same boundary. The template is then keyed by this read's own token from step 1.
4. Otherwise, or when anything in 2 or 3 cannot be read: a fresh boundary on this read, made by the resolver's own
   factory (`EvidenceResolver._new_entity_boundary`, so the two are never built differently; it refuses as a fresh one
   refuses), and that is the pass's template from now on.
So an Off-limits entry added or removed, an excluded or merged entity, a changed alias, contact or mention, between
two rows, is seen by the next row exactly as a fresh boundary would see it. What the boundary does not read (the
owner's identity binding, exclusions, opt-outs, the row itself and its neighbours) is read by each row's own reads, as
before, and is never kept.

**Scope.** The template lives for one pass on the thread that opened it (one worker run; a catch-up slice is one run),
never in a module global or on the resolver, and never longer than `REVIEW_PASS_BOUNDARY_MAX_AGE_SECONDS` (600 by
default, read as 60 to 3600): an older template is built again at the next read, and one the pass has not used for that
long (no row: the pass waits on the model, on answers or on the gate) is dropped, by a callback on the pass's own
event loop or, failing that, at the next row. Both switches are read at every row: with the boundary switch off, the
row drops the template and every read builds its own, as without it. The row scope is a context variable, so another thread or request never
sees it. A template the pass stops using (built again after a change) has its forms memory (NR175) emptied and
marked ended at once. When the pass ends, on every path, the keeper is closed: its template, token and fingerprints
dropped, the template's forms memory emptied and marked ended, and its probe connection closed. A context copied
inside a row (a task started there) keeps a closed keeper, which hands out nothing and records nothing (DR-19: nothing
derived from the owner's text or the Off-limits list outlives a request or pass).
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time

from . import switches

#: The pass whose row is running on this context (a `ReviewPassBoundary`), set by `row()` and reset on its way out.
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("review_pass_boundary", default=None)
#: The age cap's ceiling (the switch's own floor is 60): an owner may shorten a template's life, never stretch it past
#: an hour.
MAX_AGE_CEILING = 3600


def switched_on() -> bool:
    """Whether the switch is on now; anything unreadable is off (one boundary per read, as without it)."""
    try:
        return switches.on(switches.REVIEW_PASS_BOUNDARY)
    except Exception:  # noqa: BLE001 -- unreadable: off
        return False


def max_age() -> int:
    """The age cap in seconds, 60 to MAX_AGE_CEILING; the default when it cannot be read."""
    try:
        found = switches.number(switches.REVIEW_PASS_BOUNDARY_MAX_AGE)
    except Exception:  # noqa: BLE001 -- unreadable: the default
        found = switches.REVIEW_PASS_BOUNDARY_MAX_AGE.bound
    return max(switches.REVIEW_PASS_BOUNDARY_MAX_AGE.floor, min(int(found), MAX_AGE_CEILING))


def _end(template) -> None:
    """A template the pass no longer uses: its forms memory (NR175) emptied and marked ended, so a copy of it still
    held anywhere computes in full and keeps nothing. Its closure is left as it is: a copy still reading with it must
    withhold exactly as before, never with an emptied vocabulary."""
    from .entity_boundary import _ENDED
    forms = getattr(template, "_forms", None)
    if isinstance(forms, dict):
        forms.clear()
        forms[_ENDED] = True


def _in_transaction(conn) -> bool:
    try:
        return bool(conn.in_transaction)
    except Exception:  # noqa: BLE001 -- not a connection we can reason about: no reuse
        return False


class _Kept:
    """The pass's template: never handed out and reading no connection; the token read before the snapshot it was
    last proven on (None: none known); the fingerprints of its construction's reads; when it was built."""

    __slots__ = ("template", "token", "reads", "built_at")

    def __init__(self, template, token, reads, built_at):
        self.template, self.token, self.reads, self.built_at = template, token, reads, built_at


class ReviewPassBoundary:
    """One pass's boundary and the proof that it is current (module docstring). Used on the thread that made it.

    `clock` (monotonic seconds) and `loop` (the pass's event loop, for the stall callback) are the caller's; the
    tests give a fake clock and no loop."""

    def __init__(self, resolver, reviews, *, clock=time.monotonic, loop=None):
        from .search_index import SearchVerification
        self._resolver = resolver
        self._thread = threading.get_ident()
        self._closed = False
        # N3a's canonical token, unchanged; its probe connection is this pass's and is closed with it.
        self._verification = SearchVerification(resolver, reviews)
        self._kept = None    # a _Kept, or None
        self._before = {}    # an open read's connection -> the canonical token read before its snapshot (`opened`)
        self._clock, self._loop = clock, loop
        self._max_age = max_age()
        self._last_row = None   # when the last row ended (the clock's seconds)
        self._idle = None       # the stall callback's handle, between rows
        self.counts = {"built": 0, "token": 0, "reads": 0, "aged": 0, "stalled": 0, "switched_off": 0}

    def _usable(self, resolver) -> bool:
        return not self._closed and resolver is self._resolver and threading.get_ident() == self._thread

    def _token(self):
        try:
            return self._verification.canonical_token()   # None when it cannot be read
        except Exception:  # noqa: BLE001 -- unreadable never matches
            return None

    def _drop(self) -> None:
        kept, self._kept = self._kept, None
        if kept is not None:
            _end(kept.template)

    @contextlib.contextmanager
    def row(self):
        """One row's call (`prepare`, or `publish` with its own `prepare`): the reads it opens may use the pass's
        boundary. Both switches are read here, at every row. The variable is reset on every way out."""
        current = self
        if not self._closed and threading.get_ident() == self._thread:
            self._between_rows()
            if not switched_on():
                # Off from this row on: no template is kept, and every read builds its own, as without the switch.
                if self._kept is not None:
                    self.counts["switched_off"] += 1
                self._drop()
                current = None
        token = _CURRENT.set(current)
        try:
            yield
        finally:
            _CURRENT.reset(token)
            if not self._closed and threading.get_ident() == self._thread:
                self._last_row = self._clock()
                self._arm()

    def _between_rows(self) -> None:
        """A row starts: no stall callback is pending any more, the cap is read again, and a template the pass has not
        used for longer than the cap is dropped (if the callback could not do it)."""
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        self._max_age = max_age()
        if self._last_row is not None and self._clock() - self._last_row > self._max_age:
            self._stall()

    def _arm(self) -> None:
        """Between rows: drop the template once the pass has gone the cap without a row (on the pass's own loop)."""
        if self._loop is None or self._kept is None:
            return
        try:
            self._idle = self._loop.call_later(self._max_age, self._stalled)
        except Exception:  # noqa: BLE001 -- no callback (the loop is closing): the next row's check drops it
            self._idle = None

    def _stalled(self) -> None:
        """The loop's callback: no row came within the cap (or, called early, armed again for what is left)."""
        self._idle = None
        if self._closed or threading.get_ident() != self._thread or self._last_row is None:
            return
        left = self._max_age - (self._clock() - self._last_row)
        if left <= 0:
            self._stall()
        elif self._loop is not None and self._kept is not None:
            try:
                self._idle = self._loop.call_later(left, self._stalled)
            except Exception:  # noqa: BLE001 -- no callback: the next row's check drops it
                self._idle = None

    def _stall(self) -> None:
        if self._kept is not None:
            self.counts["stalled"] += 1
        self._drop()

    def opened(self, resolver, conn) -> None:
        """A resolver read is about to make its first statement: the token its snapshot is established after."""
        if self._usable(resolver):
            self._before[conn] = self._token()

    def closed(self, resolver, conn) -> None:
        self._before.pop(conn, None)

    def boundary_for(self, resolver, conn):
        """The boundary for this read, proven current or built afresh on it; None outside this pass's own use."""
        if not self._usable(resolver) or not _in_transaction(conn):
            return None
        before = self._before.get(conn)   # None for a read not opened inside a row: then no token is kept for it
        kept = self._kept
        if kept is not None and self._clock() - kept.built_at > self._max_age:
            self.counts["aged"] += 1      # older than the cap: built again, whatever the proof would say
            self._drop()
            kept = None
        if kept is not None:
            proof = self._proof(conn, kept.token, kept.reads)
            if proof is not None:
                if proof == "reads":
                    # Proven on this read's own snapshot, which was established after `before` was read.
                    kept.token = before
                self.counts[proof] += 1
                return kept.template.rebind(conn)
        self._drop()
        # The resolver's own factory: what a fresh boundary refuses with, this refuses with.
        fresh = resolver._new_entity_boundary(conn, record=True)
        reads, fresh._reads = tuple(fresh._reads), None
        handed = fresh.rebind(conn)
        fresh.conn = None   # the template reads no connection: each read gets its own copy
        self._kept = _Kept(fresh, before, reads, self._clock())
        self.counts["built"] += 1
        return handed

    def _proof(self, conn, token, reads):
        """"token", "reads", or None (not proven, or not readable: build afresh)."""
        try:
            # The read's snapshot exists before the token is read again (it does already, after `_read`'s own
            # reads; this does not rely on that).
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            after = self._token()
            if token is not None and after is not None and after == token:
                return "token"
            from .entity_boundary import unchanged_on
            if unchanged_on(conn, reads):
                return "reads"
        except Exception:  # noqa: BLE001 -- what cannot be proven is built afresh
            return None
        return None

    def close(self) -> None:
        """The pass ended: keep nothing. A copy of the context made inside a row finds this closed."""
        self._closed = True
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        self._drop()
        self._before.clear()
        self._verification.close()


def opened(resolver, conn) -> None:
    """`EvidenceResolver._read`, before its first statement on `conn`. Nothing outside a pass's row."""
    keeper = _CURRENT.get()
    if keeper is not None:
        keeper.opened(resolver, conn)


def closed(resolver, conn) -> None:
    """`EvidenceResolver._read`, as it closes `conn`."""
    keeper = _CURRENT.get()
    if keeper is not None:
        keeper.closed(resolver, conn)


def for_read(resolver, conn):
    """`EvidenceResolver.entity_boundary`, for a read of its own: the pass's boundary, or None (build one, as
    always)."""
    keeper = _CURRENT.get()
    if keeper is None:
        return None
    return keeper.boundary_for(resolver, conn)


@contextlib.contextmanager
def review_pass(resolver, reviews):
    """One automatic-review pass. Yields `row`, which a caller opens around each row's `prepare` and `publish`; with
    the switch off when the pass opens (the default) `row` is a no-op and every read builds its own boundary, as
    always. Opened inside a running event loop (the worker's), the stall callback runs on that loop."""
    keeper = None
    if switched_on():
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            keeper = ReviewPassBoundary(resolver, reviews, loop=loop)
        except Exception:  # noqa: BLE001 -- no keeper: one boundary per row, as with the switch off
            keeper = None
    if keeper is None:
        yield contextlib.nullcontext
        return
    try:
        yield keeper.row
    finally:
        keeper.close()
