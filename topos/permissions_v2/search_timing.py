"""Owner-local p2c search timing (plan MG-1/MG-3, contract IF-3). Off unless TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS=true.

Content-free by construction. Every line is

    permission_search_timing run=<32 hex> stage=<name> elapsed_ms=<ms> corr=<16 hex|-> t_ms=<ms> [key=value ...]

and the extra keys carry only durations, clocks, counts, a write-gate holder's thread name and a
code location. Never a query, record, grant, actor, key, request id or content.

``corr`` joins this node's lines to the control plane's lines for the same search. It is derived
from the request id the control plane generated at random and signed into the envelope -- the
request binding the transport checks -- so no new field crosses the wire and the id itself is never
logged. control_plane/permissions_v2/search_timing.py derives it the same way; both repos pin one
test vector.

``t_ms`` is time.monotonic() in milliseconds: the clock the write gate stamps its holders with, so a
search's stages, its gate waits and the sweeper's gate holds share one timeline.

Nesting, for anyone summing lines: the adapter stages (runtime_setup .. sign) run inside the
transport's adapter hop; ``gate_wait point=X`` lies inside stage X; ``gate_probe`` reports how long ANOTHER thread has held the gate, not this search's
time. transport_total = pre_adapter + queue_wait (both hops) + the adapter stages + send_check + send
+ a small untimed remainder (runtime lookup, post-checks, frame building). send_check's own fields
(open_ms .. check_own_ms) split it after its gate_wait.

check_own's review digest (p2c-v2/v3 grants only) enters the gate through the review store, outside
any timed section, so its wait has points of its own: ``gate_wait point=index_load_digest`` lies
inside stage index_load, and ``gate_wait point=send_check_digest`` lies inside send_check's
check_own part. Neither is written when this thread already holds the gate. Since N3a a stage that
reuses the digest an earlier stage of the same search verified (search_index.SearchVerification)
enters no gate for it and writes no digest line: in a quiet search only index_load_digest appears.

IF-3 v1.4 (WS4 N3c) splits check_own's ``members_ms`` further, on index_load and on send_check:
``dependencies_ms`` (every dependency load of the pass), inside it ``dependency_boundary_ms`` (their
Off-limits checks); ``provenance_setup_ms`` (the pass's one provenance service, built at its first
recovered iMessage, normally inside dependencies_ms), ``provenance_check_ms`` (its one store check, after
the last member) and ``provenance_snapshot_ms`` (its snapshot re-hash, after that). The provenance parts
are absent when no member needed native provenance. The pass's two gate entries have points of their
own: ``gate_wait point=index_load_provenance_setup|send_check_provenance_setup`` inside
provenance_setup_ms and ``gate_wait point=index_load_provenance|send_check_provenance`` inside
provenance_check_ms; the recheck holds the gate already, so it writes neither.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar

FLAG = "TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS"
_CORRELATION_DOMAIN = b"topos-p2c-search-timing/v1\x00"
logger = logging.getLogger("topos.permissions_v2.search_timing")

#: What MessageSearchRelease reports through ``observe``; anything else it reports is dropped.
ADAPTER_STAGES = frozenset({"runtime_setup", "admit", "index_load", "embed", "rank", "recheck", "checkpoint", "sign",
                            "accept"})
#: The only extra keys an adapter reading may carry: a batch's size, or an item's position in it (IF-3 v1.3).
ADAPTER_FIELDS = frozenset({"n", "item"})
#: Durations (ms) that split a check_own: on `index_load` (with `load_ms`, the index file read) and on
#: `send_check`. Each part lies inside that line's `check_own_ms` (IF-3 v1.3).
CHECK_OWN_PARTS = ("boundary", "digest", "members")
#: Where check_own's `members_ms` goes (IF-3 v1.4): the dependency loads and their boundary checks, and the
#: pass's one provenance service, store check and snapshot re-hash (search_index, WS4 N3c).
MEMBER_PARTS = ("dependencies", "dependency_boundary", "provenance_setup", "provenance_check", "provenance_snapshot")
ADAPTER_DURATIONS = frozenset({"check_own_ms", "load_ms", *(f"{part}_ms" for part in CHECK_OWN_PARTS + MEMBER_PARTS)})


def _durations(fields) -> dict:
    """Only the known duration keys, only finite non-negative numbers, as one ms token each."""
    out = {}
    for key, value in fields.items():
        if (key in ADAPTER_DURATIONS and isinstance(value, (int, float)) and not isinstance(value, bool)
                and 0 <= value < 1e9):
            out[key] = f"{value:.3f}"
    return out

_active: ContextVar["SearchTiming | None"] = ContextVar("topos_p2c_search_timing", default=None)
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def enabled() -> bool:
    return os.environ.get(FLAG, "").lower() == "true"


def correlation_id(request_id: str) -> str:
    """16 hex characters naming one search on both services, derived from the signed request id."""
    return hashlib.sha256(_CORRELATION_DOMAIN + request_id.encode("utf-8")).hexdigest()[:16]


def _token(value) -> str:
    """A thread name or code location, reduced to one log token."""
    return _UNSAFE.sub("_", str(value).replace(" in ", ":"))[:64] or "-"


def _gate_holder():
    """The write gate's current holder (site, ident, thread, since), read without taking the gate."""
    from topos.storage.db import write_gate
    try:
        with write_gate._holder_lock:
            return write_gate._holder
    except AttributeError:  # the gate's diagnostics changed shape: probes go quiet, searches do not
        return None


class SearchTiming:
    """One search's lines (or one sweep's). No method raises: timing never changes a search."""

    def __init__(self, corr: str | None = None):
        self.run = uuid.uuid4().hex
        self.corr = corr or "-"
        self._gate_asked: float | None = None
        self._held_back: list[tuple[str, float, float]] = []

    def emit(self, stage: str, seconds: float, **fields) -> None:
        try:
            extra = "".join(f" {key}={value}" for key, value in fields.items())
            logger.info("permission_search_timing run=%s stage=%s elapsed_ms=%.3f corr=%s t_ms=%.3f%s",
                        self.run, stage, seconds * 1000, self.corr, time.monotonic() * 1000, extra)
        except Exception:  # noqa: BLE001
            pass

    def observe(self, stage: str, seconds: float, **fields) -> None:
        """MessageSearchRelease's callback, plus write-gate readings where the adapter asks for the gate next.

        A batched search (search_transport.dispatch_message_search_batch) reports its shared stages
        once with ``n=<N>`` and its per-query stages (embed, rank, accept, sign) with ``item=<i>``;
        ``accept`` is one query's candidate walk, which lies inside the batch's ``recheck``. Only
        small integers pass; anything else is dropped.
        """
        if stage not in ADAPTER_STAGES:
            return
        extra = {key: value for key, value in fields.items()
                 if key in ADAPTER_FIELDS and isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 64}
        if stage == "index_load":
            extra.update(_durations(fields))
        self.emit(stage, seconds, **extra)
        if stage == "runtime_setup":
            self.probe("admit")  # dispatch's first gated act is admission
        elif stage == "admit":
            self.probe("index_load")  # then the grant's authority read, a ledger transaction under the gate
        elif stage == "rank":
            self.asking()  # then the recheck gate, with no call in between
        elif stage == "recheck":
            self.acquired("recheck")  # reported from inside that gate
        elif stage == "checkpoint":
            self.flush()  # that gate is released

    def probe(self, point: str) -> None:
        """Who holds the gate as this search is about to ask for it, and for how long so far."""
        try:
            holder = _gate_holder()
            if holder is None or holder.ident == threading.get_ident():
                self.emit("gate_probe", 0.0, point=point, holder="none", site="-")
            else:
                self.emit("gate_probe", time.monotonic() - holder.since, point=point,
                          holder=_token(holder.thread), site=_token(holder.site))
        except Exception:  # noqa: BLE001
            pass

    def asking(self) -> None:
        self._gate_asked = time.monotonic()

    def acquired(self, point: str) -> float | None:
        """Called inside a gated section: the wait is the gate's own acquisition stamp minus ``asking``.

        Held back until ``flush``, so timing writes no log line while this search holds the gate.
        Returns that acquisition stamp (monotonic seconds), or None when it cannot be read.
        """
        try:
            asked, self._gate_asked = self._gate_asked, None
            holder = _gate_holder()
            if asked is not None and holder is not None and holder.ident == threading.get_ident():
                self._held_back.append((point, asked, holder.since))
                return holder.since
        except Exception:  # noqa: BLE001
            pass
        return None

    def flush(self) -> None:
        held, self._held_back = self._held_back, []
        for point, asked, since in held:
            self.emit("gate_wait", since - asked, point=point, start_ms=f"{asked * 1000:.3f}")

    @contextmanager
    def gate(self, point: str):
        """Enter the write gate just before a section whose first act is entering it, to time the wait exactly.

        The gate is reentrant, so the section re-enters at once: the same work is serialized in the
        same order, only microseconds longer. The raw lock names no holder, so the gate's registry
        still names the section's own site. The gate's slow-section warning no longer sees this
        wait (it reports waited=0.0 for a long hold); this line carries it.
        """
        from topos.storage.db.write_gate import db_write_lock
        asked = time.monotonic()
        waited = None
        try:
            with db_write_lock():
                waited = time.monotonic() - asked
                yield
        finally:
            if waited is not None:
                self.emit("gate_wait", waited, point=point, start_ms=f"{asked * 1000:.3f}")

    @contextmanager
    def span(self, stage: str):
        started = time.monotonic()
        try:
            yield
        finally:
            self.emit(stage, time.monotonic() - started)


class TransportTiming(SearchTiming):
    """search_transport.py's spans for one search; the adapter reports to the same run and corr."""

    #: The send check's parts, in order, each measured from the end of the one before; the first from
    #: the gate's acquisition. open = the ledger connection and BEGIN IMMEDIATE once the gate is held.
    SEND_CHECK_PARTS = ("open", "protection", "authority", "commit", "check_own")

    def __init__(self):
        super().__init__()
        self._received = time.monotonic()
        self._received_at = time.time()
        self._hops: dict[str, dict[str, float]] = {}
        self._laps: dict[str, float | None] = {}
        self._fields: dict[str, int] = {}

    def acquired(self, point: str) -> float | None:
        since = super().acquired(point)
        self._laps = {"gate": since, "open": time.monotonic()}
        return since

    def lap(self, name: str) -> None:
        self._laps[name] = time.monotonic()

    def check_own_laps(self) -> dict | None:
        """A dict for the send check's check_own to fill (search_index laps); its parts join the send_check line."""
        self._check_own_laps = {}
        return self._check_own_laps

    def bound(self, request_id: str, **fields) -> None:
        """The request binding held: pre_adapter ends and the correlation id is known.

        For a batch, ``request_id`` is the frame's batch id (the corr both services derive from it)
        and ``n`` its size, which ``transport_total`` repeats.
        """
        try:
            self.corr = correlation_id(request_id)
            self._fields = {key: value for key, value in fields.items() if key == "n" and isinstance(value, int)}
            self.emit("pre_adapter", time.monotonic() - self._received, **self._fields)
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def active(self):
        """Make this the timing runtime.message_search() reports to, inside the worker thread."""
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def _mark(self, hop: str, event: str) -> None:
        self._hops.setdefault(hop, {})[event] = time.monotonic()

    def submitted(self, hop: str) -> None:
        self._mark(hop, "submitted")

    def started(self, hop: str) -> None:
        self._mark(hop, "started")

    def ended(self, hop: str) -> None:
        try:
            self._mark(hop, "ended")
            if hop == "send_check":
                marks = self._hops[hop]
                self.flush()
                parts, previous = {}, self._laps.get("gate")
                for part in self.SEND_CHECK_PARTS:
                    at = self._laps.get(part)
                    if previous is None or at is None:
                        break
                    parts[f"{part}_ms"] = f"{(at - previous) * 1000:.3f}"
                    previous = at
                laps = getattr(self, "_check_own_laps", None) or {}
                parts.update(_durations({f"{part}_ms": seconds * 1000 for part, seconds in laps.items()
                                         if part in CHECK_OWN_PARTS + MEMBER_PARTS}))
                self.emit("send_check", marks["ended"] - marks["started"], **parts)
        except Exception:  # noqa: BLE001
            pass

    def resumed(self, hop: str) -> None:
        """Back on the event loop: queue_wait is the executor's wait plus the loop's delay in resuming."""
        try:
            self._mark(hop, "resumed")
            marks = self._hops[hop]
            executor = marks["started"] - marks["submitted"]
            resume = marks["resumed"] - marks["ended"]
            self.emit("queue_wait", executor + resume, hop=hop,
                      executor_ms=f"{executor * 1000:.3f}", resume_ms=f"{resume * 1000:.3f}")
        except Exception:  # noqa: BLE001
            pass

    def finish(self, outcome: str) -> None:
        """After the frame went out; ``outcome`` is that frame's status (ok or error)."""
        try:
            self.flush()
            self.emit("transport_total", time.monotonic() - self._received, outcome=outcome,
                      recv_at=f"{self._received_at * 1000:.3f}", sent_at=f"{time.time() * 1000:.3f}", **self._fields)
        except Exception:  # noqa: BLE001
            pass


class _Off:
    """What the transport holds when timing is off: every call does nothing."""

    def bound(self, request_id, **fields): pass
    def active(self): return nullcontext()
    def submitted(self, hop): pass
    def started(self, hop): pass
    def ended(self, hop): pass
    def resumed(self, hop): pass
    def asking(self): pass
    def acquired(self, point): pass
    def lap(self, name): pass
    def check_own_laps(self): return None
    def span(self, stage): return nullcontext()
    def finish(self, outcome): pass


_OFF = _Off()


def transport():
    """The transport's timing for one search: a no-op object unless timing is on."""
    return TransportTiming() if enabled() else _OFF


def for_adapter() -> SearchTiming | None:
    """What runtime.message_search() reports to: the transport's timing for this search, a standalone
    one when timing is on without a transport (owner tools, tests), or None (no observer, as before)."""
    if not enabled():
        return None
    return _active.get() or SearchTiming()


def gate_wait(point: str | None):
    """Time the wait where untimed code of this search enters the write gate next: ``gate_wait point=<point>``.

    For check_own's review digest, which enters the gate through the review store's ``_db``. The
    same pattern as runtime setup: this search enters the gate first, and the section re-enters it
    at once. A no-op when timing is off, when no search's timing is active on this thread, and when
    this thread already holds the gate (no wait is possible there, and no line may be written while
    the gate is held).
    """
    if point is None or not enabled():
        return nullcontext()
    timing = _active.get()
    if timing is None:
        return nullcontext()
    try:
        from topos.storage.db.write_gate import db_write_lock
        if db_write_lock()._is_owned():
            return nullcontext()
    except Exception:  # noqa: BLE001 -- the gate's shape changed: the line goes quiet, the search does not
        return nullcontext()
    return timing.gate(point)


def timed_sweep(index) -> int:
    """The daemon's sweep. With timing on, the sweeper's own gate wait and its gate hold are timed (sweep_hold).

    sweep() enters the write gate first thing and holds it across every index file's deep check;
    entering it here first times the wait and the hold exactly without holding it any longer.
    """
    if not enabled():
        return index.sweep()
    from topos.storage.db.write_gate import db_write_lock
    line = SearchTiming()
    asked = time.monotonic()
    acquired = released = None
    removed = None
    try:
        with db_write_lock():
            acquired = time.monotonic()
            try:
                removed = index.sweep()
            finally:
                released = time.monotonic()
    finally:
        if acquired is not None and released is not None:
            line.emit("sweep_hold", released - acquired, wait_ms=f"{(acquired - asked) * 1000:.3f}",
                      start_ms=f"{acquired * 1000:.3f}", removed=removed if isinstance(removed, int) else "-")
    return removed
