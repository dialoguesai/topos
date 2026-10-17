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

IF-3 v1.5 (WS4 N5): index load checks the basis only, so `index_load` carries boundary_ms and digest_ms but no
members_ms. The gated recheck is the pass that always runs the member loop, and its `recheck` line carries that
`_current`'s boundary, digest, members and v1.4 member parts. The send check reads a revision token first under
its gate (`token_ms`, between open_ms and protection_ms) and runs its member loop only when something moved since
the recheck, so its check_own parts are absent when it skipped.

IF-3 v1.6 (1.4.4): the daemon sweep checks outside the write gate and enters it only for brief steps. Its one
`sweep_hold` line per sweep now reports those steps: elapsed_ms their total hold, wait_ms their total wait, start_ms
the first one's start, and the new keys holds (their number), longest_ms and check_ms (the sweep's time outside the
gate). The check's own brief gate entries (provenance store checks, the durable clock read) are not in it. Before,
elapsed_ms was one hold across the whole sweep.

Attribution (NR166 R0; proposed as IF-3 v1.7). Off unless TOPOS_PERMISSIONS_V2_SEARCH_ATTRIBUTION=true as well as
the timing switch; with it off every line above is exactly as before and nothing below is computed. It answers what
the slow-section log line cannot: which caller of a shared gated helper held the write gate, how long each request
waited for and held it, and whether each "nothing changed" fast path was taken. Still content-free: code locations,
closed names, counts and durations.

- ``gate_section at=<stage> site=<location> caller=<location> nested=<0|1> n=<N> wait_ms=<ms> longest_ms=<ms>``:
  the write-gate sections that ENDED on this search's thread during that stage, added up per call site and per
  caller of that site. elapsed_ms is their total hold. nested=1 when the thread already held the gate (the time is
  inside an outer section's), so a search's own hold is the sum over nested=0. A section that spans stages is
  reported by the stage it ends in. Where timing itself enters the gate first to time a wait (``gate_wait``), that
  entry is the section (site ``search_timing.gate:<point>``) and the code's own entry is nested inside it.
- ``fast_path at=<stage> path=<name> outcome=<hit|miss|first|skip> moved=<parts|-> n=<N>``: one "nothing changed"
  fast path. Paths: ``n3a_boundary`` and ``n3a_digest`` (SearchVerification's reuse of the Off-limits closure and
  of the review digest), ``n5_keep`` (the gated recheck keeping its state for the send check), ``n5_send`` (the send
  check skipping its member loop), ``bl171_guard`` (the review guard's kept verdict) and, only while a release is
  decided outside the write gate (NR166 R1, ``DECIDE_OUTSIDE_GATE``), ``r1_confirm`` (the decision's token found
  unchanged under the gate; a miss is a decision run again or refused), and, only while the daemon sweep keeps its
  interest verdicts (NR187, ``SWEEP_INTEREST_VERDICT``), ``nr187_interest`` (an index's interest members not rebuilt
  because nothing they read moved; a miss names the parts that did: ``file``, ``key``, ``canonical``, ``boundary``,
  ``inputs``, ``before``, or ``full_every`` for the full decision every Nth sweep makes). ``first`` is a search's
  first computation, with nothing to reuse yet; ``skip`` is a guard that was not needed (the review digest is the
  one the index was built on). On a miss, ``moved`` names the token parts that differed, joined by ``:`` (for
  example ``data_version:file_wal``, or ``canonical.data_version`` inside a send token), or ``not_kept`` (an earlier
  stage could not keep its value), ``unreadable``, ``identity`` or ``file_replaced``. Never a token's value.
- ``answer_index_load``, ``answer_rank``, ``answer_recheck``, ``answer_walk``: `retrieve_for_answer`'s stages, which
  had no lines. answer_rank ends inside the recheck gate, so it includes that gate's wait (the ``gate_section`` of
  the retrieval's own site carries the wait); answer_index_load and answer_recheck carry the same duration fields
  as index_load and recheck. All four are written when the retrieval ends, with ``end_ms`` (the ``t_ms`` clock) for
  when each stage ended. Their corr is ``-`` (the answers door derives none); ``run`` joins them.
- ``gate_site site= caller= nested= n= wait_ms= longest_ms= longest_wait_ms= window_ms=`` and ``fast_path_total
  path= outcome= moved= n= window_ms=``: the same two tables for the whole process (every thread, ingestion and the
  sweep included) since the last such lines, written by the sweeper at most once a minute.
- ``refusal door=<search|search_batch|answers|answer_retrieval> reason=<word>``: why this node refused a request,
  as the one bounded machine word the refusal was raised with (a ``PolicyError`` code; ``other`` for anything
  else). On 13 Oct three refusals in a row could not be told apart in the log: a share with no index, a paused
  share and a wrong mode all read the same. Nothing of it is on the wire, where every refusal stays the same
  bytes. A search's line carries its ``run`` and ``corr``. ``refusal_total door= reason= n= window_ms=`` is the
  process-wide count.
- ``index_stale share=<8 hex> class=<word>``, ``index_removed share=<8 hex>`` and ``index_rebuild share=<8 hex>
  state=<word>`` (elapsed_ms the build's length): what happened to a share's index, with ``at_ms`` (the ``t_ms``
  clock) for when. ``share`` is not the grant's id and not the file's name: it is the first 8 hex characters of a
  domain-separated SHA-256 of the index file's name (``share_label``), so two lines about one share can be joined
  across a restart, and an owner can find the share by hashing the names in the index folder. Beside it, two facts
  that tell shares apart at a glance: ``mode=<only|with_sources|records|unknown>`` (the share's mode as the last
  request or build in this process saw it) and ``members=<N|->`` (its index's member count as last seen; on an
  ``index_rebuild`` line, the count that build ended with). NR188 (DR-31): an ``index_stale`` line about a member's
  ``dependencies`` or ``classification_context`` also carries ``sub=<word>``, what moved (``conversation_context``,
  ``row_binding``, ``row_revision``, ``missing``; ``neighbour_inserted``, ``neighbour_changed``, ``row_unavailable``);
  with ``TOPOS_PERMISSIONS_V2_INDEX_NARROW_MEMBERS`` on, ``scope=member`` marks the first member a check withheld
  rather than the whole index going, and the sweep's narrowed publish is an ``index_rebuild`` line with
  ``state=narrowed`` and the members the narrowed index holds.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path

from . import switches

FLAG = switches.SEARCH_TIMINGS.name
ATTRIBUTION_FLAG = switches.SEARCH_ATTRIBUTION.name
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
#: NR166 R2b's profile: the rest of one member's check, in the order it runs (`dependencies` comes after
#: `unseal`). Written beside the parts above only with attribution on, so a September line is unchanged without
#: it. Each is a total over the pass's members; together with `dependencies` they add up to `members`, less the
#: loop's own bookkeeping and the pass's one provenance check.
MEMBER_STEPS = ("unseal", "live_rows", "review_context", "entity_context", "fingerprint", "lineage")
STEP_DURATIONS = frozenset(f"{step}_ms" for step in MEMBER_STEPS)
#: `retrieve_for_answer`'s stages (NR166 R0), reported only while attribution is on: the answers door had no lines.
ANSWER_STAGES = frozenset({"answer_index_load", "answer_rank", "answer_recheck", "answer_walk"})


def _durations(fields) -> dict:
    """Only the known duration keys, only finite non-negative numbers, as one ms token each."""
    out = {}
    steps = None   # asked once, and only for a line that carries one
    for key, value in fields.items():
        known = key in ADAPTER_DURATIONS
        if not known and key in STEP_DURATIONS:
            steps = attributing() if steps is None else steps
            known = steps
        if known and isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e9:
            out[key] = f"{value:.3f}"
    return out

_active: ContextVar["SearchTiming | None"] = ContextVar("topos_p2c_search_timing", default=None)
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def enabled() -> bool:
    return switches.on(switches.SEARCH_TIMINGS)


def correlation_id(request_id: str) -> str:
    """16 hex characters naming one search on both services, derived from the signed request id."""
    return hashlib.sha256(_CORRELATION_DOMAIN + request_id.encode("utf-8")).hexdigest()[:16]


def _token(value, limit: int = 64) -> str:
    """A thread name or code location, reduced to one log token."""
    return _UNSAFE.sub("_", str(value).replace(" in ", ":"))[:limit] or "-"


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
        self._pending: list[tuple[str, float, dict]] = []  # attribution lines, held back while the gate is held

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
            if stage in ANSWER_STAGES and attributing():
                self._answer_stage(stage, seconds, fields)
            return
        extra = {key: value for key, value in fields.items()
                 if key in ADAPTER_FIELDS and isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 64}
        if stage in ("index_load", "recheck"):  # IF-3 v1.5: the recheck carries its member loop's split too
            extra.update(_durations(fields))
        self.emit(stage, seconds, **extra)
        self._collect(stage)
        if stage == "sign":
            self._release()  # no gate is held at a sign: a search without a transport ends here
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
        self._release()

    # -- attribution (NR166 R0); every method is inert while it is off ------------------------------------------

    def _answer_stage(self, stage: str, seconds: float, fields) -> None:
        """One of `retrieve_for_answer`'s stages. answer_walk is its last, reported with the gate released.

        Two of them end inside the gate, so each line is held back with its own end (`end_ms`, the `t_ms` clock).
        """
        try:
            extra = _durations(fields) if stage in ("answer_index_load", "answer_recheck") else {}
            self._pending.append((stage, seconds, dict(extra, end_ms=f"{time.monotonic() * 1000:.3f}")))
            self._collect(stage)
            if stage == "answer_walk":
                self._release()
                _close_thread()
        except Exception:  # noqa: BLE001
            pass

    def _collect(self, stage: str) -> None:
        """This thread's gate sections and fast-path outcomes since the last stage, added up as that stage's lines.

        Held back: a stage may be reported from inside the gate, and attribution writes no line there.
        """
        try:
            sections, paths = getattr(_open, "sections", None), getattr(_open, "paths", None)
            if not sections and not paths:
                return
            if not attributing():  # switched off since this thread's search was opened: nothing is written
                _close_thread()
                return
            by_site: dict = {}
            for site, caller, waited, held, nested in sections or ():
                entry = by_site.setdefault((site, caller, nested), [0, 0.0, 0.0, 0.0])
                entry[0] += 1
                entry[1] += waited
                entry[2] += held
                entry[3] = max(entry[3], held)
            for (site, caller, nested), (count, waited, held, longest) in by_site.items():
                self._pending.append(("gate_section", held, dict(
                    at=stage, site=_token(site, 160), caller=_token(caller, 160), nested=int(nested), n=count,
                    wait_ms=f"{waited * 1000:.3f}", longest_ms=f"{longest * 1000:.3f}")))
            by_path: dict = {}
            for key in paths or ():
                by_path[key] = by_path.get(key, 0) + 1
            for (path, outcome, moved), count in by_path.items():
                self._pending.append(("fast_path", 0.0, dict(at=stage, path=path, outcome=outcome,
                                                               moved=_token(moved, 160), n=count)))
            dropped = getattr(_open, "dropped", 0)
            if dropped:
                self._pending.append(("attribution_dropped", 0.0, dict(at=stage, n=dropped)))
                _open.dropped = 0
            if sections:
                sections.clear()
            if paths:
                paths.clear()
        except Exception:  # noqa: BLE001
            pass

    def _release(self) -> None:
        """Write the held-back attribution lines, unless this thread holds the gate: then a later stage does."""
        if not self._pending:
            return
        try:
            from topos.storage.db.write_gate import db_write_lock
            if db_write_lock()._is_owned():
                return
            pending, self._pending = self._pending, []
            for stage, seconds, fields in pending:
                self.emit(stage, seconds, **fields)
        except Exception:  # noqa: BLE001
            pass

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
        nested = attributing() and db_write_lock()._is_owned()
        try:
            with db_write_lock():
                waited = time.monotonic() - asked
                yield
        finally:
            if waited is not None:
                self.emit("gate_wait", waited, point=point, start_ms=f"{asked * 1000:.3f}")
                try:
                    if attributing():  # this entry is the section; the code's own entry is nested inside it
                        _section(f"search_timing.gate in {point}", "-", waited, time.monotonic() - asked - waited,
                                 nested)
                except Exception:  # noqa: BLE001
                    pass

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
    SEND_CHECK_PARTS = ("open", "token", "protection", "authority", "commit", "check_own")

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
        if attributing():
            _open_thread()  # this worker thread's gate sections and fast paths are this search's until `ended`

    def ended(self, hop: str) -> None:
        try:
            self._mark(hop, "ended")
            self._collect(hop)
            _close_thread()
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
                                         if part in CHECK_OWN_PARTS + MEMBER_PARTS + MEMBER_STEPS}))
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
    timing = _active.get()
    if timing is None:
        timing = SearchTiming()
        if attributing():
            _open_thread()  # no transport: this thread's sections are this adapter's until its last stage
    return timing


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
    """The daemon's sweep. With timing on, one sweep_hold line per sweep (IF-3 v1.6, 1.4.4).

    The sweep checks outside the write gate and enters it only for brief steps (the grants' authority, the review
    digest, each removal), which it records on its own thread (SearchIndexService.sweep). elapsed_ms is their total
    hold, wait_ms their total wait, start_ms the first one's start, holds how many there were, longest_ms the
    longest, and check_ms the rest of the sweep, spent outside the gate. The check's own brief gate entries are not
    in elapsed_ms: a recovered iMessage row's provenance store check and the durable clock read, about 200 per sweep
    of 2 to 10 ms each on the 2 Oct census copy. Until 1.4.4 this entered the gate first and the sweep held it across
    its whole check, so elapsed_ms was the whole sweep.
    """
    if not enabled():
        return index.sweep()
    if attributing():
        emit_totals()  # the process-wide tables, at most once a minute, from a thread that holds no gate
        write_index_events()
    line = SearchTiming()
    started = time.monotonic()
    removed = None
    try:
        removed = index.sweep()
    finally:
        ended = time.monotonic()
        try:
            holds = list(index._sweep_stats.holds)
        except AttributeError:  # an index that records no steps
            holds = []
        held = sum(hold for _at, hold, _wait in holds)
        waited = sum(wait for _at, _hold, wait in holds)
        line.emit("sweep_hold", held, wait_ms=f"{waited * 1000:.3f}",
                  start_ms=f"{(holds[0][0] if holds else started) * 1000:.3f}",
                  removed=removed if isinstance(removed, int) else "-", holds=len(holds),
                  longest_ms=f"{max((hold for _at, hold, _wait in holds), default=0.0) * 1000:.3f}",
                  check_ms=f"{max(ended - started - held - waited, 0.0) * 1000:.3f}")
    return removed


# -- attribution (NR166 R0): gate sections by call site and caller, and the fast paths' outcomes ---------------------

#: Most sections or outcomes one thread keeps for an open search before it counts them as dropped instead.
_OPEN_MAX = 4096
#: Fewest seconds between two process-wide summaries from the sweeper (`emit_totals`).
TOTALS_SECONDS = 60.0
_FILES = ("main", "wal", "journal")
#: The parts of SearchVerification's two tokens after their name, in order (search_index.canonical_token, review_token).
_TOKEN_PARTS = {"canonical": ("data_version", "files"),
                "reviews": ("data_version", "files", "floor", "generation", "revision", "identity")}

_attribution_lock = threading.Lock()
_sites: dict = {}   # (site, caller, nested) -> [sections, wait total, longest wait, hold total, longest hold], seconds
_paths: dict = {}   # (path, outcome, moved) -> times
_refusals: dict = {}  # (door, reason) -> times
_events: list = []    # index events seen while this thread held the gate: (stage, seconds, fields), written later
_shares: dict = {}    # index file name -> [mode word, member count] as last seen; in memory only, never written
_WORD = re.compile(r"[a-z0-9_]{1,64}")
_SHARE_DOMAIN = b"topos-p2c-share-label/v1\x00"
_open = threading.local()  # .sections and .paths: lists while a search is open on this thread, else None
_installed = False
_totals_at: float | None = None


def attributing() -> bool:
    """Whether attribution is on: its own switch and timing's, since its lines are a search's timing lines.

    Keeps the write gate's section observer in step with the answer: installed while on, removed once off.
    """
    global _installed
    on = enabled() and switches.on(switches.SEARCH_ATTRIBUTION)
    if on != _installed:
        try:
            from topos.storage.db import write_gate
            with _attribution_lock:
                write_gate.set_section_observer(_section if on else None)
                _installed = on
        except Exception:  # noqa: BLE001 -- a gate without the hook: attribution stays off, searches go on
            return False
    return on


def _open_thread() -> None:
    _open.sections, _open.paths, _open.dropped = [], [], 0


def _close_thread() -> None:
    _open.sections = _open.paths = None


def _section(site: str, caller: str, waited: float, held: float, nested: bool) -> None:
    """The write gate's observer: one finished section. It runs under the gate, so it only counts: no line, no I/O."""
    key = (site, caller, bool(nested))
    with _attribution_lock:
        entry = _sites.get(key)
        if entry is None:
            entry = _sites[key] = [0, 0.0, 0.0, 0.0, 0.0]
        entry[0] += 1
        entry[1] += waited
        entry[2] = max(entry[2], waited)
        entry[3] += held
        entry[4] = max(entry[4], held)
    if not nested:   # this thread's running total of gate waits, for `gate_waited` (an answer job's split)
        _open.waited = getattr(_open, "waited", 0.0) + waited
    kept = getattr(_open, "sections", None)
    if kept is not None:
        if len(kept) < _OPEN_MAX:
            kept.append((site, caller, waited, held, bool(nested)))
        else:
            _open.dropped = getattr(_open, "dropped", 0) + 1


def gate_waited() -> float:
    """Seconds this thread has waited for the write gate, in sections that were not inside another, while
    attribution has been on. A running total: read it twice and subtract."""
    return getattr(_open, "waited", 0.0)


class AnswerJobTimer:
    """Attribution only (NR166 R4a): where one answer job's seconds went, as one line when it ends.

        permission_search_timing ... stage=answer_job elapsed_ms=<run to end> corr=- ... outcome=<word>
            queued_ms= retrieval_ms= writer_ms= writer_calls= judge_ms= judge_calls= gate_wait_ms=

    `queued_ms`: accepted to started, from the job's own clock (whole seconds). `retrieval_ms`: its retrievals,
    which hold or wait for the gate. `writer_ms`, `judge_ms`: inside the model calls, waiting for the host
    included. `gate_wait_ms`: this thread's waits for the write gate during the job (they lie inside
    `retrieval_ms` and the receipt's write). The parts do not add up to the whole: the rest is the job's own
    checks. Counts and durations only: no question, no id, no record.
    """

    def __init__(self):
        self.began = time.perf_counter()
        self.parts = {"retrieval": 0.0, "writer": 0.0, "judge": 0.0}
        self.calls = {"writer": 0, "judge": 0}
        self._waited = gate_waited()
        _open.job = self   # this thread's retrievals report here (`retrieval_spent`)

    def around(self, part: str, call):
        """`call` (the job's writer or judge, a coroutine function), timed. It decides and returns as before."""
        async def timed(*args, **kwargs):
            lap = time.perf_counter()
            try:
                return await call(*args, **kwargs)
            finally:
                self.parts[part] += time.perf_counter() - lap
                self.calls[part] += 1
        return timed

    def end(self, outcome, queued_seconds) -> None:
        """Write the job's line. Never raises; never called with the gate held (the receipt has committed)."""
        try:
            _open.job = None
            SearchTiming().emit(
                "answer_job", time.perf_counter() - self.began, outcome=_word(outcome or "answered"),
                queued_ms=f"{max(0.0, float(queued_seconds)) * 1000:.3f}",
                retrieval_ms=f"{self.parts['retrieval'] * 1000:.3f}",
                writer_ms=f"{self.parts['writer'] * 1000:.3f}", writer_calls=self.calls["writer"],
                judge_ms=f"{self.parts['judge'] * 1000:.3f}", judge_calls=self.calls["judge"],
                gate_wait_ms=f"{max(0.0, gate_waited() - self._waited) * 1000:.3f}")
        except Exception:  # noqa: BLE001
            pass


def answer_job_timer():
    """A timer for the answer job starting on this thread, or None while attribution is off. Never raises."""
    try:
        return AnswerJobTimer() if attributing() else None
    except Exception:  # noqa: BLE001
        return None


def retrieval_spent(seconds: float) -> None:
    """One of an answer job's retrievals took this long (search_release reports it; nothing without a job)."""
    job = getattr(_open, "job", None)
    if job is not None:
        job.parts["retrieval"] += seconds


def _moved(kept, now, prefix: str = "") -> str:
    """Which parts of two tokens differ, by name. Never a value: a token holds file states and row digests."""
    names: list[str] = []
    if isinstance(kept, dict) and isinstance(now, dict):  # N5's send token (search_index.send_token)
        for part in sorted(set(kept) | set(now)):
            if kept.get(part) != now.get(part):
                inner = _moved(kept.get(part), now.get(part), prefix=f"{part}.") if part in _TOKEN_PARTS else ""
                names.append(inner if inner and inner != "other" else part)
    elif (isinstance(kept, tuple) and isinstance(now, tuple) and kept and kept[0] in _TOKEN_PARTS
            and len(kept) == len(now) == len(_TOKEN_PARTS[kept[0]]) + 1 and kept[0] == now[0]):
        for name, before, after in zip(_TOKEN_PARTS[kept[0]], kept[1:], now[1:]):
            if before == after:
                continue
            if name == "files" and isinstance(before, tuple) and isinstance(after, tuple) and len(before) == len(after) == 3:
                names.extend(f"{prefix}file_{label}" for label, old, new in zip(_FILES, before, after) if old != new)
            else:
                names.append(prefix + name)
    return ":".join(names) or "other"


def fast_path(path: str, outcome: str, moved: str = "-") -> None:
    """Count one "nothing changed" fast path's outcome. Inert while attribution is off; never raises."""
    try:
        if not attributing():
            return
        key = (path, outcome, moved)
        with _attribution_lock:
            _paths[key] = _paths.get(key, 0) + 1
        kept = getattr(_open, "paths", None)
        if kept is not None:
            if len(kept) < _OPEN_MAX:
                kept.append(key)
            else:
                _open.dropped = getattr(_open, "dropped", 0) + 1
    except Exception:  # noqa: BLE001
        pass


def reuse(path: str, hit: bool, kept=None, now=None, *, first: bool = False, ignore: tuple = (),
          absent: str = "not_kept") -> None:
    """A fast path that compares a kept token with the one read now: a hit, a search's first computation, or a miss
    with the parts that moved (`ignore`: parts of a send token the comparison leaves out; `absent`: what a missing
    `kept` means). The tokens are only compared here; nothing of them is stored or written."""
    try:
        if not attributing():
            return
        if hit:
            fast_path(path, "hit")
        elif first:
            fast_path(path, "first")
        elif now is None:
            fast_path(path, "miss", "unreadable")
        elif kept is None:
            fast_path(path, "miss", absent)
        else:
            if ignore and isinstance(kept, dict) and isinstance(now, dict):
                kept = {part: value for part, value in kept.items() if part not in ignore}
                now = {part: value for part, value in now.items() if part not in ignore}
            fast_path(path, "miss", _moved(kept, now))
    except Exception:  # noqa: BLE001
        pass


def _word(value) -> str:
    """A bounded machine word, or `other`: a refusal's code is a literal in the code, never data."""
    return value if isinstance(value, str) and _WORD.fullmatch(value) else "other"


def refusal(door: str, error, timing=None) -> None:
    """Why a request was refused, as one fixed word in the node's own log. Inert while attribution is off.

    `error` is the exception the door caught. A release that refused after admission answers `permission_denied`
    whatever the cause; the adapter leaves the cause on that exception as `reason`, and it is read here and
    nowhere else. `timing`: the search's own timing, so the line joins its other lines.
    """
    try:
        if not attributing():
            return
        reason = _word(getattr(error, "reason", None) or getattr(error, "code", None))
        key = (_word(door), reason)
        with _attribution_lock:
            _refusals[key] = _refusals.get(key, 0) + 1
        line = timing if isinstance(timing, SearchTiming) else SearchTiming()
        line.emit("refusal", 0.0, door=key[0], reason=reason)
    except Exception:  # noqa: BLE001
        pass


def share_label(path) -> str:
    """Eight hex characters naming one share's index in attribution lines: not its grant id, not its file name."""
    return hashlib.sha256(_SHARE_DOMAIN + Path(path).name.encode("utf-8")).hexdigest()[:8]


def share_seen(path, *, mode=None, members=None) -> None:
    """Remember a share's mode word and member count for its later index lines. Inert while attribution is off.

    Kept in memory by the index file's name, which is never written: a line names the share by `share_label`."""
    try:
        if not attributing():
            return
        with _attribution_lock:
            if Path(path).name not in _shares and len(_shares) >= _OPEN_MAX:
                return
            known = _shares.setdefault(Path(path).name, ["unknown", None])
            if mode in ("only", "with_sources", "records"):
                known[0] = mode
            if isinstance(members, int) and not isinstance(members, bool) and members >= 0:
                known[1] = members
    except Exception:  # noqa: BLE001
        pass


def index_event(stage: str, path, seconds: float = 0.0, **fields) -> None:
    """A share's index went stale (`index_stale`, with the class), was removed (`index_removed`) or was rebuilt
    (`index_rebuild`, with its state and member count). Inert while attribution is off. A line is never written
    by a thread that holds the write gate: such an event waits for the next one that does not, or the sweeper."""
    try:
        if not attributing() or stage not in ("index_stale", "index_removed", "index_rebuild"):
            return
        extra = {"state": _word(fields["state"])} if "state" in fields else {}
        if "reason" in fields:
            extra["class"] = _word(fields["reason"])
        for name in ("sub", "scope"):   # NR188: what moved, in a fixed word, and whether only members were withheld
            if name in fields:
                extra[name] = _word(fields[name])
        counted = fields.get("members")
        if stage == "index_rebuild" and extra.get("state") == "ready":
            share_seen(path, members=counted)
        with _attribution_lock:
            mode, members = _shares.get(Path(path).name, ("unknown", None))
        if stage == "index_rebuild" and isinstance(counted, int) and not isinstance(counted, bool):
            members = counted   # the count this build ended with
        event = (stage, seconds, dict(share=share_label(path), mode=mode, members="-" if members is None else members,
                                      at_ms=f"{time.monotonic() * 1000:.3f}", **extra))
        with _attribution_lock:
            if len(_events) < _OPEN_MAX:
                _events.append(event)
        write_index_events()
    except Exception:  # noqa: BLE001
        pass


def write_index_events() -> None:
    """Write the index events that are waiting, unless this thread holds the gate."""
    try:
        from topos.storage.db.write_gate import db_write_lock
        if not _events or db_write_lock()._is_owned():
            return
        with _attribution_lock:
            waiting = list(_events)
            _events.clear()
        line = SearchTiming()
        for stage, seconds, fields in waiting:
            line.emit(stage, seconds, **fields)
    except Exception:  # noqa: BLE001
        pass


def totals(*, reset: bool = False) -> dict:
    """The process-wide tables since the last reset: `sites`, `paths` and `refusals`, keyed as the module's are."""
    with _attribution_lock:
        found = {"sites": {key: tuple(value) for key, value in _sites.items()}, "paths": dict(_paths),
                 "refusals": dict(_refusals)}
        if reset:
            _sites.clear()
            _paths.clear()
            _refusals.clear()
    return found


def emit_totals(*, every: float = TOTALS_SECONDS) -> bool:
    """Write the process-wide tables as `gate_site` and `fast_path_total` lines and start them again.

    At most once per `every` seconds, and never from a thread that holds the gate. True when it wrote.
    """
    global _totals_at
    try:
        if not attributing():
            return False
        from topos.storage.db.write_gate import db_write_lock
        now = time.monotonic()
        if db_write_lock()._is_owned() or (_totals_at is not None and now - _totals_at < every):
            return False
        window, _totals_at = (0.0 if _totals_at is None else now - _totals_at), now
        found = totals(reset=True)
        line = SearchTiming()
        for (site, caller, nested), (count, waited, longest_wait, held, longest) in sorted(found["sites"].items()):
            line.emit("gate_site", held, site=_token(site, 160), caller=_token(caller, 160), nested=int(nested), n=count,
                      wait_ms=f"{waited * 1000:.3f}", longest_ms=f"{longest * 1000:.3f}",
                      longest_wait_ms=f"{longest_wait * 1000:.3f}", window_ms=f"{window * 1000:.3f}")
        for (path, outcome, moved), count in sorted(found["paths"].items()):
            line.emit("fast_path_total", 0.0, path=path, outcome=outcome, moved=_token(moved, 160), n=count,
                      window_ms=f"{window * 1000:.3f}")
        for (door, reason), count in sorted(found["refusals"].items()):
            line.emit("refusal_total", 0.0, door=door, reason=reason, n=count, window_ms=f"{window * 1000:.3f}")
        return True
    except Exception:  # noqa: BLE001
        return False
