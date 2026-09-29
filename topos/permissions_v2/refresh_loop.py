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
3. It is coalesced and rate-limited. Drops within ``debounce`` share one pass, passes start at
   least ``min_interval`` apart, a running assessment pass defers them (up to ``max_defer``),
   and a failed restore backs off exponentially for at most ``max_attempts`` tries. After that
   the grant stays dark until the owner acts, which is the behaviour without this module.
4. It builds on a read snapshot outside the write gate (the rebuild's merge-gate path) and
   writes a ``topos-node-system-action/v1`` receipt to the ledger with its cause classes.
   Receipts hold counts, grant ids and policy hashes, never a record, name or reason text.

``TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED`` (RD2). Keeps the window of every active
p2c-v3 grant assessed as it rolls. An incremental pass re-checks the conversations that
received rows since the last pass (a new row changes its neighbours' context revision). A
full pass reconciles the whole window once a day and after a lost state file. Passes never
rebuild an index themselves: each new assessment moves the review digest, the sweep drops the
index as drift, and the restore above rebuilds it once the pass is idle. Model calls per pass
are bounded (owner decision OD-12 sets the budget).

Both act as the node's own process for its owner, the precedent of
``Runtime.ensure_evidence_reviews``. Nothing here changes what a recipient can receive: a
record still leaves the node only if the per-candidate re-decision permits it at read time.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

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
STATE_VERSION = "topos-search-refresh-state/v1"
RECEIPT_VERSION = "topos-node-system-action/v1"
# The assessment worker's own bound on one pass. A longer grant window is kept assessed for
# its newest 31 days, the same limit an owner-started pass has.
MAX_WINDOW_SECONDS = 31 * 86400

CauseClass = Literal["review_changed", "protection_changed", "context_changed", "restart_gap"]
RestoreState = Literal["ready", "over_cap", "removed", "stale", "failed"]


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
    grants: list[RestoredGrant]


class CatchUpReceipt(StrictModel):
    version: Literal["topos-node-system-action/v1"]
    action: Literal["message_assessment_catchup"]
    actor: Literal["node_system"]
    cause_class: Literal["startup_backlog", "daily_reconciliation", "new_ingest"]
    scope: Literal["full_window", "changed_conversations"]
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

    @classmethod
    def from_env(cls, env=None) -> "RefreshSettings":
        env = os.environ if env is None else env
        restore = _flag(RESTORE_FLAG, env)
        # Catch-up without restore would drop every grant index on its first new assessment and
        # leave it dark, so it only runs with restore on.
        return cls(restore=restore, catchup=restore and _flag(CATCHUP_FLAG, env),
                   min_interval=float(_seconds(MIN_INTERVAL_ENV, env, 300, floor=60)),
                   max_assessed=_seconds(BUDGET_ENV, env, 500, floor=1))

    @property
    def enabled(self) -> bool:
        return self.restore or self.catchup


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
                 settings: RefreshSettings, clock: Callable[[], float] = time.time):
        self.ledger = ledger
        self.root = Path(root)
        self._index, self._worker = index, worker
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

    # -- persisted state -----------------------------------------------------

    def _state_path(self) -> Path:
        return self.root / STATE_FILE

    def _load_state(self) -> dict:
        if self._state is None:
            state = {"version": STATE_VERSION, "names": [], "ingest_high_water": None, "last_full_pass_at": None}
            try:
                loaded = json.loads(self._state_path().read_text("utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == STATE_VERSION:
                    state.update({key: loaded.get(key) for key in ("names", "ingest_high_water", "last_full_pass_at")})
                    if not isinstance(state["names"], list):
                        state["names"] = []
            except (OSError, ValueError):
                pass
            self._state = state
        return self._state

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
            if self._assessing() and now < first + self.settings.max_defer:
                return None
            due = {grant_id: entry for grant_id, entry in self._pending.items() if entry["not_before"] <= now}
            if not due:
                return None
            self._last_restore_at = now
            self._pass_ended = False
        service = self._index()
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
                                 finished_at=int(self.clock()), grants=grants)
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
            for table in ("conversation_messages", "ai_chat_messages"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "ingested_at" not in columns:
                    continue
                if conn.execute(f"SELECT 1 FROM {table} WHERE julianday(ingested_at)>julianday(?,'unixepoch') LIMIT 1",
                                (high_water,)).fetchone():
                    return True
            return False
        finally:
            conn.close()

    def run_catchup(self) -> CatchUpReceipt | None:
        """Every tick. Cheap unless a pass is finishing or `catchup_interval` has passed: the ledger,
        the index service and the canonical database are read at most once per interval."""
        if not self.settings.catchup or self._worker is None:
            return None
        from .message_review_contract import AutomaticReviewRequest
        now = int(self.clock())
        with self._lock:
            if self._pass is not None:
                worker = self._worker_object()
                return None if worker.running() else self._finish_pass(worker, now)
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
            last_full, high_water = state["last_full_pass_at"], state["ingest_high_water"]
            if high_water is None or self._full_pass_due(now, last_full):
                cause = "startup_backlog" if last_full is None else "daily_reconciliation"
                scope, ingested_after = "full_window", None
            else:
                if not self._new_ingest(self._index().resolver.path, high_water):
                    return None
                cause, scope, ingested_after = "new_ingest", "changed_conversations", high_water
            request = AutomaticReviewRequest(after=max(now - window, 0), before=now)
            with node_principal(self.owner_id):
                worker.start_node_pass(request, now=now, ingested_after=ingested_after,
                                       max_assessed=self.settings.max_assessed)
            self._pass = {"cause": cause, "scope": scope, "after": request.after, "before": now, "started_at": now}
        return None

    def _finish_pass(self, worker, now: int) -> CatchUpReceipt:
        run = self._pass
        self._pass = None
        self._pass_ended = True
        with node_principal(self.owner_id):
            status = worker.status()
        exhausted = status.assessed >= self.settings.max_assessed
        state = status.state if status.state in ("complete", "cancelled", "failed") else "failed"
        if state == "complete" and not exhausted:
            # Rows ingested after the pass started are picked up by the next one.
            self._state["ingest_high_water"] = run["started_at"]
            if run["scope"] == "full_window":
                self._state["last_full_pass_at"] = run["started_at"]
            self._save_state()
        receipt = CatchUpReceipt(version=RECEIPT_VERSION, action="message_assessment_catchup", actor="node_system",
                                 cause_class=run["cause"], scope=run["scope"], window_after=run["after"],
                                 window_before=run["before"], started_at=run["started_at"], finished_at=now,
                                 state=state, scanned=status.scanned, assessed=status.assessed, current=status.current,
                                 withheld=status.withheld, unresolved=status.unresolved, budget_exhausted=exhausted)
        self._record(receipt)
        return receipt

    # -- receipts and the thread -------------------------------------------

    def _record(self, receipt) -> None:
        try:
            with node_principal(self.owner_id):
                self.ledger.record_system_action(receipt.model_dump(), now=int(self.clock()))
        except Exception as exc:  # noqa: BLE001
            _log.warning("node system action receipt not recorded (%s)", type(exc).__name__)

    def step(self) -> None:
        """One scheduling round. Never raises: this runs on a daemon thread."""
        for part in (self.run_catchup, self.run_pending):
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
