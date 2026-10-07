"""Explicit, disabled-by-default, single-process beta protocol configuration."""
from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


from . import switches
from .canonical import PolicyError
from .contract import Hash, Identifier, StrictModel
from .ledger import NodeIdentity, PolicyLedger
from .node_protocol import NodePolicyProtocol
from .protection_clock import current_protection_revision, ensure_protection_clock, repair_identity_coverage


class NodeProtocolConfig(StrictModel):
    version: Literal["topos-policy-node-config/v1"]
    identity: NodeIdentity
    cp_issuer_id: Identifier
    frontend_client_id: Identifier
    trusted_cp_keys: dict[Identifier, Hash]
    node_signing_kid: Identifier
    node_signing_key_path: str
    canonical_database_path: str
    ledger_path: str
    evidence_review_store_path: str | None = None
    projection_review_store_path: str | None = None


EVIDENCE_REVIEWS_FLAG = switches.EVIDENCE_REVIEWS.name
#: Every node config names an environment with this prefix (``load_runtime``; ``switches.is_bound`` reads the same).
BETA_ENVIRONMENT_PREFIX = "permissions-beta-"
# The store's default place: the durable permissions-v2 directory beside the canonical database,
# where the ledger, the signing key and the canonical floor already live. A config may still name
# another file inside that directory.
DEFAULT_EVIDENCE_REVIEW_STORE = "evidence-reviews.db"


class Runtime:
    def __init__(self, protocol: NodePolicyProtocol, lock_file, config_path: Path, *, evidence_review_store_path: Path | None = None,
                 projection_review_store_path: Path | None = None):
        self.protocol = protocol
        self.lock_file = lock_file
        self.config_path = config_path
        self.pid = os.getpid()
        self.evidence_review_store_path = evidence_review_store_path
        self._evidence_review_runtime = None
        self.projection_review_store_path = projection_review_store_path
        self._projection_review_runtime = None
        self._ingestion_service = None
        self._ingestion_snapshot_root = None
        self._identity_service = None
        self._canonical_floor = None
        self._message_search_index = None
        self._sweeper = None
        self._sweeper_stop = threading.Event()
        self._refresh = None
        self._index_rebuilds = None

    def ingestion(self):
        """Owner-attested snapshots use only the paired canonical DB and root."""
        if self.pid != os.getpid():
            raise PolicyError("configuration_restart_required")
        if not switches.on(switches.ENABLED):
            raise PolicyError("permissions_v2_disabled")
        if not switches.on(switches.INGEST_SNAPSHOTS):
            raise PolicyError("ingest_snapshots_disabled")
        expected = self.protocol.canonical_database.parent / "permissions-v2" / "ingest-snapshots"
        configured = switches.snapshot_root(self.protocol.canonical_database)
        if not configured or Path(configured) != expected:
            raise PolicyError("ingest_snapshots_not_configured")
        from .evidence import EvidenceBinding
        from .ingest_provenance import IngestProvenanceService
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            if self._ingestion_snapshot_root is not None and self._ingestion_snapshot_root != expected:
                raise PolicyError("configuration_restart_required")
            if self._ingestion_service is None:
                self._ingestion_service = IngestProvenanceService(
                    canonical_database=self.protocol.canonical_database,
                    binding=EvidenceBinding.parse(self.protocol.ledger.identity.model_dump()),
                    snapshot_root=expected)
                self._ingestion_snapshot_root = expected
            return self._ingestion_service

    def ingestion_connection(self):
        """A new thread-owned connection; never create or select another DB."""
        self.ingestion()  # Recheck feature/root/process configuration on every open.
        conn = sqlite3.connect(self.protocol.canonical_database.as_uri() + "?mode=rw", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def evidence_reviews(self, *, require_existing=True):
        """Trusted runtime accessor; request payloads never select enrollment."""
        if self.pid != os.getpid():
            raise PolicyError("configuration_restart_required")
        if not switches.on(switches.ENABLED):
            raise PolicyError("permissions_v2_disabled")
        # On by default: under implicit review the store holds only the owner's deselections, and a
        # node without it could neither honour a deselection nor build a search index. Set the
        # variable off to switch the whole evidence surface off.
        if not switches.on(switches.EVIDENCE_REVIEWS):
            raise PolicyError("evidence_reviews_disabled")
        if self.evidence_review_store_path is None:
            raise PolicyError("evidence_reviews_not_configured")
        from .evidence import EvidenceBinding
        from .evidence_review_runtime import ReviewEnrollmentRuntime
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            if self._evidence_review_runtime is None:
                self._evidence_review_runtime = ReviewEnrollmentRuntime(canonical_database=self.protocol.canonical_database,
                    binding=EvidenceBinding.parse(self.protocol.ledger.identity.model_dump()), path=self.evidence_review_store_path)
            # Only once a floor exists: a node that never enabled identity
            # attestations reads exactly as it did before, and one that has a
            # floor checks it on every evidence read.
            if self.protocol.canonical_floor is not None:
                self._evidence_review_runtime.resolver.canonical_floor = self.protocol.canonical_floor
            return self._evidence_review_runtime.get(require_existing=require_existing)

    def ensure_evidence_reviews(self) -> bool:
        """Enroll the private review store at startup, as the owner's own process.

        Implicit review needs no owner action: new facts are available as they are
        ingested, and the store exists so that a deselection can be recorded and a
        search index built before the owner has opened any review surface. The
        node's own process on its own socket IS the owner's application, which is
        what the enrollment's principal check asks for; no request can reach here.
        A store that cannot be enrolled is logged and left for the owner surfaces
        to report (`evidence_reviews_not_enrolled`), never a reason to refuse start.
        """
        import logging
        if not switches.on(switches.EVIDENCE_REVIEWS) or self.evidence_review_store_path is None:
            return False
        from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
        token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=self.protocol.ledger.identity.owner_id))
        try:
            self.evidence_reviews(require_existing=False)
            return True
        except PolicyError as exc:
            logging.getLogger(__name__).warning("permissions v2 evidence review store not enrolled at startup: %s", exc.code)
            return False
        finally:
            reset_principal(token)

    def projection_reviews(self, *, require_existing=True):
        """Output enrollment cannot implicitly enroll evidence or recipient state."""
        if self.pid != os.getpid():
            raise PolicyError("configuration_restart_required")
        if not switches.on(switches.PROJECTION_REVIEWS):
            raise PolicyError("projection_reviews_disabled")
        if self.projection_review_store_path is None:
            raise PolicyError("projection_reviews_not_configured")
        from .projection_review_runtime import ProjectionEnrollmentRuntime
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            evidence = self.evidence_reviews(require_existing=True)
            if self._projection_review_runtime is None:
                self._projection_review_runtime = ProjectionEnrollmentRuntime(
                    canonical_database=self.protocol.canonical_database, binding=evidence.resolver.binding,
                    path=self.projection_review_store_path, evidence_service=evidence)
            return self._projection_review_runtime.get(require_existing=require_existing)

    def canonical_floor(self):
        """One floor store for this process, shared by everything that reads it.

        Separate stores would each keep their own in-process fold of the event
        log and their own idea of the current revision, so two of them would
        race each other's republishes. It is installed on first use by the
        owner's own process, never by a request.
        """
        from .canonical_floor import CanonicalFloorStore
        import sqlite3 as _sqlite3
        if self._canonical_floor is None and self.protocol.canonical_floor is not None:
            # Attached at startup because this node already has one (load_runtime).
            self._canonical_floor = self.protocol.canonical_floor
        if self._canonical_floor is None:
            identity = self.protocol.ledger.identity
            directory = self.protocol.canonical_database.parent / "permissions-v2"
            if not directory.is_dir():
                raise PolicyError("identity_attestations_not_configured")
            floor = CanonicalFloorStore(directory / "canonical-floor.json", owner_id=identity.owner_id,
                                        node_id=identity.node_id, resource_id=identity.resource_id)
            if not floor.path.exists():
                conn = _sqlite3.connect(self.protocol.canonical_database.as_uri() + "?mode=ro", uri=True)
                try:
                    floor.install(conn)
                finally:
                    conn.close()
            self._canonical_floor = floor
            # Everything that reads canonical permission state checks the same
            # floor: the protocol before it signs anything, and the resolver on
            # every read. Attaching it in one place is what keeps them agreeing.
            self.protocol.canonical_floor = floor
        return self._canonical_floor

    def identity_attestations(self):
        """Owner identity consent. Its own flag, and it enrolls nothing.

        The floor file sits beside the canonical database, in the same private
        directory as the other permission state, and is installed on first use
        by the owner's own process rather than by any request.
        """
        if self.pid != os.getpid():
            raise PolicyError("configuration_restart_required")
        if not switches.on(switches.ENABLED):
            raise PolicyError("permissions_v2_disabled")
        if not switches.on(switches.IDENTITY_ATTESTATIONS):
            raise PolicyError("identity_attestations_disabled")
        from .evidence import EvidenceBinding, EvidenceResolver
        from .identity_attestation import IdentityAttestationService
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            floor = self.canonical_floor()
            if self._identity_service is None:
                identity = self.protocol.ledger.identity
                resolver = EvidenceResolver(self.protocol.canonical_database,
                                            binding=EvidenceBinding.parse(identity.model_dump()))
                resolver.canonical_floor = floor
                self._identity_service = IdentityAttestationService(resolver=resolver, floor=floor)
            return self._identity_service

    def message_search_index(self):
        """p2c-v1's per-grant index service. Its own flag; enrolls nothing; needs owner evidence reviews."""
        if self.pid != os.getpid():
            raise PolicyError("configuration_restart_required")
        if not switches.on(switches.MESSAGE_SEARCH):
            raise PolicyError("message_search_disabled")
        from .search_index import SearchIndexService, root_for, local_passage_embedder
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            reviews = self.evidence_reviews(require_existing=True)
            if (self._message_search_index is None or self._message_search_index.resolver is not reviews.resolver
                    or self._message_search_index.reviews is not reviews.reviews):
                self._message_search_index = SearchIndexService(ledger=self.protocol.ledger, resolver=reviews.resolver,
                    reviews=reviews.reviews, root=root_for(self.protocol.canonical_database),
                    passage_embedder=local_passage_embedder)
                self._start_sweeper()
            return self._message_search_index

    def record_keys_root(self):
        """The one RecordKeys store (opaque_ids) both the locator view and search use: one key per grant."""
        from .search_index import root_for
        return root_for(self.protocol.canonical_database)

    def hold_for_the_exclude_carry(self):
        """Refuse a share read while the upgrade step that carries the older per-person excludes into Off-limits is
        owed on this node and has not finished (review R2-M2): until it has run, a person the owner had excluded
        is not withheld. Raises with the node's own code for it (`contact_excludes.OWED`, `FAILED`); the share
        doors answer a recipient with their one refusal, as for any other reason."""
        from topos.features.lifecycle.contact_excludes import hold

        reason = hold(self.protocol.canonical_database)
        if reason is not None:
            raise PolicyError(reason)

    def message_search(self):
        """A fresh adapter over the one index service; request payloads never select anything here."""
        self.hold_for_the_exclude_carry()
        import time as _time
        from .search_release import MessageSearchRelease
        from .search_timing import for_adapter
        started = _time.perf_counter()
        # Owner-local timing only (search_timing.py): no query, identity, record, policy or content.
        timing = for_adapter()
        observe = None
        if timing is None:
            index = self.message_search_index()
        else:
            with timing.gate("runtime_setup"):
                index = self.message_search_index()
            observe = timing.observe
            observe("runtime_setup", _time.perf_counter() - started)
        return MessageSearchRelease(protocol=self.protocol, resolver=index.resolver, reviews=index.reviews,
                                    index=index, clock=lambda: int(_time.time()), observe=observe)

    def answers(self):
        """One process-local answer queue for every share on this node."""
        self.hold_for_the_exclude_carry()
        from .answer_release import AnswerService
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            if getattr(self, "_answers", None) is None:
                self._answers = AnswerService(self)
            return self._answers

    def automatic_message_reviews(self):
        from .automatic_review_worker import AutomaticReviewWorker
        from topos.storage.db.write_gate import with_db_write
        with with_db_write():
            service = self.evidence_reviews(require_existing=True)
            worker = getattr(self, "_automatic_reviews", None)
            if worker is None:
                def refresh():
                    index = self.message_search_index()
                    index.sweep()
                    index.rebuild_all()
                worker = AutomaticReviewWorker(service.resolver, service.reviews, refresh=refresh)
                self._automatic_reviews = worker
            return worker

    def index_rebuilds(self):
        """N3: the one queue of index work owner changes ask for (index_rebuilds.py), created on first use.

        Asked for under the write gate by the owner handlers, so it takes no gate and starts nothing that does: the
        queue's thread starts on its first request and asks for the index service only when it builds. A closed
        runtime starts none."""
        if self.pid != os.getpid():
            raise PolicyError("configuration_restart_required")
        from .index_rebuilds import IndexRebuilds
        from .refresh_loop import protection_sync
        from .search_index import root_for
        with _rebuilds_start:
            if self._sweeper_stop.is_set():
                raise PolicyError("configuration_restart_required")
            if self._index_rebuilds is None:
                self._index_rebuilds = IndexRebuilds(ledger=self.protocol.ledger,
                                                     root=root_for(self.protocol.canonical_database),
                                                     index=self.message_search_index,
                                                     sync_protection=protection_sync(self.protocol))
            return self._index_rebuilds

    def refresh_loop(self):
        """Plan WS7 RD2 + RD4/N7 (refresh_loop.py). None unless its own flags are on.

        One loop per process: the startup thread and a bind (A2A-1 §4.2 step 16) may both ask for it at once,
        and the second caller gets the loop the first one started. A closed runtime starts none: a loop begun
        after ``close`` would outlive it."""
        from .refresh_loop import RefreshLoop, RefreshSettings, protection_sync
        settings = RefreshSettings.from_env()
        if not settings.enabled:
            return None
        with _refresh_start:
            closed = getattr(self, "_sweeper_stop", None)
            if closed is not None and closed.is_set():
                return None
            if self._refresh is None:
                index = self.message_search_index()
                # A restore first makes the protection sync recipient admission makes (eb0a1f2a, lost on the way
                # to main): after a protection clock move every rebuild is otherwise `stale`. It changes no policy.
                self._refresh = RefreshLoop(ledger=self.protocol.ledger, root=index.root,
                                            index=self.message_search_index,
                                            worker=self.automatic_message_reviews if settings.catchup else None,
                                            settings=settings, sync_protection=protection_sync(self.protocol),
                                            owed=lambda: _owed_rebuilds(self))
                self._refresh.start(index)  # the sweeper started above waits 10 s before its first sweep
        return self._refresh

    def _start_sweeper(self, interval: float = 10.0):
        """The index is a scrub surface: a daemon timer deletes stale files even when no request comes."""
        if self._sweeper is not None:
            return
        from .search_timing import timed_sweep
        stop = self._sweeper_stop

        def loop():
            while not stop.wait(interval):
                index = self._message_search_index
                if index is not None:
                    timed_sweep(index)
                    if self._refresh is not None:
                        self._refresh.after_sweep(index)
        self._sweeper = threading.Thread(target=loop, name="p2c-index-sweep", daemon=True)
        self._sweeper.start()

    def close(self):
        with _refresh_start:   # a loop being started now is either seen here and closed, or never started
            self._sweeper_stop.set()
            refresh = self._refresh
        if refresh is not None:
            refresh.close()
        with _rebuilds_start:
            rebuilds = self._index_rebuilds
        if rebuilds is not None:
            rebuilds.close()
        if getattr(self, "_automatic_reviews", None):
            self._automatic_reviews.close()
        if getattr(self, "_answers", None):
            self._answers.close()
        self.lock_file.close()


_runtime: Runtime | None = None
_lock = threading.Lock()
#: Held while a runtime creates its one refresh loop (``Runtime.refresh_loop``).
_refresh_start = threading.Lock()
#: Held while a runtime creates its one owner-change rebuild queue (``Runtime.index_rebuilds``). Its own lock: that
#: accessor is called under the write gate, and the refresh loop's creation takes the gate while holding the one above.
_rebuilds_start = threading.Lock()


def _owed_rebuilds(runtime) -> frozenset:
    """The grants a runtime's owner-change queue has yet to build (its refresh loop leaves those to it); none before
    the queue exists."""
    queue = getattr(runtime, "_index_rebuilds", None)
    return queue.owed() if queue is not None else frozenset()


#: The snapshot lane's folder inside the sharing folder: the one ``INGEST_SNAPSHOT_ROOT`` a bound node defaults to and
#: the only one ``Runtime.ingestion`` accepts.
SNAPSHOT_ROOT_NAME = Path(switches.INGEST_SNAPSHOT_ROOT.bound).name


def ensure_snapshot_root(durable: Path) -> bool:
    """Make ``<sharing folder>/ingest-snapshots`` 0700 when it is not there; True when this call made it.

    The snapshot lane (the owner's standing iMessage proof, its recovery and refresh) writes its captures there and
    refuses unless the folder is a private directory. The design had an operator provision it, so on a node that
    bound itself it never existed and every standing run failed ``FileNotFoundError`` (T4 F1). The bind makes it
    before its commit, and every load makes it when a bound node lacks it. What is already there is never followed,
    changed or replaced: the lane checks it where it uses it."""
    root = durable / SNAPSHOT_ROOT_NAME
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        return False
    os.chmod(root, 0o700)     # exact, whatever the process's umask took away
    return True


def _private_file(path: Path) -> bytes:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise PolicyError("private_config_required")
    return path.read_bytes()


def _existing_floor(durable: Path, ledger: PolicyLedger):
    """The floor store for a node that already has one, attached before the protocol starts.

    The protocol refuses to start when its ledger has recorded a floor and none
    is attached, so that a node that lost its floor never signs anything. The
    runtime used to attach the floor lazily, on the first identity or review
    call, so every process after the one that recorded it refused at startup:
    one restart took down every signed route. A recorded floor whose file is gone
    still gets a store here, and that store's check refuses, as intended. A node
    with neither keeps starting without one and installs it on first use.
    """
    from .canonical_floor import CanonicalFloorStore

    path = durable / "canonical-floor.json"
    with ledger._transaction() as conn:
        recorded = conn.execute("SELECT 1 FROM p2a_canonical_floor WHERE singleton=1").fetchone() is not None
    if not recorded and not path.exists() and not path.is_symlink():
        return None
    identity = ledger.identity
    return CanonicalFloorStore(path, owner_id=identity.owner_id, node_id=identity.node_id, resource_id=identity.resource_id)


def load_runtime(config_path: Path, *, active_database: Path) -> Runtime:
    """Explicit trusted config; no environment secret/key discovery fallback."""
    config_path = config_path.resolve(strict=True)
    config = NodeProtocolConfig.parse(_private_file(config_path))
    if not config.identity.environment_id.startswith(BETA_ENVIRONMENT_PREFIX) or not config.trusted_cp_keys:
        raise PolicyError("beta_configuration_required")
    for variable in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = os.environ.get(variable, "1")
        if not value.isdecimal() or int(value) != 1:
            raise PolicyError("single_process_required")
    canonical = Path(config.canonical_database_path)
    ledger_path = Path(config.ledger_path)
    key_path = Path(config.node_signing_key_path)
    if not all(path.is_absolute() for path in (canonical, ledger_path, key_path)):
        raise PolicyError("absolute_paths_required")
    canonical = canonical.resolve(strict=True)
    if canonical != active_database.resolve(strict=True):
        raise PolicyError("canonical_database_binding")
    durable = canonical.parent / "permissions-v2"
    durable.mkdir(mode=0o700, exist_ok=True)
    if durable.is_symlink() or durable.stat().st_mode & 0o077:
        raise PolicyError("private_directory_required")
    try:
        ensure_snapshot_root(durable)
    except OSError as exc:   # the snapshot lane then refuses with its own code; sharing itself still loads
        import logging
        logging.getLogger(__name__).warning("permissions v2 snapshot folder not made (%s)", type(exc).__name__)
    if ledger_path.resolve().parent != durable or key_path.resolve(strict=True).parent != durable:
        raise PolicyError("durable_path_binding")
    # A config without a store path gets the default inside the durable directory: nothing the
    # owner must edit by hand for implicit review to hold their deselections.
    review_path = (Path(config.evidence_review_store_path) if config.evidence_review_store_path is not None
                   else durable / DEFAULT_EVIDENCE_REVIEW_STORE)
    projection_path = Path(config.projection_review_store_path) if config.projection_review_store_path is not None else None
    protected_paths = {canonical, ledger_path, key_path, config_path, durable / "protocol.lock"}
    for private_review_path in (review_path, projection_path):
        if private_review_path is None:
            continue
        from .evidence import _checked_file
        marker_path = private_review_path.with_name(private_review_path.name + ".enrollment.json")
        if (not private_review_path.is_absolute() or private_review_path.parent != durable
            or {private_review_path, marker_path} & protected_paths):
            raise PolicyError("review_database_binding")
        _checked_file(private_review_path, code="review_database_binding", may_create=True)
        _checked_file(marker_path, code="review_database_binding", may_create=True)
        protected_paths.update({private_review_path, marker_path})
    try:
        seed = bytes.fromhex(_private_file(key_path).decode("ascii").strip())
        key = Ed25519PrivateKey.from_private_bytes(seed)
    except (UnicodeError, ValueError):
        raise PolicyError("node_signing_key_invalid") from None
    lock_file = (durable / "protocol.lock").open("a+")
    os.chmod(lock_file.name, 0o600)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise PolicyError("single_process_required") from None
    try:
        # A node that gained an identity table since its clock was installed watches it from this load on,
        # with no hand step (protection_clock.repair_identity_coverage). Anything else wrong with the clock
        # is left as it is and refused below, as before.
        repair_identity_coverage(canonical, owner_id=config.identity.owner_id)
        # Existing floor is loaded only to reopen the ledger; the first signed
        # request synchronizes actual protection before claiming current state.
        if ledger_path.exists():
            with sqlite3.connect(ledger_path.as_uri() + "?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT protection_revision FROM p2a_node WHERE singleton=1").fetchone()
                if row is None:
                    raise PolicyError("ledger_identity")
                revision = row[0]
        else:
            ensure_protection_clock(canonical, owner_id=config.identity.owner_id)
            with sqlite3.connect(canonical.as_uri() + "?mode=ro", uri=True) as conn:
                conn.execute("BEGIN")
                revision = current_protection_revision(conn, owner_id=config.identity.owner_id)
        keys = {kid: bytes.fromhex(value) for kid, value in config.trusted_cp_keys.items()}
        ledger = PolicyLedger(ledger_path, identity=config.identity, protection_revision=revision, trusted_keys=keys)
        protocol = NodePolicyProtocol(ledger, canonical_database=canonical, cp_issuer_id=config.cp_issuer_id, frontend_client_id=config.frontend_client_id, trusted_cp_keys=keys, node_signing_kid=config.node_signing_kid, node_signing_key=key, canonical_floor=_existing_floor(durable, ledger))
        return Runtime(protocol, lock_file, config_path, evidence_review_store_path=review_path,
                       projection_review_store_path=projection_path)
    except BaseException:
        lock_file.close()
        raise


def get_runtime() -> Runtime:
    """The node's one runtime, loaded on first use from its private config (``switches.is_bound`` says when one binds).

    The master switch is on when the environment says so, or by default on a bound node. The config is the one
    ``TOPOS_PERMISSIONS_V2_CONFIG_PATH`` names, or, with that unset, the one the node keeps beside the database it
    serves (``switches.default_config_path``).
    """
    global _runtime
    if not switches.on(switches.ENABLED):
        raise PolicyError("permissions_v2_disabled")
    raw_path = switches.explicit(switches.CONFIG_PATH)
    if raw_path is None:
        located = switches.default_config_path()
        if located is None or not os.path.lexists(located):
            raise PolicyError("beta_configuration_required")
        raw_path = str(located)
    if not Path(raw_path).is_absolute():
        raise PolicyError("beta_configuration_required")
    path = Path(raw_path).resolve(strict=True)
    with _lock:
        if _runtime is not None:
            if _runtime.pid != os.getpid() or _runtime.config_path != path:
                raise PolicyError("configuration_restart_required")
            return _runtime
        _runtime = load_runtime(path, active_database=_served_database())
        _runtime.ensure_evidence_reviews()
        _forget_committed_pending(_runtime)
        return _runtime


def _forget_committed_pending(runtime: Runtime) -> None:
    """At the first load, a bind's pending record whose key this config commits is done with: removed, so that no
    later bind takes the committed key up again (``self_bind.forget_committed_pending``; review N2 finding 1)."""
    try:
        from .self_bind import forget_committed_pending
        forget_committed_pending(runtime.protocol.canonical_database.parent / "permissions-v2",
                                 runtime.protocol.node_signing_kid)
    except Exception:  # noqa: BLE001 -- bookkeeping, never a reason to refuse a load
        pass


def _served_database() -> Path:
    """The database this node serves: the one every other reader of "which database?" binds.

    ``storage.db.paths.resolve_active_database`` answers with the explicit TOPOS_DATABASE_PATH
    when one is set and otherwise with the active profile slot. An app-launched node never sets
    TOPOS_DATABASE_PATH (only ``--db-path`` does), and requiring it refused every coordination
    message on a normally installed node with canonical_database_binding: no grant could ever
    activate there, while the beta stacks, which set the path, worked. ``load_runtime`` still
    refuses unless this file IS the configured canonical database, so a node serving another
    Topos stays unbound. Side-effect free: no adoption, no logging.
    """
    from topos.storage.db.paths import resolve_active_database

    served = resolve_active_database().path
    if not served:
        raise PolicyError("canonical_database_binding")
    return Path(served)
