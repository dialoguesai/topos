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
   never built here; the owner hooks build it.
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
   floors and, with the interest flag, the interest label rubric) differ from the ones the last
   completed full pass ran under. An install that changes a rule stales every assessment it
   touches (OD-54 staled every AI-chat assessment, and the grant's AI-chat rows went from 31
   releasable to 0 until a manual pass), so this full pass runs at once, not at night.
3. ``proof_change``: what makes a row provable or eligible moved with no new ingest
   (:func:`proof_digest`: capture receipts, identity attestations, source installs and posture
   overrides, native enrollments, the protection clock). A capture receipt proves rows ingested
   long ago, and an Off-limits edit changes every assessment's context; the ingest high-water
   mark sees neither. A full pass at once.
4. ``budget_continuation``: the last pass stopped at its model budget. The same scope again,
   one ``catchup_interval`` after it ended so that the restored index serves in between, until
   a pass ends within budget. It runs under the rules and proof its first pass ran under; a
   change of either starts a new pass of cause 2 or 3 instead.
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

With ``TOPOS_PERMISSIONS_V2_INTEREST_SOURCES`` also on (IF-5 Q&A I8), the browsing-interest
objects are kept stored and their labels assessed: after a pass, inside what that pass left of
its budget, or once per ``interest_interval`` when nothing else ran. The objects are built on a
read snapshot and stored by ``interest_family.persist`` in one short write under the gate. The
labels with no current assessment (``interest_review.pending``) are assessed one model call at a
time with no lock held, each published by ``interest_review.publish`` under the gate against the
protected vocabulary current at that moment. That is ``interest_review.assess_pending``'s own
sequence, run by its parts because it holds one connection across its model calls, which from a
background thread means taking SQLite's write lock outside the gate. With the flag off nothing
here touches the interest family.

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
# Unchanged by the keys added after v1 (revisions, proof, continuation): each is optional, a file
# without one loads with it empty, and an older loop reading a newer file ignores them.
STATE_VERSION = "topos-search-refresh-state/v1"
RECEIPT_VERSION = "topos-node-system-action/v1"
PROOF_VERSION = "topos-refresh-proof/v1"
# The assessment worker's own bound on one pass. A longer grant window is kept assessed for
# its newest 31 days, the same limit an owner-started pass has.
MAX_WINDOW_SECONDS = 31 * 86400
# The worker gives up a pass after this many model failures in a row; the interest labels do too.
MAX_CONSECUTIVE_FAILURES = 3

CauseClass = Literal["review_changed", "protection_changed", "context_changed", "restart_gap"]
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
    interest_interval: float = 3600.0

    @classmethod
    def from_env(cls, env=None) -> "RefreshSettings":
        env = os.environ if env is None else env
        restore = _flag(RESTORE_FLAG, env)
        # Catch-up without restore would drop every grant index on its first new assessment and
        # leave it dark, so it only runs with restore on.
        catchup = restore and _flag(CATCHUP_FLAG, env)
        interests = False
        if catchup:
            # The interest index's own reading of its flag, so the two never disagree about it.
            from .interest_index import enabled as interest_sources_enabled
            interests = interest_sources_enabled(env)
        return cls(restore=restore, catchup=catchup,
                   min_interval=float(_seconds(MIN_INTERVAL_ENV, env, 300, floor=60)),
                   max_assessed=_seconds(BUDGET_ENV, env, 500, floor=1), interests=interests)

    @property
    def enabled(self) -> bool:
        return self.restore or self.catchup


def assessment_revisions(*, interests: bool = False, env=None) -> dict:
    """The rules a machine assessment is current under (``automatic_message_review.is_current``), for the
    families enabled now. Version strings and hashes only. Raises what the pinned rubric's read raises."""
    from .automatic_message_review import (CONTEXT_VERSIONS, JOURNAL_CONTEXT, JOURNAL_FLOORS_VERSION, MODEL_REVISION,
                                           rubric_revision_for)
    from .evidence_families import enabled_tables
    tables = enabled_tables(env)
    revisions = {"model": MODEL_REVISION, "rubric": {table: rubric_revision_for(table) for table in tables},
                 "context": {table: CONTEXT_VERSIONS[table] for table in tables if table in CONTEXT_VERSIONS}}
    if "journal_entries" in tables:
        revisions["context"]["journal_entries"] = dict(JOURNAL_CONTEXT)
        revisions["journal_floors"] = JOURNAL_FLOORS_VERSION
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

    # -- persisted state -----------------------------------------------------

    def _state_path(self) -> Path:
        return self.root / STATE_FILE

    def _load_state(self) -> dict:
        if self._state is None:
            state = {"version": STATE_VERSION, "names": [], "ingest_high_water": None, "last_full_pass_at": None,
                     "assessment_revisions": None, "proof_digest": None, "continuation": None}
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
            causes |= entry["causes"]
            policy_hash = self._policy_hash(grant_id, int(self.clock()))
            try:
                with node_principal(self.owner_id):
                    result = service.rebuild(grant_id, now=int(self.clock()))
                state, count = result["state"], result["member_count"]
            except Exception as exc:  # noqa: BLE001 -- the rebuild already purged; never log content
                _log.warning("search index restore failed (%s)", type(exc).__name__)
                state, count = "failed", 0
            with self._lock:
                if state in ("ready", "over_cap", "removed"):
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
        windows = [policy.search.window.max_age_seconds for _grant, _authority, policy in self._active_grants(now)
                   if policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH]
        return min(max(windows), MAX_WINDOW_SECONDS) if windows else None

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
        """The pass this check starts, or None: the first cause in the module docstring's order that applies."""
        revisions, proof = self._assessment_revisions(), self._proof_digest(canonical)
        last_full, high_water, owed = state["last_full_pass_at"], state["ingest_high_water"], state.get("continuation")
        # An owed continuation runs under the rules and proof its first pass ran under; a change of either
        # is a new pass of its own cause, compared with those, not with the last completed full pass.
        base = owed if owed is not None else {"revisions": state.get("assessment_revisions"),
                                              "proof": state.get("proof_digest")}
        under = {"revisions": revisions, "proof": proof}

        def full(cause, origin=None):
            return {"cause": cause, "origin": origin or cause, "scope": "full_window", "ingested_after": None,
                    **under}

        if last_full is None:
            # Nothing to compare with yet: the backlog (or what its budget left of it) establishes the record.
            return full("budget_continuation" if owed is not None else "startup_backlog", "startup_backlog")
        if revisions is not None and revisions != base.get("revisions"):
            return full("revision_change")
        if proof is not None and proof != base.get("proof"):
            return full("proof_change")
        if owed is not None:
            return {"cause": "budget_continuation", "origin": owed["origin"], "scope": owed["scope"],
                    "ingested_after": owed.get("ingested_after"), **under}
        if high_water is None or self._full_pass_due(now, last_full):
            return full("daily_reconciliation")
        if not self._new_ingest(canonical, high_water):
            return None
        return {"cause": "new_ingest", "origin": "new_ingest", "scope": "changed_conversations",
                "ingested_after": high_water, **under}

    def run_catchup(self) -> CatchUpReceipt | None:
        """Every tick. Cheap unless a pass is running or finishing, or `catchup_interval` has passed: the
        ledger, the index service, the rules and the canonical database are read at most once per interval."""
        if not self.settings.catchup or self._worker is None:
            return None
        from .message_review_contract import AutomaticReviewRequest
        now = int(self.clock())
        with self._lock:
            if self._pass is not None:
                worker = self._worker_object()
                if worker.running():
                    self._note_progress(worker, now)
                    return None
                return self._finish_pass(worker, now)
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
            request = AutomaticReviewRequest(after=max(now - window, 0), before=now)
            with node_principal(self.owner_id):
                worker.start_node_pass(request, now=now, ingested_after=plan["ingested_after"],
                                       max_assessed=self.settings.max_assessed)
            self._pass = {**plan, "after": request.after, "before": now, "started_at": now,
                          "assessed_seen": 0, "progress_at": now}
        return None

    def _note_progress(self, worker, now: int) -> None:
        """The node pass published another assessment since the last tick: it still holds the restore back."""
        try:
            with node_principal(self.owner_id):
                assessed = worker.status().assessed
        except Exception:  # noqa: BLE001 -- unknown progress keeps the last known time
            return
        if assessed != self._pass["assessed_seen"]:
            self._pass.update(assessed_seen=assessed, progress_at=now)

    def _finish_pass(self, worker, now: int) -> CatchUpReceipt:
        run = self._pass
        self._pass = None
        self._pass_ended = True
        with node_principal(self.owner_id):
            status = worker.status()
        exhausted = status.assessed >= self.settings.max_assessed
        state = status.state if status.state in ("complete", "cancelled", "failed") else "failed"
        if state == "complete":
            stored = self._load_state()
            if exhausted:
                # Owed: the same scope again, under the same rules and proof, one interval from now so
                # the restore that follows this pass serves in between.
                stored["continuation"] = {key: run[key] for key in
                                          ("scope", "ingested_after", "origin", "revisions", "proof")}
                self._last_catchup_check = now
            else:
                # Rows ingested after the pass started are picked up by the next one.
                stored["ingest_high_water"] = run["started_at"]
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
                self._interest_after_pass = (max(self.settings.max_assessed - status.assessed, 0),
                                             run["scope"] == "full_window" and not exhausted)
        receipt = CatchUpReceipt(version=RECEIPT_VERSION, action="message_assessment_catchup", actor="node_system",
                                 cause_class=run["cause"], scope=run["scope"], window_after=run["after"],
                                 window_before=run["before"], started_at=run["started_at"], finished_at=now,
                                 state=state, scanned=status.scanned, assessed=status.assessed, current=status.current,
                                 withheld=status.withheld, unresolved=status.unresolved, budget_exhausted=exhausted)
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
        counts = dict(inserted=0, closed=0, unchanged=0, pending=0, assessed=0, unresolved=0)
        try:
            state = self._refresh_interests(budget, counts)
        except Exception as exc:  # noqa: BLE001 -- class name only; never a label
            _log.warning("interest refresh failed (%s)", type(exc).__name__)
            state = "failed"
        if counts["assessed"] or counts["inserted"] or counts["closed"]:
            with self._lock:
                self._pass_ended = True  # its drops (once interests are index members) are all in
        receipt = InterestRefreshReceipt(version=RECEIPT_VERSION, action="interest_refresh", actor="node_system",
                                         cause_class=cause, started_at=now, finished_at=int(self.clock()), state=state,
                                         budget=budget, budget_exhausted=counts["pending"] > budget, **counts)
        self._record(receipt)
        return receipt

    def _refresh_interests(self, budget: int, counts: dict) -> str:
        from . import interest_family as fam
        from . import interest_review as ir
        from .entity_boundary import EntityBoundary
        service = self._index()
        with node_principal(self.owner_id):
            with service.reviews._db() as db:
                opt_outs = service.reviews._opt_outs_in(db)
            with service.resolver._read(gated=False) as (conn, _floor):
                boundary = EntityBoundary(conn)
                built = fam.build(conn, owner_id=self.owner_id, now_us=int(self.clock()) * 1_000_000,
                                  boundary=boundary, opt_outs=opt_outs)
                pending = ir.pending(conn, owner_id=self.owner_id, objects=built.objects, boundary=boundary)
            counts.update(self._canonical_write(service, lambda conn: fam.persist(conn, built)))
            counts["pending"] = len(pending)
            failures = 0
            for prepared in pending[:budget]:
                if self._stop.is_set() or self._worker_object().running():
                    return "cancelled"
                try:
                    labels = asyncio.run(ir.assess(prepared))  # one local call; no database or gate is held
                except Exception:  # noqa: BLE001 -- the model's answer may hold text; count it only
                    counts["unresolved"] += 1
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        return "failed"
                    continue
                failures = 0
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
