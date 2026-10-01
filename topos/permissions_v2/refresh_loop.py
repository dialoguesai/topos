"""Node-side refresh of permitted-set search: plan WS7 items RD4/N7 and RD2. Off by default.

Without this, new messages reach a grant only after an owner action. The owner starts
machine assessment over a bounded window, and a grant index is rebuilt only by owner hooks.
The 10 s daemon sweep deletes an index that drifted and never rebuilds it, so the grant
refuses (`search_index_missing`) until the owner acts, and the rolling window slowly empties.
Two parts, each behind its own flag:

``TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED`` (N7). Restores a grant index that a drift
dropped. MESSAGE_SEARCH.md approved it with four conditions, met here as follows:

1. It runs only to restore an index a drift dropped, never on its own timer. ``observe`` runs
   after each daemon sweep and queues a grant only when an index file that was published has
   gone while the grant is still an active search grant. The last published set is kept on
   disk, so a drop across a restart is still a drop. A grant that never had an index is
   never built here; the owner hooks build it. One addition (WS0, 1 Oct): a change of the
   browsing interests a grant signs moves no index basis, so no drift would ever drop that
   index; such a change queues it here too (cause ``interest_changed``, IF-5 I8 below). A
   second (IF-6 §10): a change of the node's facts does not either (cause ``facts_changed``,
   below).
2. It re-evaluates the already-signed policy with the unchanged decision function: it calls
   the owner hooks' own ``SearchIndexService.rebuild``, which reads only the ledger's authority.
   First it syncs the node's protection revision the way recipient admission does
   (:func:`protection_sync`), because after a protection clock move every rebuild is otherwise
   `stale`. That changes no policy: an envelope signed before the move still refuses at
   admission (`authority_binding`) until the owner's grant Sync, and the index is ready then.
3. It is coalesced and rate-limited. Drops within ``debounce`` share one pass, passes start at
   least ``min_interval`` apart, and a failed restore backs off exponentially for at most
   ``max_attempts`` tries. After that the grant stays dark until the owner acts, which is the
   behaviour without this module. A running assessment pass defers them. The owner's pass
   defers them up to ``max_defer``. The node's own pass (below) defers them until it ends,
   because each assessment it publishes moves the review digest, so a rebuild started under it
   can only end `stale` and back off: measured, that pushed the restore after a 500-call pass
   back by the backoff, and a run of such passes could spend every attempt. Its end restores at
   once. A node pass that has published nothing for ``max_defer`` (stalled, or only scanning
   current rows) no longer defers them.
4. It builds on a read snapshot outside the write gate (the rebuild's merge-gate path) and
   writes a ``topos-node-system-action/v1`` receipt to the ledger with its cause classes.
   Receipts hold counts, grant ids and policy hashes, never a record, name or reason text.

``TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED`` (RD2). Keeps the window of every active
p2c-v3 grant assessed as it rolls, for every enabled evidence family. A check runs at most once
per ``catchup_interval`` and starts at most one pass, the first of these that applies:

1. ``startup_backlog``: no full pass has completed here (an install, or a lost state file).
2. ``revision_change``: the rules an assessment is current under (:func:`assessment_revisions`:
   each enabled family's rubric revision, the context versions, the model revision, the journal
   floors, with the interest flag the interest label rubric, and with the derived-facts flag the
   rule that a journal review carries the model's own label (IF-6 v1b)) differ from the ones the last
   completed full pass ran under. An install that changes a rule stales every assessment it
   touches (OD-54 staled every AI-chat assessment, and the grant's AI-chat rows went from 31
   releasable to 0 until a manual pass), so this full pass runs at once, not at night.
3. ``proof_change``: what makes a row provable or eligible moved with no new ingest
   (:func:`proof_digest`: capture receipts, identity attestations, source installs and posture
   overrides, native enrollments, the protection clock). A capture receipt proves rows ingested
   long ago, and an Off-limits edit changes every assessment's context; the ingest high-water
   mark sees neither. A full pass at once.
4. ``budget_continuation``: the last pass stopped at its model budget. The same scope again,
   from the slice it stopped in (below), one ``catchup_interval`` after it ended so that the
   restored index serves in between, until a pass ends within budget. It runs under the rules
   and proof its first pass ran under; a change of either starts a new pass of cause 2 or 3
   instead.
5. ``daily_reconciliation``: the whole window, at least ``full_interval`` apart, and only inside
   ``full_hours``.
6. ``new_ingest``: the conversations (and journal entries) that received rows since the last
   pass, because a new row changes its neighbours' context revision.

The revisions and proof digest the last full pass ran under, and an owed continuation, are kept
in the state file beside the published set, so a restart neither loses them nor re-runs a pass
for a rule that did not change. A pass of cause 2 or 3 does not count as the nightly one. Passes
never rebuild an index themselves: each new assessment moves the review digest, the sweep drops
the index as drift, and the restore above rebuilds it once the pass is idle. Every pass makes at
most ``max_assessed`` model calls (owner decision OD-12 sets the budget).

A pass of either scope covers the whole window of the widest active knowledge grant. The worker
refuses a window wider than 31 days, a guard that stays, so the pass walks it in adjacent slices
of at most 31 days, newest first, one worker run each (:func:`window_slices`). Before this every
pass covered only the newest 31 days: on a 90-day grant, rows 31-90 days old that a capture
receipt proved, a rule change staled or an install brought stayed unassessed (WS0, 1 Oct). The
slices of a pass share its budget. The next slice starts in the tick the previous one ends, so
neither the restore nor the interest refresh sees the pass end between them. The state above
moves only when the last slice ends within budget: a restart mid-pass marks nothing done and the
next check plans again. A pass that stops at its budget in an older slice owes that slice and
every older one as planned (the continuation's ``slices``), and when the last of them ends within
budget the ingest high-water mark becomes the time the window's newest slice was walked
(``high_water``), so ``new_ingest`` re-checks the rows that arrived since. One receipt per pass,
its slices' counts summed, its window the slices it planned.

With ``TOPOS_PERMISSIONS_V2_INTEREST_SOURCES`` also on (IF-5 Q&A I8), the browsing-interest
objects are kept stored and their labels assessed: after a pass, inside what that pass left of
its budget, or once per ``interest_interval`` when nothing else ran. The objects are built on a
read snapshot and stored by ``interest_family.persist`` in one short write under the gate. The
labels with no current assessment (``interest_review.pending``) are assessed one model call at a
time with no lock held, each published by ``interest_review.publish`` under the gate against the
protected vocabulary current at that moment. That is ``interest_review.assess_pending``'s own
sequence, run by its parts because it holds one connection across its model calls, which from a
background thread means taking SQLite's write lock outside the gate. When a run stored, closed or
labelled anything, the grants that sign interests and have an index here are queued for a rebuild
on the restore's own queue (cause ``interest_changed``; its debounce, interval, deferral and
backoff), because a new interest or a newly assessed label moves no index basis, so no drift
would ever drop the index (WS0, 1 Oct, on Lane C's finding). With the flag off nothing here
touches the interest family.

A cluster whose own label is a bad name for a grant (it names a site, echoes a page title, or is
not a short topic name) and which nothing explicitly excludes is given a second try at a label in
the same refresh (``interest_relabel``; owner direction, 1 Oct): after the labels owed an
assessment, inside the same budget, at most ``interest_relabel.RETRIES`` model calls per cluster
label in total. Each call runs with no lock held, and each answer is judged and stored in one
short write under the gate, against the rows current at that moment. When a label is accepted the
objects are built and stored again and the new labels assessed, still inside the budget. The
second tries are model calls a dark grant would wait for, so when a restore is owed they wait
once for the next round, which follows that restore. ``TOPOS_PERMISSIONS_V2_INTEREST_RELABEL``
set off turns the second tries off: the refresh is then what it was without them, and its
receipt carries none of their counts.

With ``TOPOS_PERMISSIONS_V2_DERIVED_FACTS`` also on (IF-6 §10; inert without the journal family), a fact the
extractor writes after a grant's index was built (the derivation pass lags the ingest, or a re-derivation closes
and replaces a fact on an already-indexed entry) moves no index basis and no member row, so no drift would drop
the index and the fact would wait for an unrelated rebuild. ``observe`` therefore keeps a cheap digest of the
facts (:func:`fact_digest`: their count, the highest rowid, the latest ``valid_from`` and ``valid_to``), kept in
the state file so a restart compares with the last one seen; when it moves, the active knowledge grants that could
release an inferred fact (v1: they sign ``journal_entry`` and ``fact``) and have an index here are queued on the
restore's own queue, cause ``facts_changed`` (its debounce, interval, deferral and backoff). A grant whose rebuild
is running when its facts move again stays queued for one more rebuild, so a fact written behind a build's
snapshot is not dropped with the finished entry. With the flag off nothing is read or queued.

Both act as the node's own process for its owner, the precedent of
``Runtime.ensure_evidence_reviews``. Nothing here changes what a recipient can receive: a
record still leaves the node only if the per-candidate re-decision permits it at read time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, get_args

from pydantic import model_serializer

from .canonical import PolicyError
from .contract import Hash, Identifier, Number, StrictModel
from .opaque_ids import private_file
from .search_contract import CAPABILITY_KNOWLEDGE_SEARCH, SEARCH_CAPABILITIES

_log = logging.getLogger(__name__)

RESTORE_FLAG = "TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED"
CATCHUP_FLAG = "TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED"
MIN_INTERVAL_ENV = "TOPOS_PERMISSIONS_V2_INDEX_RESTORE_MIN_INTERVAL_SECONDS"
BUDGET_ENV = "TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_MAX_PER_PASS"
STATE_FILE = "refresh-state.json"
# Unchanged by the keys added after v1 (revisions, proof, continuation, a continuation's slices): each is
# optional, a file without one loads with it empty, and an older loop reading a newer file ignores them.
STATE_VERSION = "topos-search-refresh-state/v1"
RECEIPT_VERSION = "topos-node-system-action/v1"
PROOF_VERSION = "topos-refresh-proof/v1"
FACTS_VERSION = "topos-refresh-facts/v1"
# The assessment worker's own bound on one run (AutomaticReviewWorker._launch refuses a wider window, as it does
# for an owner-started pass; that refusal is a guard and stays). A wider grant window is walked in slices of at
# most this, newest first, one worker run each (window_slices).
MAX_SLICE_SECONDS = 31 * 86400
# A worker run's counts, summed over the slices of one pass into its receipt.
_COUNTS = ("scanned", "assessed", "current", "withheld", "unresolved")
# The worker gives up a pass after this many model failures in a row; the interest labels do too.
MAX_CONSECUTIVE_FAILURES = 3

CauseClass = Literal["review_changed", "protection_changed", "context_changed", "restart_gap", "interest_changed",
                     "facts_changed"]
RestoreState = Literal["ready", "over_cap", "removed", "stale", "failed"]
CatchUpCause = Literal["startup_backlog", "daily_reconciliation", "new_ingest", "revision_change", "proof_change",
                       "budget_continuation"]
Scope = Literal["full_window", "changed_conversations"]
# The full passes whose completion counts as the nightly reconciliation. A continuation counts as
# its first pass would; a pass for a rule or proof change does not move the nightly schedule.
RECONCILING = frozenset({"startup_backlog", "daily_reconciliation"})


class RestoredGrant(StrictModel):
    grant_id: Identifier
    policy_hash: Hash | None
    state: RestoreState
    member_count: Number


class RestoreReceipt(StrictModel):
    version: Literal["topos-node-system-action/v1"]
    action: Literal["search_index_restore"]
    actor: Literal["node_system"]
    cause_classes: list[CauseClass]
    first_drop_at: Number
    started_at: Number
    finished_at: Number
    protection_synced: bool        # the sync before the rebuilds moved the node's protection revision
    grants: list[RestoredGrant]


class CatchUpReceipt(StrictModel):
    """One per pass: the counts are its slices' summed, the window spans the slices it planned."""
    version: Literal["topos-node-system-action/v1"]
    action: Literal["message_assessment_catchup"]
    actor: Literal["node_system"]
    cause_class: CatchUpCause
    scope: Scope
    window_after: Number
    window_before: Number
    started_at: Number
    finished_at: Number
    state: Literal["complete", "cancelled", "failed"]
    scanned: Number
    assessed: Number
    current: Number
    withheld: Number
    unresolved: Number
    budget_exhausted: bool


class InterestRefreshReceipt(StrictModel):
    """IF-5 Q&A I8: browsing-interest objects stored and their labels assessed. Counts only, never a label."""
    version: Literal["topos-node-system-action/v1"]
    action: Literal["interest_refresh"]
    actor: Literal["node_system"]
    cause_class: Literal["after_pass", "interval"]
    started_at: Number
    finished_at: Number
    state: Literal["complete", "cancelled", "failed"]
    inserted: Number      # interest_family.persist: objects stored anew
    closed: Number        # ...closed because they changed or no longer qualify
    unchanged: Number
    pending: Number       # labels with no current assessment (interest_review.pending)
    assessed: Number      # labels assessed and published
    unresolved: Number    # model calls that failed, or whose publication the vocabulary refused
    budget: Number        # model calls allowed: what the pass before left, or OD-12's budget
    budget_exhausted: bool
    rebuild_requested: Number   # grants that sign interests queued for a rebuild because interests changed
    # interest_relabel: a second try at a label that is a bad name. None, and absent from the stored receipt, when
    # the refresh ran without it (its switch off) and in every receipt written before it.
    relabel_pending: Number | None = None   # clusters owed a second label when the refresh began
    relabel_calls: Number | None = None     # model calls made for them
    relabelled: Number | None = None        # second labels every label check accepted

    @model_serializer(mode="wrap")
    def _without_counts_that_were_not_taken(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for key in ("relabel_pending", "relabel_calls", "relabelled"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data


def _flag(name: str, env) -> bool:
    return env.get(name, "").lower() == "true"


def _seconds(name: str, env, default: int, *, floor: int) -> int:
    raw = env.get(name, "")
    if not raw.isdecimal():
        return default
    return max(int(raw), floor)


@dataclass(frozen=True)
class RefreshSettings:
    restore: bool = False
    catchup: bool = False
    debounce: float = 30.0
    min_interval: float = 300.0       # OD-11: the coalescing interval
    max_defer: float = 600.0
    backoff: float = 300.0
    max_backoff: float = 3600.0
    max_attempts: int = 8
    catchup_interval: float = 300.0
    full_interval: float = 72000.0   # at least 20 h between full passes...
    # ...and only inside this local-time window, because every new assessment darkens the grant
    # until the restore that follows the pass. None runs one whenever full_interval has passed.
    full_hours: tuple[int, int] | None = (2, 6)
    max_assessed: int = 500           # OD-12: local-model calls per pass
    tick: float = 5.0
    interests: bool = False           # IF-5 I8: TOPOS_PERMISSIONS_V2_INTEREST_SOURCES, with catch-up on
    relabels: bool = False            # interest_relabel: with interests, unless its switch is set off
    interest_interval: float = 3600.0
    facts: bool = False               # IF-6 §10: TOPOS_PERMISSIONS_V2_DERIVED_FACTS (journal family on), restore on

    @classmethod
    def from_env(cls, env=None) -> "RefreshSettings":
        env = os.environ if env is None else env
        restore = _flag(RESTORE_FLAG, env)
        # Catch-up without restore would drop every grant index on its first new assessment and
        # leave it dark, so it only runs with restore on.
        catchup = restore and _flag(CATCHUP_FLAG, env)
        interests = relabels = False
        if catchup:
            # The interest index's own reading of its flag, so the two never disagree about it.
            from .interest_index import enabled as interest_sources_enabled
            from .interest_relabel import enabled as relabel_enabled
            interests = interest_sources_enabled(env)
            relabels = interests and relabel_enabled(env)
        facts = False
        if restore:
            # The projection's own reading of its flag (and of the journal family it needs), so they never disagree.
            from .inferred_facts import enabled as derived_facts_enabled
            facts = derived_facts_enabled(env)
        return cls(restore=restore, catchup=catchup,
                   min_interval=float(_seconds(MIN_INTERVAL_ENV, env, 300, floor=60)),
                   max_assessed=_seconds(BUDGET_ENV, env, 500, floor=1), interests=interests, relabels=relabels,
                   facts=facts)

    @property
    def enabled(self) -> bool:
        return self.restore or self.catchup


def assessment_revisions(*, interests: bool = False, env=None) -> dict:
    """The rules a machine assessment is current under (``automatic_message_review.is_current``), for the
    families enabled now. Version strings and hashes only. Raises what the pinned rubric's read raises."""
    from .automatic_message_review import (CONTEXT_VERSIONS, JOURNAL_CONTEXT, JOURNAL_FLOORS_VERSION,
                                           JOURNAL_MODEL_LABEL_VERSION, MODEL_REVISION, rubric_revision_for)
    from .inferred_facts import enabled as derived_facts_enabled
    from .evidence_families import enabled_tables
    tables = enabled_tables(env)
    revisions = {"model": MODEL_REVISION, "rubric": {table: rubric_revision_for(table) for table in tables},
                 "context": {table: CONTEXT_VERSIONS[table] for table in tables if table in CONTEXT_VERSIONS}}
    if "journal_entries" in tables:
        revisions["context"]["journal_entries"] = dict(JOURNAL_CONTEXT)
        revisions["journal_floors"] = JOURNAL_FLOORS_VERSION
        if derived_facts_enabled(env):
            # IF-6 v1b: a journal review must carry the model's own protected label (`is_current`); turning the
            # flag on is a rule change, so the catch-up re-assesses the journal reviews published without one.
            revisions["journal_model_label"] = JOURNAL_MODEL_LABEL_VERSION
    if interests:
        from .interest_review import rubric_revision as interest_rubric_revision
        revisions["interest_rubric"] = interest_rubric_revision()
    return revisions


def _columns(conn, table: str) -> set:
    found = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchmany(2)
    if len(found) != 1 or found[0][0] != "table":
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _sha256(value) -> str | None:
    return None if value is None else hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()


# (part, table, columns it needs, query). Ids, counts, states and times only; constant SQL. A table
# that is absent, or lacks a column, contributes nothing (a node without the feature has no such proof).
_PROOF_QUERIES = (
    # OD-50/OD-52 receipts (journal, browser visits, AI-chat export imports) and OD-39's AI-chat capture
    # receipts: append-only, so the listing changes exactly when one is attested or revoked.
    ("capture_receipts", "capture_receipts",
     {"owner_id", "canonical_table", "receipt_id", "row_count", "attested_at", "revoked_at"},
     "SELECT canonical_table, receipt_id, row_count, attested_at, revoked_at FROM capture_receipts "
     "WHERE owner_id=? ORDER BY canonical_table, receipt_id"),
    ("ai_chat_capture_receipts", "ai_chat_capture_receipts",
     {"owner_id", "source_id", "receipt_id", "row_count", "attested_at", "revoked_at"},
     "SELECT source_id, receipt_id, row_count, attested_at, revoked_at FROM ai_chat_capture_receipts "
     "WHERE owner_id=? ORDER BY source_id, receipt_id"),
    # The owner's identity attestations: an append-only ledger keyed by its sequence.
    ("identity_attestations", "permissions_v2_identity_attestations", {"sequence"},
     "SELECT COUNT(*), MAX(sequence) FROM permissions_v2_identity_attestations"),
    # Posture overrides per dataset, which evidence._source_posture reads beside the installs.
    ("posture_overrides", "user_ingestion_sources", {"source_id", "dataset_id", "posture"},
     "SELECT source_id, dataset_id, posture FROM user_ingestion_sources ORDER BY source_id, dataset_id"),
    # Native iMessage enrollments: a recovery adds one, a refresh (RD8) moves its revision.
    ("native_enrollments", "ingest_provenance_enrollments", {"enrollment_id", "revision", "state", "source_generation"},
     "SELECT enrollment_id, revision, state, source_generation FROM ingest_provenance_enrollments "
     "ORDER BY enrollment_id"),
)
# Source clock v2's own list (ingest_provenance._WATCHED_UPDATE_COLUMNS): what an install row's posture rests on,
# never a sync receipt (last_sync_at, updated_at).
_INSTALL_COLUMNS = ("source_id", "install_id", "is_active", "status", "scope_key", "source_definition_json")


def proof_digest(conn, *, owner_id: str) -> str:
    """What makes a row provable or eligible, as one digest. ``conn`` is one read snapshot of the canonical
    database; nothing it reads is a row of evidence, and only the digest leaves here."""
    from .protection_clock import clock_state
    hasher = hashlib.sha256(PROOF_VERSION.encode("ascii"))

    def part(name, value):
        hasher.update(json.dumps([name, value], sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))

    for name, table, needed, sql in _PROOF_QUERIES:
        if needed <= _columns(conn, table):
            part(name, [list(row) for row in conn.execute(sql, (owner_id,) if "?" in sql else ())])
    installs = _columns(conn, "source_runtime_installs")
    if {"source_id", "is_active", "status"} <= installs:
        present = [column for column in _INSTALL_COLUMNS if column in installs]
        rows = conn.execute(f"SELECT {', '.join(present)} FROM source_runtime_installs").fetchall()
        hashed = [[_sha256(value) if column == "source_definition_json" else value
                   for column, value in zip(present, row)] for row in rows]
        # Every install row, retired ones too: ai_chat_capture.install_dataset reads them all.
        part("source_installs", [present, sorted(hashed, key=lambda row: json.dumps(row, default=str))])
    try:
        # Off-limits, owner-only marks, exclusions, attestations and native publication move it.
        part("protection_clock", list(clock_state(conn)))
    except PolicyError:
        part("protection_clock", None)
    return hasher.hexdigest()


def fact_digest(conn) -> str:
    """IF-6 §10: the node's facts, as one cheap digest. Their count, the highest rowid, the latest ``valid_from`` and
    the latest ``valid_to``: a new fact and a closed one move it, an edit in place does not (its row digest is the
    index member's own check). Raises what the read raises; the caller then queues nothing."""
    row = conn.execute("SELECT COUNT(*), MAX(rowid), MAX(valid_from), MAX(valid_to) FROM signal_objects "
                       "WHERE object_type='fact'").fetchone()
    return hashlib.sha256(json.dumps([FACTS_VERSION, list(row)], default=str).encode("utf-8")).hexdigest()


def protection_sync(protocol) -> Callable[[], bool]:
    """Recipient admission's own protection bookkeeping (``NodePolicyProtocol.admit``), run before a restore.

    A protection clock move (Off-limits, owner-only marks, exclusions, an identity attestation, a native
    publication) leaves the ledger's revision behind, and a rebuild against it is always `stale`. The sync is
    the one a recipient's next request would make anyway. True when it moved the revision.
    """
    def sync() -> bool:
        with protocol.ledger._transaction() as db:
            before = protocol.ledger._node(db)["protection_revision"]
            protocol._sync_protection(db)
            return protocol.ledger._node(db)["protection_revision"] != before
    return sync


@contextmanager
def node_principal(owner_id: str):
    """The node's own process acting for its owner on its own socket (see ensure_evidence_reviews)."""
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=owner_id))
    try:
        yield
    finally:
        reset_principal(token)


def window_slices(after: int, before: int) -> list[list[int]]:
    """[after, before] as adjacent slices of at most MAX_SLICE_SECONDS, newest first: one worker run each. The
    worker reads a window closed at both ends, so a row on a shared bound is read by both slices (current the
    second time, no model call) and none falls between them."""
    slices, end = [], before
    while end > after:
        start = max(end - MAX_SLICE_SECONDS, after)
        slices.append([start, end])
        end = start
    return slices


def _valid_resume(slices, high_water) -> bool:
    """A continuation's slices. None walks the whole window again: a file written before slices, or a pass that
    stopped in the window's newest slice. Otherwise the slices still owed, adjacent and newest first, each one the
    worker takes, none newer than the high-water mark the chain leaves."""
    if slices is None:
        return high_water is None
    if type(high_water) is not int or not isinstance(slices, list) or not slices:
        return False
    bound = high_water
    for n, pair in enumerate(slices):
        if not (isinstance(pair, list) and len(pair) == 2 and all(type(value) is int for value in pair)):
            return False
        after, before = pair
        if not 0 <= after < before <= bound or before - after > MAX_SLICE_SECONDS or (n and before != bound):
            return False
        bound = after
    return True


class RefreshLoop:
    """One per runtime. `observe` is called by the daemon sweep; the rest runs on its own thread."""

    def __init__(self, *, ledger, root: Path, index: Callable[[], object], worker: Callable[[], object] | None,
                 settings: RefreshSettings, clock: Callable[[], float] = time.time,
                 sync_protection: Callable[[], bool] | None = None):
        self.ledger = ledger
        self.root = Path(root)
        self._index, self._worker = index, worker
        self._sync_protection = sync_protection   # protection_sync(protocol) on a node; condition 2
        self._worker_cache = None
        self.settings = settings
        self.clock = clock
        self.owner_id = ledger.identity.owner_id
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._names: set[str] | None = None       # index files seen at the last observation
        self._signals: tuple | None = None        # (review digest, protection clock) when last settled
        self._pending: dict[str, dict] = {}       # grant id -> causes, attempts, not_before, first_drop_at
        self._last_restore_at: float | None = None
        self._last_catchup_check: float | None = None
        self._pass: dict | None = None            # the node's running assessment pass
        self._pass_ended = False                  # a pass just finished: its drops are all in
        self._state: dict | None = None
        self._interest_after_pass: tuple | None = None   # (budget the pass left, was it a full pass)
        self._last_interest_at: float | None = None
        self._relabels_waited = False             # the last refresh held its second tries back for a restore
        self._facts: str | None = None            # IF-6: the fact digest when last observed

    # -- persisted state -----------------------------------------------------

    def _state_path(self) -> Path:
        return self.root / STATE_FILE

    def _load_state(self) -> dict:
        if self._state is None:
            state = {"version": STATE_VERSION, "names": [], "ingest_high_water": None, "last_full_pass_at": None,
                     "assessment_revisions": None, "proof_digest": None, "continuation": None, "fact_digest": None}
            try:
                loaded = json.loads(self._state_path().read_text("utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == STATE_VERSION:
                    state.update({key: loaded.get(key) for key in state if key != "version"})
                    if not isinstance(state["names"], list):
                        state["names"] = []
                    if not isinstance(state["assessment_revisions"], dict):
                        state["assessment_revisions"] = None
                    if not isinstance(state["proof_digest"], str):
                        state["proof_digest"] = None
                    if not isinstance(state["fact_digest"], str):
                        state["fact_digest"] = None
                    if not self._valid_continuation(state["continuation"]):
                        state["continuation"] = None
            except (OSError, ValueError):
                pass
            self._state = state
        return self._state

    @staticmethod
    def _valid_continuation(owed) -> bool:
        if not isinstance(owed, dict) or owed.get("origin") not in get_args(CatchUpCause):
            return False
        if not _valid_resume(owed.get("slices"), owed.get("high_water")):
            return False
        after = owed.get("ingested_after")
        if owed.get("scope") == "full_window":
            return after is None
        return owed.get("scope") == "changed_conversations" and type(after) is int and after >= 0

    def _save_state(self) -> None:
        path = self._state_path()
        temporary = path.with_name("." + STATE_FILE + "." + os.urandom(6).hex())
        private_file(temporary)
        try:
            temporary.write_text(json.dumps(self._state, sort_keys=True), "utf-8")
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _persist_names(self, names: set[str]) -> None:
        """Keep on disk every index that is published or still owed a restore.

        A dropped index stays listed until it is restored, its grant stops being an active search
        grant, or its restore gives up. A restart between a drop and its restore then still sees
        the drop (cause `restart_gap`) instead of forgetting a grant that had an index.
        """
        from .search_index import index_path
        owed = {index_path(self.root, grant_id).name for grant_id in self._pending}
        wanted = sorted(set(names) | owed)
        state = self._load_state()
        if wanted != sorted(state["names"]):
            state["names"] = wanted
            self._save_state()

    # -- N7: restore an index a drift dropped ------------------------------

    def observe(self, service) -> None:
        """After every daemon sweep, with the sweep's own index service. Queues a restore only
        for a published index that has gone; costs a directory listing when nothing moved."""
        if not self.settings.restore:
            return
        try:
            self._observe_facts(service)
        except Exception as exc:  # noqa: BLE001 -- the drop check below still runs; the move is seen next sweep
            _log.warning("fact observation failed (%s)", type(exc).__name__)
        names = {path.name for path in self.root.glob("grant-*.db")}
        with self._lock:
            state = self._load_state()
            restart = self._names is None
            previous = set(state["names"]) if restart else self._names
            self._names = names
            gone = previous - names
            if not gone:
                if restart or names - previous:
                    self._signals = self._current_signals(service)
                self._persist_names(names)
                return
            causes = self._causes(service, restart)
            now = self.clock()
            for grant_id in self._dropped_grants(int(now), gone):
                entry = self._pending.setdefault(grant_id, {"causes": set(), "attempts": 0, "not_before": 0.0,
                                                            "first_drop_at": now})
                entry["causes"] |= causes
            self._persist_names(names)
        self._wake.set()

    def after_sweep(self, service) -> None:
        """The sweeper's hook. Never raises: an exception here would end the sweep thread."""
        try:
            self.observe(service)
        except Exception as exc:  # noqa: BLE001 -- class name only
            _log.warning("search refresh observation failed (%s)", type(exc).__name__)

    def _current_signals(self, service) -> tuple | None:
        from .protection_clock import clock_state
        try:
            conn = sqlite3.connect(Path(service.resolver.path).as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                clock = tuple(clock_state(conn))
            finally:
                conn.close()
            return service.reviews.current_authority_digest(), clock
        except Exception:  # noqa: BLE001 -- a cause class is diagnosis only
            return None

    # -- IF-6 §10: facts the extractor wrote after a build -----------------

    def _observe_facts(self, service) -> None:
        """Queue the grants that could release an inferred fact when the facts moved since the last observation
        (the last one recorded in the state file across a restart). The first observation only records."""
        if not self.settings.facts:
            return
        digest = self._fact_digest(service)
        if digest is None:
            return
        with self._lock:
            before = self._facts if self._facts is not None else self._load_state().get("fact_digest")
        if before is not None and before != digest:
            self._request_fact_rebuilds(self.clock())   # if this raises, the next sweep sees the same move
        with self._lock:
            self._facts = digest
            state = self._load_state()
            if state.get("fact_digest") != digest:
                state["fact_digest"] = digest
                self._save_state()

    @staticmethod
    def _fact_digest(service) -> str | None:
        try:
            conn = sqlite3.connect(Path(service.resolver.path).as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                return fact_digest(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 -- no digest, no trigger
            _log.warning("fact state unreadable (%s)", type(exc).__name__)
            return None

    def _fact_grants(self, now: int) -> list[str]:
        """Active knowledge grants that could release an inferred fact (IF-6 v1: they sign `journal_entry` and
        `fact`) and have an index here, published or owed. A grant that never had an index is still never built
        here: the owner hooks build it."""
        from .search_index import index_path
        names = {path.name for path in self.root.glob("grant-*.db")}
        with self._lock:
            owed = set(self._pending)
        return [grant_id for grant_id, _authority, policy in self._active_grants(now)
                if policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH
                and {"journal_entry", "fact"} <= set(policy.search.result_types)
                and (index_path(self.root, grant_id).name in names or grant_id in owed)]

    def _request_fact_rebuilds(self, now: float) -> int:
        """Queue them on the restore's own queue, cause `facts_changed`. A grant whose rebuild is running now is
        kept for one more rebuild: that build's snapshot may predate the facts that moved."""
        grants = self._fact_grants(int(now))
        with self._lock:
            for grant_id in grants:
                entry = self._pending.setdefault(grant_id, {"causes": set(), "attempts": 0, "not_before": 0.0,
                                                            "first_drop_at": now})
                entry["causes"].add("facts_changed")
                if entry.get("running"):
                    entry["again"] = True
        if grants:
            self._wake.set()
        return len(grants)

    def _causes(self, service, restart: bool) -> set[str]:
        if restart:
            return {"restart_gap"}
        now, before = self._current_signals(service), self._signals
        if now is None or before is None:
            return {"context_changed"}
        causes = set()
        if now[0] != before[0]:
            causes.add("review_changed")
        if now[1] != before[1]:
            causes.add("protection_changed")
        return causes or {"context_changed"}

    def _active_grants(self, now: int):
        """(grant id, authority, policy) for every grant the ledger holds active right now."""
        found = []
        with self.ledger._transaction() as db:
            for row in db.execute("SELECT grant_id FROM p2a_grants ORDER BY grant_id").fetchall():
                try:
                    authority, policy = self.ledger._authority(db, row["grant_id"], now)
                except PolicyError:
                    continue
                found.append((row["grant_id"], authority, policy))
        return found

    def _dropped_grants(self, now: int, names: set[str]) -> list[str]:
        from .search_index import index_path
        return [grant_id for grant_id, _authority, policy in self._active_grants(now)
                if policy.versions.capability in SEARCH_CAPABILITIES and index_path(self.root, grant_id).name in names]

    def _policy_hash(self, grant_id: str, now: int) -> str | None:
        try:
            with self.ledger._transaction() as db:
                authority, _ = self.ledger._authority(db, grant_id, now)
            return authority.policy_hash
        except PolicyError:
            return None

    def _deferred(self, now: float, first: float) -> bool:
        """Whether a running assessment pass holds the restore back (condition 3)."""
        if self._pass is not None:
            # The node's own pass: a rebuild under it ends `stale` at its next assessment. Its end
            # restores at once; only a pass that has published nothing for max_defer is restored under.
            return now < self._pass["progress_at"] + self.settings.max_defer
        return self._assessing() and now < first + self.settings.max_defer

    def run_pending(self) -> RestoreReceipt | None:
        if not self.settings.restore:
            return None
        now = self.clock()
        with self._lock:
            if not self._pending:
                return None
            first = min(entry["first_drop_at"] for entry in self._pending.values())
            if now < first + self.settings.debounce and not self._pass_ended:
                return None
            if self._last_restore_at is not None and now < self._last_restore_at + self.settings.min_interval:
                return None
            if self._deferred(now, first):
                return None
            due = {grant_id: entry for grant_id, entry in self._pending.items() if entry["not_before"] <= now}
            if not due:
                return None
            self._last_restore_at = now
            self._pass_ended = False
        service = self._index()
        synced = False
        if self._sync_protection is not None:
            try:
                synced = bool(self._sync_protection())
            except Exception as exc:  # noqa: BLE001 -- the rebuild then reports `stale`; class name only
                _log.warning("protection sync before restore failed (%s)", type(exc).__name__)
        grants, causes = [], set()
        for grant_id, entry in sorted(due.items()):
            with self._lock:
                causes |= entry["causes"]
                entry["running"] = True
            policy_hash = self._policy_hash(grant_id, int(self.clock()))
            try:
                with node_principal(self.owner_id):
                    result = service.rebuild(grant_id, now=int(self.clock()))
                state, count = result["state"], result["member_count"]
            except Exception as exc:  # noqa: BLE001 -- the rebuild already purged; never log content
                _log.warning("search index restore failed (%s)", type(exc).__name__)
                state, count = "failed", 0
            with self._lock:
                entry["running"] = False
                again = entry.pop("again", False)
                if again and state in ("ready", "over_cap"):
                    # IF-6: facts moved while this build ran; its snapshot may predate them. One more, as new.
                    entry.update(causes={"facts_changed"}, attempts=0, not_before=0.0, first_drop_at=self.clock())
                elif state in ("ready", "over_cap", "removed"):
                    self._pending.pop(grant_id, None)
                else:
                    entry["attempts"] += 1
                    if entry["attempts"] >= self.settings.max_attempts:
                        self._pending.pop(grant_id, None)
                    else:
                        entry["not_before"] = self.clock() + min(
                            self.settings.backoff * 2 ** (entry["attempts"] - 1), self.settings.max_backoff)
            grants.append(RestoredGrant(grant_id=grant_id, policy_hash=policy_hash, state=state, member_count=count))
        with self._lock:
            self._signals = self._current_signals(service)
            self._persist_names({path.name for path in self.root.glob("grant-*.db")})
        receipt = RestoreReceipt(version=RECEIPT_VERSION, action="search_index_restore", actor="node_system",
                                 cause_classes=sorted(causes), first_drop_at=int(first), started_at=int(now),
                                 finished_at=int(self.clock()), protection_synced=synced, grants=grants)
        self._record(receipt)
        return receipt

    # -- RD2: keep the window assessed -------------------------------------

    def _full_pass_due(self, now: int, last_full) -> bool:
        if last_full is None:
            return True  # the backlog after install or a lost state file: run it now
        if now - last_full < self.settings.full_interval:
            return False
        if self.settings.full_hours is None:
            return True
        start, end = self.settings.full_hours
        return start <= time.localtime(now).tm_hour < end

    def _worker_object(self):
        # The runtime's worker is a singleton; resolving it takes the write gate, so do it once.
        if self._worker_cache is None:
            self._worker_cache = self._worker()
        return self._worker_cache

    def _assessing(self) -> bool:
        if self._worker is None:
            return False
        try:
            return self._worker_object().running()
        except PolicyError:
            return False

    def _window_seconds(self, now: int) -> int | None:
        """The widest window of the active knowledge grants, whole: a pass walks it in slices (window_slices)."""
        windows = [policy.search.window.max_age_seconds for _grant, _authority, policy in self._active_grants(now)
                   if policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH]
        return max(windows) if windows else None

    @staticmethod
    def _new_ingest(path, high_water: int) -> bool:
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        try:
            from .evidence_families import enabled_tables
            for table in enabled_tables():
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "ingested_at" not in columns:
                    continue
                if conn.execute(f"SELECT 1 FROM {table} WHERE julianday(ingested_at)>julianday(?,'unixepoch') LIMIT 1",
                                (high_water,)).fetchone():
                    return True
            return False
        finally:
            conn.close()

    def _assessment_revisions(self) -> dict | None:
        """None when the rules cannot be read (the pinned rubric is missing or not the reviewed bytes):
        no change can be told then, and the pass it would start could not assess anyway."""
        try:
            return assessment_revisions(interests=self.settings.interests)
        except Exception as exc:  # noqa: BLE001 -- class name only
            _log.warning("assessment revisions unreadable (%s)", type(exc).__name__)
            return None

    def _proof_digest(self, path) -> str | None:
        """One read-only snapshot of the canonical database, at most once per check; None when unreadable."""
        try:
            conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("BEGIN")
                return proof_digest(conn, owner_id=self.owner_id)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 -- no digest, no trigger
            _log.warning("proof state unreadable (%s)", type(exc).__name__)
            return None

    def _plan(self, now: int, state: dict, canonical) -> dict | None:
        """The pass this check starts, or None: the first cause in the module docstring's order that applies.
        Its ``slices`` are None to walk the whole window from the newest slice, or the slices a continuation owes."""
        revisions, proof = self._assessment_revisions(), self._proof_digest(canonical)
        last_full, high_water, owed = state["last_full_pass_at"], state["ingest_high_water"], state.get("continuation")
        # An owed continuation runs under the rules and proof its first pass ran under; a change of either
        # is a new pass of its own cause, compared with those, not with the last completed full pass.
        base = owed if owed is not None else {"revisions": state.get("assessment_revisions"),
                                              "proof": state.get("proof_digest")}
        under = {"revisions": revisions, "proof": proof}

        def full(cause, origin=None, resume=None):
            return {"cause": cause, "origin": origin or cause, "scope": "full_window", "ingested_after": None,
                    "slices": resume.get("slices") if resume else None,
                    "high_water": resume.get("high_water") if resume else None, **under}

        if last_full is None:
            # Nothing to compare with yet: the backlog (or what its budget left of it) establishes the record. What
            # it left resumes at the slice it stopped in only under the rules and proof it ran under; otherwise the
            # whole window again, as before slices.
            same = (owed is not None and owed.get("scope") == "full_window"
                    and owed.get("revisions") == revisions and owed.get("proof") == proof)
            return full("budget_continuation" if owed is not None else "startup_backlog", "startup_backlog",
                        owed if same else None)
        if revisions is not None and revisions != base.get("revisions"):
            return full("revision_change")
        if proof is not None and proof != base.get("proof"):
            return full("proof_change")
        if owed is not None:
            return {"cause": "budget_continuation", "origin": owed["origin"], "scope": owed["scope"],
                    "ingested_after": owed.get("ingested_after"), "slices": owed.get("slices"),
                    "high_water": owed.get("high_water"), **under}
        if high_water is None or self._full_pass_due(now, last_full):
            return full("daily_reconciliation")
        if not self._new_ingest(canonical, high_water):
            return None
        return {"cause": "new_ingest", "origin": "new_ingest", "scope": "changed_conversations",
                "ingested_after": high_water, "slices": None, "high_water": None, **under}

    def run_catchup(self) -> CatchUpReceipt | None:
        """Every tick. Cheap unless a pass is running or finishing, or `catchup_interval` has passed: the
        ledger, the index service, the rules and the canonical database are read at most once per interval."""
        if not self.settings.catchup or self._worker is None:
            return None
        now = int(self.clock())
        with self._lock:
            if self._pass is not None:
                worker = self._worker_object()
                if worker.running():
                    self._note_progress(worker, now)
                    return None
                return self._slice_ended(worker, now)
            if self._last_catchup_check is not None and now < self._last_catchup_check + self.settings.catchup_interval:
                return None
            self._last_catchup_check = now
        worker = self._worker_object()
        if worker.running():
            return None  # the owner's own pass; never interfere with it
        with self._lock:
            state = self._load_state()
            window = self._window_seconds(now)
            if window is None:
                return None
            plan = self._plan(now, state, self._index().resolver.path)
            if plan is None:
                return None
            slices = plan["slices"] or window_slices(max(now - window, 0), now)
            if not slices:
                return None
            run = {**plan, "slices": slices, "slice": 0, "after": slices[-1][0], "before": slices[0][1],
                   "started_at": now, "progress_at": now, "counts": dict.fromkeys(_COUNTS, 0)}
            self._start_slice(worker, run, now)
            self._pass = run
        return None

    def _start_slice(self, worker, run: dict, now: int) -> None:
        """The pass's current slice as one worker run, with what the pass has left of its budget."""
        from .message_review_contract import AutomaticReviewRequest
        after, before = run["slices"][run["slice"]]
        with node_principal(self.owner_id):
            worker.start_node_pass(AutomaticReviewRequest(after=after, before=before), now=now,
                                   ingested_after=run["ingested_after"],
                                   max_assessed=self.settings.max_assessed - run["counts"]["assessed"])
        # A run's status starts at zero. The pass's progress_at stays: starting a slice publishes nothing.
        run["assessed_seen"] = 0

    def _note_progress(self, worker, now: int) -> None:
        """The node pass published another assessment since the last tick: it still holds the restore back."""
        try:
            with node_principal(self.owner_id):
                assessed = worker.status().assessed
        except Exception:  # noqa: BLE001 -- unknown progress keeps the last known time
            return
        if assessed != self._pass["assessed_seen"]:
            self._pass.update(assessed_seen=assessed, progress_at=now)

    def _slice_ended(self, worker, now: int) -> CatchUpReceipt | None:
        """A slice's worker run ended. The pass ends here at its last slice, at its budget, or at a slice that did
        not complete; otherwise the next slice starts in this same tick, so the restore and the interest refresh
        never see the pass end between its slices and no rebuild runs between them."""
        run, ended = self._pass, self._pass_ended
        self._pass, self._pass_ended = None, True   # over, unless the next slice starts below
        with node_principal(self.owner_id):
            status = worker.status()
        for key in _COUNTS:
            run["counts"][key] += getattr(status, key)
        exhausted = run["counts"]["assessed"] >= self.settings.max_assessed
        state = status.state if status.state in ("complete", "cancelled", "failed") else "failed"
        if state == "complete" and not exhausted and run["slice"] + 1 < len(run["slices"]):
            run["slice"] += 1
            try:
                self._start_slice(worker, run, now)
            except Exception as exc:  # noqa: BLE001 -- class name only; the pass ends failed and marks nothing done
                _log.warning("catch-up slice not started (%s)", type(exc).__name__)
                state = "failed"
            else:
                self._pass, self._pass_ended = run, ended
                return None
        return self._finish_pass(run, state, exhausted, now)

    @staticmethod
    def _resume(run: dict) -> dict:
        """Where the continuation of a pass that stopped at its budget starts. In the window's newest slice: the
        whole window again, planned at its own time, as before slices. In an older one: that slice and every older
        one as planned, and the time the newest was walked, which becomes the ingest high-water mark when the last
        ends within budget (new_ingest then re-checks what arrived since)."""
        if run["slice"] == 0 and run["high_water"] is None:
            return {"slices": None, "high_water": None}
        return {"slices": [list(pair) for pair in run["slices"][run["slice"]:]],
                "high_water": run["started_at"] if run["high_water"] is None else run["high_water"]}

    def _finish_pass(self, run: dict, state: str, exhausted: bool, now: int) -> CatchUpReceipt:
        counts = run["counts"]
        if state == "complete":
            stored = self._load_state()
            if exhausted:
                # Owed: the same scope again from the slice this pass stopped in, under the same rules and proof,
                # one interval from now so the restore that follows this pass serves in between.
                stored["continuation"] = {key: run[key] for key in
                                          ("scope", "ingested_after", "origin", "revisions", "proof")}
                stored["continuation"].update(self._resume(run))
                self._last_catchup_check = now
            else:
                # Rows ingested after the window's newest slice was walked are picked up by the next pass.
                stored["ingest_high_water"] = run["started_at"] if run["high_water"] is None else run["high_water"]
                stored["continuation"] = None
                if run["scope"] == "full_window":
                    if run["origin"] in RECONCILING:
                        stored["last_full_pass_at"] = run["started_at"]
                    if run["revisions"] is not None:
                        stored["assessment_revisions"] = run["revisions"]
                    if run["proof"] is not None:
                        stored["proof_digest"] = run["proof"]
            self._save_state()
            if self.settings.interests:
                self._interest_after_pass = (max(self.settings.max_assessed - counts["assessed"], 0),
                                             run["scope"] == "full_window" and not exhausted)
        receipt = CatchUpReceipt(version=RECEIPT_VERSION, action="message_assessment_catchup", actor="node_system",
                                 cause_class=run["cause"], scope=run["scope"], window_after=run["after"],
                                 window_before=run["before"], started_at=run["started_at"], finished_at=now,
                                 state=state, budget_exhausted=exhausted, **counts)
        self._record(receipt)
        return receipt

    # -- IF-5 Q&A I8: browsing interests stored and their labels assessed ------

    def run_interests(self) -> InterestRefreshReceipt | None:
        """After a pass, within what it left of its budget, or once per `interest_interval` when nothing else
        ran. Never with the interest flag off, never beside a pass, never while the owner's pass runs."""
        if not (self.settings.catchup and self.settings.interests) or self._worker is None:
            return None
        now = int(self.clock())
        with self._lock:
            if self._pass is not None:
                return None
            after, self._interest_after_pass = self._interest_after_pass, None
            interval_due = (self._last_interest_at is None
                            or now >= self._last_interest_at + self.settings.interest_interval)
            if after is not None and (after[1] or interval_due):
                cause, budget = "after_pass", after[0]
            elif after is None and interval_due and self._load_state().get("continuation") is None:
                cause, budget = "interval", self.settings.max_assessed
            else:
                return None
        if self._worker_object().running():
            return None  # the owner's own pass; the interval brings this back once it is done
        self._last_interest_at = now
        counts = dict(inserted=0, closed=0, unchanged=0, pending=0, assessed=0, unresolved=0, short=False)
        if self.settings.relabels:
            counts.update(relabel_pending=0, relabel_calls=0, relabelled=0)
        try:
            state = self._refresh_interests(budget, counts)
        except Exception as exc:  # noqa: BLE001 -- class name only; never a label
            _log.warning("interest refresh failed (%s)", type(exc).__name__)
            state = "failed"
        short = counts.pop("short")   # a label or a second try the budget did not reach
        requested = 0
        if counts["assessed"] or counts["inserted"] or counts["closed"]:
            try:
                requested = self._request_interest_rebuilds(self.clock())
            except Exception as exc:  # noqa: BLE001 -- class name only
                _log.warning("interest rebuild request failed (%s)", type(exc).__name__)
        receipt = InterestRefreshReceipt(version=RECEIPT_VERSION, action="interest_refresh", actor="node_system",
                                         cause_class=cause, started_at=now, finished_at=int(self.clock()), state=state,
                                         budget=budget, budget_exhausted=short or counts["pending"] > budget,
                                         rebuild_requested=requested, **counts)
        self._record(receipt)
        return receipt

    def _interest_grants(self, now: int) -> list[str]:
        """Active grants that sign interests (``interest_index.admits``) and have an index here, published or
        owed. A grant that never had an index is still never built here: the owner hooks build it."""
        from .interest_index import admits
        from .search_index import index_path
        names = {path.name for path in self.root.glob("grant-*.db")}
        with self._lock:
            owed = set(self._pending)
        return [grant_id for grant_id, _authority, policy in self._active_grants(now)
                if admits(policy) and (index_path(self.root, grant_id).name in names or grant_id in owed)]

    def _request_interest_rebuilds(self, now: float) -> int:
        """Queue the grants that sign interests on the restore's own queue, cause `interest_changed`."""
        grants = self._interest_grants(int(now))
        with self._lock:
            for grant_id in grants:
                entry = self._pending.setdefault(grant_id, {"causes": set(), "attempts": 0, "not_before": 0.0,
                                                            "first_drop_at": now})
                entry["causes"].add("interest_changed")
        if grants:
            self._wake.set()
        return len(grants)

    def _refresh_interests(self, budget: int, counts: dict) -> str:
        from . import interest_family as fam
        from . import interest_relabel as relabel
        service = self._index()
        waited, self._relabels_waited = self._relabels_waited, False
        with node_principal(self.owner_id):
            with service.reviews._db() as db:
                opt_outs = service.reviews._opt_outs_in(db)
            built, pending, owed = self._interest_snapshot(service, opt_outs)

            def store(conn):
                if self.settings.relabels:
                    relabel.prune(conn, owner_id=self.owner_id, built=built)
                return fam.persist(conn, built)

            counts.update(self._canonical_write(service, store))
            counts["pending"] = len(pending)
            spent = {"calls": 0, "failures": 0}
            state = self._assess_interest_labels(service, pending, budget, counts, spent)
            if not self.settings.relabels:
                return state            # the refresh as it was before interest_relabel: nothing more is read or asked
            counts["relabel_pending"] = len(owed)
            if state != "complete" or not owed:
                return state
            with self._lock:
                restore_owed = bool(self._pending)
            if restore_owed and not waited:
                # A restore is owed, and it runs right after this refresh. A grant whose index was dropped is dark
                # until it does, and the second tries are model calls it would wait for: they stand back for the
                # next round, which is due at once. Once only: the round after a wait makes them whatever is owed.
                self._relabels_waited, self._last_interest_at = True, None
                return state
            state = self._second_labels(service, owed, opt_outs, budget, counts, spent)
            if state != "complete" or not counts["relabelled"]:
                return state
            # A cluster with a second label is an interest object now: store it and assess its label, as above.
            seen = {prepared["label_revision"] for prepared in pending}
            built, pending, _owed = self._interest_snapshot(service, opt_outs)
            fresh = [prepared for prepared in pending if prepared["label_revision"] not in seen]
            stored = self._canonical_write(service, lambda conn: fam.persist(conn, built))
            # Both stores add up; `unchanged` stays the first store's (what this refresh found and left as it was).
            counts.update(inserted=counts["inserted"] + stored["inserted"], closed=counts["closed"] + stored["closed"],
                          pending=counts["pending"] + len(fresh))
            return self._assess_interest_labels(service, fresh, budget, counts, spent)

    def _interest_snapshot(self, service, opt_outs) -> tuple:
        """One read snapshot outside the gate: the objects, the labels owed an assessment (interest_review.pending)
        and, unless interest_relabel is switched off, the clusters owed a second label (interest_relabel.pending)."""
        from . import interest_family as fam
        from . import interest_relabel as relabel
        from . import interest_review as ir
        from .entity_boundary import EntityBoundary
        with service.resolver._read(gated=False) as (conn, _floor):
            boundary = EntityBoundary(conn)
            built = fam.build(conn, owner_id=self.owner_id, now_us=int(self.clock()) * 1_000_000,
                              boundary=boundary, opt_outs=opt_outs)
            return (built, ir.pending(conn, owner_id=self.owner_id, objects=built.objects, boundary=boundary),
                    relabel.pending(conn, owner_id=self.owner_id, built=built) if self.settings.relabels else [])

    def _assess_interest_labels(self, service, pending: list, budget: int, counts: dict, spent: dict) -> str:
        """One model call per label, with no database or gate held; each published under the gate. `spent` counts
        the refresh's model calls against its budget, and its model failures in a row, across both kinds of call."""
        from . import interest_review as ir
        from .entity_boundary import EntityBoundary
        for prepared in pending:
            if spent["calls"] >= budget:
                counts["short"] = True
                break
            if self._stop.is_set() or self._worker_object().running():
                return "cancelled"
            spent["calls"] += 1
            try:
                labels = asyncio.run(ir.assess(prepared))  # one local call; no database or gate is held
            except Exception:  # noqa: BLE001 -- the model's answer may hold text; count it only
                counts["unresolved"] += 1
                spent["failures"] += 1
                if spent["failures"] >= MAX_CONSECUTIVE_FAILURES:
                    return "failed"
                continue
            spent["failures"] = 0
            try:
                # Against the vocabulary current under the gate: one that moved while the model ran refuses.
                self._canonical_write(service, lambda conn: ir.publish(
                    conn, owner_id=self.owner_id, prepared=prepared, classification=labels,
                    boundary=EntityBoundary(conn)))
            except PolicyError:
                counts["unresolved"] += 1
                continue
            counts["assessed"] += 1
        return "complete"

    def _second_labels(self, service, owed: list, opt_outs, budget: int, counts: dict, spent: dict) -> str:
        """interest_relabel's tries for the clusters owed one: each model call with no database or gate held, each
        answer judged and stored in one write under the gate, against the rows and the exclusions current there.
        A call the model did not complete spends no try; an answer that could not be stored (the cluster's label
        or its exclusions moved meanwhile) is dropped."""
        from . import interest_relabel as relabel
        from .entity_boundary import EntityBoundary
        for prepared in owed:
            while prepared is not None:
                if spent["calls"] >= budget:
                    counts["short"] = True
                    return "complete"
                if self._stop.is_set() or self._worker_object().running():
                    return "cancelled"
                spent["calls"] += 1
                counts["relabel_calls"] += 1
                try:
                    answer = asyncio.run(relabel.ask(prepared))  # one local call; no database or gate is held
                except Exception:  # noqa: BLE001 -- the model's answer may hold text; count it only
                    counts["unresolved"] += 1
                    spent["failures"] += 1
                    if spent["failures"] >= MAX_CONSECUTIVE_FAILURES:
                        return "failed"
                    break
                spent["failures"] = 0
                try:
                    result, broken = self._canonical_write(service, lambda conn: relabel.publish(
                        conn, owner_id=self.owner_id, prepared=prepared, answer=answer,
                        now_us=int(self.clock()) * 1_000_000, boundary=EntityBoundary(conn), opt_outs=opt_outs))
                except PolicyError:
                    counts["unresolved"] += 1
                    break
                counts["relabelled"] += result.label is not None
                prepared = relabel.next_try(prepared, result, answer, broken)
        return "complete"

    @staticmethod
    def _canonical_write(service, operation):
        """One short write transaction on the canonical database, under the node write gate."""
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            conn = sqlite3.connect(Path(service.resolver.path).as_uri() + "?mode=rw", uri=True, timeout=5)
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = operation(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    # -- receipts and the thread -------------------------------------------

    def _record(self, receipt) -> None:
        try:
            with node_principal(self.owner_id):
                self.ledger.record_system_action(receipt.model_dump(), now=int(self.clock()))
        except Exception as exc:  # noqa: BLE001
            _log.warning("node system action receipt not recorded (%s)", type(exc).__name__)

    def step(self) -> None:
        """One scheduling round. Never raises: this runs on a daemon thread. The interests run before the
        restore, so one restore follows a pass and the labels assessed after it."""
        for part in (self.run_catchup, self.run_interests, self.run_pending):
            try:
                part()
            except Exception as exc:  # noqa: BLE001 -- class name only; never content
                _log.warning("search refresh step failed (%s)", type(exc).__name__)

    def start(self, service=None) -> None:
        """`service`: observe the published set now, before the sweeper's first sweep can drop an
        index this loop has never seen (a node without a state file has no other record of it)."""
        if not self.settings.enabled or self._thread is not None:
            return
        if service is not None:
            self.after_sweep(service)

        def loop():
            while not self._stop.is_set():
                self.step()
                self._wake.wait(self.settings.tick)
                self._wake.clear()

        self._thread = threading.Thread(target=loop, name="p2c-search-refresh", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()


def start_at_startup(*, delay: float = 60.0) -> bool:
    """App startup: bring the loop up with the node when its flags are on. A no-op otherwise.

    The permissions runtime is otherwise created by the first request, so after a restart
    nothing would restore or assess until someone asked. The delay keeps it out of startup.
    """
    if not RefreshSettings.from_env().enabled:
        return False

    def run():
        if delay:
            time.sleep(delay)
        try:
            from .runtime import get_runtime
            runtime = get_runtime()
            with node_principal(runtime.protocol.ledger.identity.owner_id):
                runtime.refresh_loop()
        except PolicyError as exc:
            _log.warning("search refresh not started: %s", exc.code)
        except Exception as exc:  # noqa: BLE001
            _log.warning("search refresh not started (%s)", type(exc).__name__)

    threading.Thread(target=run, name="p2c-search-refresh-start", daemon=True).start()
    return True
