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
4. Otherwise, or when anything in 2 or 3 cannot be read: a fresh `EntityBoundary(conn)` on this read (it refuses as a
   fresh one refuses), and that is the pass's template from now on.
So an Off-limits entry added or removed, an excluded or merged entity, a changed alias, contact or mention, between
two rows, is seen by the next row exactly as a fresh boundary would see it. What the boundary does not read (the
owner's identity binding, exclusions, opt-outs, the row itself and its neighbours) is read by each row's own reads, as
before, and is never kept.

**Scope.** The template lives for one pass on the thread that opened it (one worker run; a catch-up slice is one run),
never in a module global or on the resolver. The row scope is a context variable, so another thread or request never
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

from . import switches

#: The pass whose row is running on this context (a `ReviewPassBoundary`), set by `row()` and reset on its way out.
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("review_pass_boundary", default=None)


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


class ReviewPassBoundary:
    """One pass's boundary and the proof that it is current (module docstring). Used on the thread that made it."""

    def __init__(self, resolver, reviews):
        from .search_index import SearchVerification
        self._resolver = resolver
        self._thread = threading.get_ident()
        self._closed = False
        # N3a's canonical token, unchanged; its probe connection is this pass's and is closed with it.
        self._verification = SearchVerification(resolver, reviews)
        self._kept = None    # (template EntityBoundary, token before its proven snapshot or None, its reads)
        self._before = {}    # an open read's connection -> the canonical token read before its snapshot (`opened`)
        self.counts = {"built": 0, "token": 0, "reads": 0}

    def _usable(self, resolver) -> bool:
        return not self._closed and resolver is self._resolver and threading.get_ident() == self._thread

    def _token(self):
        try:
            return self._verification.canonical_token()   # None when it cannot be read
        except Exception:  # noqa: BLE001 -- unreadable never matches
            return None

    @contextlib.contextmanager
    def row(self):
        """One row's call (`prepare`, or `publish` with its own `prepare`): the reads it opens may use the pass's
        boundary. The variable is reset on every way out."""
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)

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
        from .entity_boundary import EntityBoundary
        before = self._before.get(conn)   # None for a read not opened inside a row: then no token is kept for it
        kept = self._kept
        if kept is not None:
            template, token, reads = kept
            proof = self._proof(conn, token, reads)
            if proof is not None:
                if proof == "reads":
                    # Proven on this read's own snapshot, which was established after `before` was read.
                    self._kept = (template, before, reads)
                self.counts[proof] += 1
                return template.rebind(conn)
        if kept is not None:
            self._kept = None
            _end(kept[0])
        fresh = EntityBoundary(conn, record=True)   # what a fresh boundary refuses with, this refuses with
        reads, fresh._reads = tuple(fresh._reads), None
        handed = fresh.rebind(conn)
        fresh.conn = None   # the template reads no connection: each read gets its own copy
        self._kept = (fresh, before, reads)
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
        kept, self._kept = self._kept, None
        self._before.clear()
        if kept is not None:
            _end(kept[0])
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
    the switch off (the default) `row` is a no-op and every read builds its own boundary, as always."""
    keeper = None
    if switches.on(switches.REVIEW_PASS_BOUNDARY):
        try:
            keeper = ReviewPassBoundary(resolver, reviews)
        except Exception:  # noqa: BLE001 -- no keeper: one boundary per row, as with the switch off
            keeper = None
    if keeper is None:
        yield contextlib.nullcontext
        return
    try:
        yield keeper.row
    finally:
        keeper.close()
