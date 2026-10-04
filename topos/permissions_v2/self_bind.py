"""The node binds itself for sharing at its owner's first share (any-to-any N2; contract A2A-1 §4.2).

The control plane sends ``permissions_v2_bind`` carrying a bind it signed with its relay stamp key: before a node
is bound that is the only control-plane key it trusts (``topos/relay_stamp.py`` pinned it at first boot). The node
checks the bind, makes its own node key (and, at a first bind, its node id), backs its database up, installs or
verifies the protection clock, writes its private sharing config, loads the sharing runtime, starts the protection
doorbell and the search refresh loop, and answers with a proof signed by the new key (``bind_protocol``). It writes
no environment variable and no ``.env`` line: a node is bound when its config sits beside the database it serves
(``switches.is_bound``), and every switch reads that on its next call, so nothing needs a restart.

Files, all in ``<folder of the served database>/permissions-v2/`` (0700):

- ``config.json`` (0600): the node config. Its rename into place is the one moment the node becomes bound.
- ``node-signing.key`` (0600): the node key, the hex of a 32-byte Ed25519 seed. It never leaves the node.
- ``bind-pending.json`` (0600): the identity a bind is making, written before the key and deleted after the
  proof. A leftover one belongs to a bind that stopped before its config was committed: the next bind for the
  same identity takes it up with its key; any other leftover, and its key, is moved to ``stale/`` first. One whose
  key a committed config names (a bind died between its commit and its last step) is removed the first time the
  node finds itself bound: at the runtime's first load, or by an ``already_bound`` answer. And no leftover is taken
  up beside a ledger, whose runtime may have served that key (review N2 finding 1).
- ``config.json.failed-<time>``: a config that was committed but would not load. The node is unbound again.
- ``ingest-snapshots/`` (0700): the snapshot lane's folder, made before the config commits (and at every runtime
  load that finds it missing), where the owner's standing iMessage proof writes its captures (T4 F1). A bind that
  fails after making it removes it again.
- ``stale/<time>-<random>-previous-ledger/``: a ledger and share indexes already here when a bind commits a new
  config (a folder that lost only its config). Moved aside whole, kept, never read again (A2A-1 amendment 5.4).

The backup goes beside the migration backups (``storage/db/migrations/backup.py``) as
``database-pre-sharing-bind[--<profile>]-<UTC time>.db``. Migration retention only ever counts
``database-pre-v*`` names, so it never deletes one; the disk report lists them as the owner's own snapshots. It is
written as ``….db.partial`` and renamed when whole; a partial copy a process death left is counted as free room by
the next bind's disk check and removed before its backup (review N2 finding 5).

Right after a bind commits and loads, the node sends one heartbeat carrying its new key id, rather than leaving the
control plane to wait up to 30 s for the next one, before which it routes nothing to the node (T4 F2).

``already_bound`` is answered only for a node that can serve: one whose runtime cannot load, or whose protection
clock no longer verifies, answers ``bind_failed`` with ``cause`` set to the node's code, and nothing is written
(review N2 finding 4).

Every refusal is ``{"id", "type", "status": "error", "code", "error"}`` with the codes of §4.2. Refusals before the
backup write nothing. ``answer`` never raises, and no refusal and no log line carries a path, a key, an id or a
payload: a log line names a refusal code, an outcome or an exception class, nothing else.

Where §4.2 is silent this module decides as follows (A2A-1 amendment 5 where it says so; otherwise the N2 report):

- The kill switch (``TOPOS_PERMISSIONS_V2_ENABLED`` set off), or a config path the environment names somewhere a
  bind does not write, refuses before anything is written (``bind_failed``). A bind never writes an environment.
  (Amendment 5.2.)
- A config that is present but does not bind this database is never replaced: ``bound_elsewhere`` when it binds
  another database (a folder another Topos left here, §4.6, which is reported and never repaired), and
  ``bind_failed`` when it is damaged. (Amendment 5.2.)
- A node key with no pending identity beside it (its config was deleted by hand) is moved to ``stale/`` like any
  other leftover before a new key is made: nothing on the node can say which identity it held. (N2 report, Q3.)
- A bind is used once: a frame whose nonce this process already took is refused as spent (``bind_expired``). The
  control plane makes a fresh nonce for every bind, so only a replay meets this. (Amendment 5.1.)
- A bind that commits a new config sets any ledger and share indexes already in the folder aside first, so the new
  ledger starts fresh. (Amendment 5.4.)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import bind_protocol, switches
from .bind_protocol import MESSAGE_TYPE, SignedBind
from .canonical import PolicyError

_log = logging.getLogger(__name__)

CONFIG_NAME = "config.json"
CONFIG_TMP_NAME = "config.json.tmp"
FAILED_PREFIX = "config.json.failed-"
PENDING_NAME = "bind-pending.json"
KEY_NAME = "node-signing.key"
LEDGER_NAME = "ledger.db"
#: A previous ledger on disk: the ledger with its journal files. The share indexes beside it (``search_index``'s
#: ``ROOT_NAME``: each share's index, the record keys, the refresh loop's state) go with it.
PREVIOUS_LEDGER_FILES = (LEDGER_NAME, LEDGER_NAME + "-journal", LEDGER_NAME + "-wal", LEDGER_NAME + "-shm")
STALE_NAME = "stale"
CONFIG_VERSION = "topos-policy-node-config/v1"
#: The snapshot lane's folder inside the sharing folder (``runtime.SNAPSHOT_ROOT_NAME``).
SNAPSHOT_ROOT_NAME = "ingest-snapshots"
#: The short thread that sends the heartbeat after a bind (``_announce_key``).
HEARTBEAT_THREAD = "permissions-v2-bind-heartbeat"
BACKUP_PREFIX = "database-pre-sharing-bind"
#: A backup is written under its final name plus this, and renamed when whole.
PARTIAL_SUFFIX = ".partial"
_PENDING_FIELDS = frozenset({"node_id", "kid", "environment_id", "resource_id", "owner_id", "created_at"})

#: One bind at a time in this process (§4.2 step 5).
_LOCK = threading.Lock()
#: The nonce of every bind this process has taken, until that bind expires: a bind is used once. The control plane
#: makes a fresh nonce for every bind it sends, so only a replayed frame ever finds its nonce here.
_TAKEN: dict[str, int] = {}


class BindRefused(Exception):
    """One refusal of §4.2: the frame's numeric code and its error, and for a bound node that cannot serve, the code
    it cannot serve with (``cause``). It carries nothing else."""

    def __init__(self, status: int, code: str, *, cause: str | None = None):
        super().__init__(code)
        self.status, self.code, self.cause = status, code, cause


#: ``verify_bind``'s codes and their answers (§4.2 step 3). Every other code is a malformed bind.
_VERIFY_REFUSALS = {
    "bind_frame_mismatch": (400, "bind_frame_mismatch"),
    "bind_expired": (403, "bind_expired"),
    "stamp_key_unavailable": (403, "stamp_key_unavailable"),
    "bind_signature_invalid": (403, "bind_signature_invalid"),
}


def answer(message, principal) -> dict:
    """The node's whole answer to one bind frame: the proof, or a refusal. Never raises."""
    frame_id = message.get("id") if isinstance(message, dict) else None
    try:
        proof = _bind(message, principal)
    except BindRefused as refused:
        _log.info("permissions v2 bind refused: %s%s", refused.code,
                  f" (cause: {refused.cause})" if refused.cause else "")
        frame = {"id": frame_id, "type": MESSAGE_TYPE, "status": "error", "code": refused.status,
                 "error": refused.code}
        if refused.cause:
            frame["cause"] = refused.cause      # a node code (e.g. protection_clock_unavailable), never data
        return frame
    except Exception as exc:  # noqa: BLE001 -- never a path, a key or a payload in the answer
        _log.warning("permissions v2 bind failed (%s)", type(exc).__name__)
        return {"id": frame_id, "type": MESSAGE_TYPE, "status": "error", "code": 503, "error": "bind_failed"}
    _log.info("permissions v2 bind answered: %s", proof.outcome)
    return {"id": frame_id, "type": MESSAGE_TYPE, "status": "ok", "payload": {"proof": proof.model_dump()}}


def _bind(message, principal):
    from topos.principal import OWNER_APP
    from topos.relay_stamp import _load_public_key_bytes
    from .protection_doorbell import AUTO_RESYNC_CLIENT

    # 0 is the dispatcher's: the stamp verified with the pinned key and its class is the owner's app.
    # 1. Only the control plane's relay, never the owner socket; never the automatic re-sync's client, which may
    #    only ask a status.
    if (principal is None or principal.cls != OWNER_APP or principal.channel != "cp_relay"
            or principal.client_id == AUTO_RESYNC_CLIENT):
        raise BindRefused(403, "bind_channel")
    # 2. The payload is exactly {"bind": {...}}.
    payload = message.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"bind"} or not isinstance(payload["bind"], dict):
        raise BindRefused(400, "bind_payload_invalid")
    # 3. Its shape, the frame it rode in, its time and the pinned stamp key's signature.
    try:
        bind = bind_protocol.verify_bind(payload["bind"], stamp_public_key=_load_public_key_bytes(),
                                         message_id=message.get("id"), now=int(time.time()))
    except PolicyError as exc:
        raise BindRefused(*_VERIFY_REFUSALS.get(exc.code, (400, "bind_payload_invalid"))) from None
    # 4. The owner the bind names is the owner the stamp names.
    if bind.owner_id != principal.acting_user:
        raise BindRefused(403, "bind_owner_mismatch")
    # 5. One bind at a time. The database write gate is never held across the steps below; the calls that need
    #    it (the clock install, the runtime load) take it themselves.
    if not _LOCK.acquire(blocking=False):
        raise BindRefused(409, "bind_busy")
    try:
        _take(bind)
        return _bind_locked(bind)
    finally:
        _LOCK.release()


def _take(bind: SignedBind) -> None:
    """A replayed bind is refused as spent, with the code of an expired one (§4.2 names no replay step: see the
    N2 report). Called under the bind lock; an entry lives only as long as its bind could still verify."""
    now = int(time.time())
    for nonce in [nonce for nonce, expires in _TAKEN.items() if expires <= now]:
        del _TAKEN[nonce]
    if bind.nonce in _TAKEN:
        raise BindRefused(403, "bind_expired")
    _TAKEN[bind.nonce] = bind.expires_at


def _bind_locked(bind: SignedBind):
    served = _served_database()
    _check_engine_owner(served, bind.owner_id)                       # 6
    _check_install_scopes(served, bind)                              # 7
    _check_loaded_runtime(served)                                    # 8
    durable = served.parent / switches.DURABLE_DIRECTORY
    bound = _current_binding(served, durable)                        # 9
    if bound is not None:
        return _already_bound(bind, bound, durable)
    if not bind.new_key_allowed:                                     # 10
        raise BindRefused(409, "not_bound")
    _check_disk(served)                                              # 11
    _backup(served)                                                  # 12
    node_id, node_key, kid = _make_identity(durable, bind)           # 13
    _install_clock(served, bind.owner_id)                            # 14
    made_snapshot_root = _snapshot_root(durable)                     # the snapshot lane's folder (T4 F1)
    try:
        config_path = _commit_config(durable, served, bind, node_id, kid)    # 15
        _load_runtime(durable, config_path, bind, node_id)           # 16
    except BindRefused:
        if made_snapshot_root:      # within the bind's rollback: a folder this bind made goes with it
            _remove_empty_directory(durable / SNAPSHOT_ROOT_NAME)
        raise
    _start_loops()
    _announce_key()                                                  # T4 F2: the new key id, now
    _drop_pending(durable)                                           # 17
    return bind_protocol.sign_bind_proof(bind=bind, node_id=node_id, node_key=node_key, kid=kid, outcome="bound",
                                         engine_version=_engine_version(), now=int(time.time()))


# --- the checks: steps 6 to 9, which write nothing --------------------------------------------------------------

def _served_database() -> Path:
    """The database this node serves, as the runtime binds it (``runtime._served_database``), resolved."""
    from .runtime import _served_database as served
    try:
        return served().resolve(strict=True)
    except (PolicyError, OSError):
        raise BindRefused(503, "bind_failed") from None


@contextmanager
def _read_only(path: Path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        yield conn
    finally:
        conn.close()


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _check_engine_owner(served: Path, owner_id: str) -> None:
    """Step 6: the node's own owner is the bind's. The clock install enforces the same rule (``node_owner_binding``);
    the node never rewrites ``engine_config.user_id`` to make it so."""
    with _read_only(served) as conn:
        row = (conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()
               if _has_table(conn, "engine_config") else None)
    if row is None or row[0] != owner_id:
        raise BindRefused(403, "owner_mismatch")


def _json_object(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _check_install_scopes(served: Path, bind: SignedBind) -> None:
    """Step 7: every source this node installed for a named Topos or owner names this bind's.

    Evidence posture enforces the same rule per row (``evidence.py``), where ``app_id`` is another spelling of
    ``topos_id``. A scope with no concrete value, a legacy table with no scope column, and a scope that does not
    parse (evidence refuses that row on its own) pass this check."""
    with _read_only(served) as conn:
        if not _has_table(conn, "source_runtime_installs"):
            return
        columns = {row[1] for row in conn.execute("PRAGMA table_info(source_runtime_installs)")}
        if "scope_key" not in columns:
            return
        scopes = [row[0] for row in conn.execute("SELECT scope_key FROM source_runtime_installs")]
    expected = {"topos_id": bind.resource_id, "app_id": bind.resource_id, "user_id": bind.owner_id}
    for raw in scopes:
        scope = _json_object(raw)
        if scope is None:
            continue
        for field, value in expected.items():
            found = scope.get(field)
            if found is None or (isinstance(found, str) and found.strip() in ("", "*")):
                continue
            if not isinstance(found, str) or found.strip() != value:
                raise BindRefused(409, "topos_mismatch")


def _parse_config(path: Path):
    from .runtime import NodeProtocolConfig, _private_file
    return NodeProtocolConfig.parse(_private_file(path))


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


def _read_key(path: Path) -> Ed25519PrivateKey | None:
    """A node key file as the runtime reads it (private, the hex of a 32-byte seed), or None."""
    from .runtime import _private_file
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(_private_file(path).decode("ascii").strip()))
    except Exception:  # noqa: BLE001 -- missing, not private or not a key: not usable
        return None


def _looked_at(durable: Path) -> Path:
    """Where this node keeps its config: the path the environment names, or beside the database it serves."""
    named = switches.explicit(switches.CONFIG_PATH)
    conventional = durable / CONFIG_NAME
    if named is None or os.path.realpath(named) == os.path.realpath(conventional):
        return conventional
    return Path(named)


def _check_loaded_runtime(served: Path) -> None:
    """Step 8: a runtime this process already loaded must still be the one the node's config describes.

    It holds the ledger and the process lock for its own identity, so a bind may not write a new identity under
    it, and a config that changed under it needs a restart before anything else."""
    from . import runtime as runtime_module
    loaded = runtime_module._runtime
    if loaded is None:
        return
    try:
        current = _looked_at(served.parent / switches.DURABLE_DIRECTORY).resolve(strict=True)
        config = _parse_config(current)
        protocol = loaded.protocol
        key = _read_key(Path(config.node_signing_key_path))
        same = (loaded.pid == os.getpid() and current == loaded.config_path
                and config.identity == protocol.ledger.identity and config.cp_issuer_id == protocol.cp_issuer_id
                and config.frontend_client_id == protocol.frontend_client_id
                and {kid: bytes.fromhex(value) for kid, value in config.trusted_cp_keys.items()}
                == dict(protocol.trusted_cp_keys)
                and config.node_signing_kid == protocol.node_signing_kid
                and key is not None and _public(key) == _public(protocol.node_signing_key))
    except Exception:  # noqa: BLE001 -- gone, unreadable or different: all mean the loaded runtime is stale
        same = False
    if not same:
        raise BindRefused(409, "restart_required")


def binding_switched_off() -> bool:
    """The node's kill switch (``TOPOS_PERMISSIONS_V2_ENABLED`` set off, or unreadable, which reads as off): every
    bind is refused, and the heartbeat offers neither a bind nor a key id (review N2 finding 7)."""
    return switches.explicit(switches.ENABLED) is False


def _current_binding(served: Path, durable: Path):
    """Step 9's question: the config that binds this node now, as (path, config), or None when there is none."""
    if binding_switched_off():
        _log.info("permissions v2 bind: sharing is switched off on this node")
        raise BindRefused(503, "bind_failed")
    path = _looked_at(durable)
    conventional = durable / CONFIG_NAME
    switches.forget_bound()
    if switches.is_bound():
        try:
            return path, _parse_config(path.resolve(strict=True))
        except Exception:  # noqa: BLE001 -- changed since the predicate read it
            raise BindRefused(503, "bind_failed") from None
    if path != conventional:
        _log.info("permissions v2 bind: the environment names a sharing config elsewhere, and it does not load")
        raise BindRefused(503, "bind_failed")
    if not os.path.lexists(conventional):
        return None
    try:
        other = Path(_parse_config(conventional.resolve(strict=True)).canonical_database_path)
        elsewhere = other.is_absolute() and Path(os.path.realpath(other)) != served
    except Exception:  # noqa: BLE001 -- damaged: not a config for another database
        elsewhere = False
    if elsewhere:
        raise BindRefused(409, "bound_elsewhere")
    _log.info("permissions v2 bind: a sharing config is present that does not load; it is left as it is")
    raise BindRefused(503, "bind_failed")


def _already_bound(bind: SignedBind, bound, durable: Path):
    """Step 9's answers for a bound node: the same identity is answered with the key it has.

    Only a node that can serve is vouched for (review N2 finding 4): one whose runtime cannot load, or whose
    protection clock no longer verifies, answers ``bind_failed`` with the cause, and nothing is written. The one write
    on this path is the removal of a pending record whose key the committed config names (finding 1): a bind that
    died between its commit and its last step left it, and the key it holds is now this node's registered key."""
    _path, config = bound
    identity = config.identity
    if ((identity.environment_id, identity.resource_id, identity.owner_id)
            != (bind.environment_id, bind.resource_id, bind.owner_id)
            or (bind.node_id is not None and bind.node_id != identity.node_id)):
        raise BindRefused(409, "bound_elsewhere")
    if (config.cp_issuer_id != bind.cp_issuer_id or dict(config.trusted_cp_keys) != dict(bind.trusted_cp_keys)
            or config.frontend_client_id != bind.frontend_client_id):
        raise BindRefused(409, "bind_conflict")
    key = _read_key(Path(config.node_signing_key_path))
    if key is None:
        raise BindRefused(503, "bind_failed", cause="node_signing_key_invalid")
    cause = _serving_refusal()
    if cause is not None:
        raise BindRefused(503, "bind_failed", cause=cause)
    forget_committed_pending(durable, config.node_signing_kid)
    return bind_protocol.sign_bind_proof(bind=bind, node_id=identity.node_id, node_key=key,
                                         kid=config.node_signing_kid, outcome="already_bound",
                                         engine_version=_engine_version(), now=int(time.time()))


def _serving_refusal() -> str | None:
    """Why this bound node could not serve a share now, as a node code, or None when it can.

    The runtime is the node's own (``get_runtime``): already loaded, or loaded now exactly as the node's first
    request would load it. A load that refuses writes nothing that matters (it stops before its ledger and clock
    writes). Then the protection clock is verified read-only, since a clock that lost a trigger after the load
    refuses every read while the loaded runtime looks fine."""
    from . import runtime as runtime_module
    from .protection_clock import current_protection_revision
    try:
        runtime = runtime_module.get_runtime()
        with _read_only(Path(runtime.protocol.canonical_database)) as conn:
            conn.execute("BEGIN")
            current_protection_revision(conn, owner_id=runtime.protocol.ledger.identity.owner_id)
    except PolicyError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001 -- the class name only
        return type(exc).__name__
    return None


def forget_committed_pending(durable: Path, kid: str) -> bool:
    """Remove a pending record whose key the committed config names (review N2 finding 1). Never raises.

    A pending record exists so that a bind which stopped before its commit can be taken up with its key, a key that
    has signed nothing. Once a config names that key, the record has done its job, and keeping it would let a later
    bind take up a key the control plane may already hold. Called wherever the node finds itself bound with a
    committed config: the ``already_bound`` answer here, and the runtime's first load."""
    from .runtime import _private_file
    pending = durable / PENDING_NAME
    try:
        record = json.loads(_private_file(pending))
    except Exception:  # noqa: BLE001 -- none, or not one this code wrote: left to step 13's own rules
        return False
    if not isinstance(record, dict) or record.get("kid") != kid:
        return False
    try:
        pending.unlink()
        _fsync_directory(durable)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _log.warning("permissions v2 bind: a committed pending record was not removed (%s)", type(exc).__name__)
        return False
    _log.info("permissions v2 bind: removed the pending record of a key already committed")
    return True


# --- the disk and the backup: steps 11 and 12 -------------------------------------------------------------------

def _database_bytes(served: Path) -> int:
    total = 0
    for path in (served, served.with_name(served.name + "-wal")):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _free_bytes(directory: Path):
    """Free bytes on the volume the backups go to (its nearest existing folder), or None when unreadable."""
    from topos.engine.disk_space import free_bytes
    return free_bytes(directory)


def _disk_floor(served: Path) -> int:
    """The node's disk floor: the owner's setting in this database, or the shipped default."""
    from topos.engine.disk_space import min_free_bytes
    with _read_only(served) as conn:
        return int(min_free_bytes(conn))


def _stranded_partials(directory: Path) -> list:
    """Partial bind backups a process death left behind (review N2 finding 5): our own prefix and suffix only, never
    another file. Each is an incomplete copy of the whole database that nothing lists or ever completes."""
    try:
        return sorted(path for path in directory.glob(f"{BACKUP_PREFIX}*.db{PARTIAL_SUFFIX}")
                      if path.is_file() and not path.is_symlink())
    except OSError:
        return []


def _check_disk(served: Path) -> None:
    """Step 11: room for the backup twice over, and the node's floor left after it. Unreadable is not full. Stranded
    partial backups count as free: step 12 removes them before it writes."""
    from topos.storage.db.migrations.backup import backup_dir_for
    directory = backup_dir_for(served)
    free = _free_bytes(directory)
    reclaimable = 0
    for path in _stranded_partials(directory):
        try:
            reclaimable += path.stat().st_size
        except OSError:
            pass
    if free is not None and free + reclaimable < 2 * _database_bytes(served) + _disk_floor(served):
        raise BindRefused(409, "disk_low")


def _unused(path: Path, *, partner_suffix: str = "") -> Path:
    """``path``, or the first ``<stem>-<n><suffix>`` beside it that does not exist (nor its partner, when one is
    named): nothing is ever replaced."""
    candidate, number = path, 2
    while os.path.lexists(candidate) or (partner_suffix and
                                         os.path.lexists(candidate.with_name(candidate.name + partner_suffix))):
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        number += 1
    return candidate


def _backup(served: Path) -> Path:
    """Step 12: the whole database, with the online backup API from a read-only source, as the migration backup
    copies it (``backup_database_before_migrations``), into the same folder."""
    from topos.storage.db.migrations.backup import _safe_token, backup_dir_for
    from topos.storage.db.paths import active_profile_id_for

    directory = backup_dir_for(served)
    made_directory = not directory.exists()
    partial = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stranded = _stranded_partials(directory)
        for path in stranded:
            _discard(path)
        if stranded:
            _log.info("permissions v2 bind: removed %d partial backup(s) an earlier bind left", len(stranded))
        profile = active_profile_id_for(served)
        owner = f"--{_safe_token(profile)}" if profile else ""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        final = _unused(directory / f"{BACKUP_PREFIX}{owner}-{stamp}.db", partner_suffix=PARTIAL_SUFFIX)
        partial = final.with_name(final.name + PARTIAL_SUFFIX)
        os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        source = sqlite3.connect(served.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            target = sqlite3.connect(str(partial))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        _fsync_file(partial)
        os.rename(partial, final)
        partial = None
        _fsync_directory(directory)
    except Exception as exc:  # noqa: BLE001
        _log.warning("permissions v2 bind: the backup failed (%s)", type(exc).__name__)
        if partial is not None:
            _discard(partial)
        if made_directory:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise BindRefused(503, "backup_failed") from None
    _log.info("permissions v2 bind: database backed up before binding (%.1f MB)", final.stat().st_size / 1e6)
    return final


# --- files ------------------------------------------------------------------------------------------------------

def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("permissions v2 bind: a temporary file was not removed (%s)", type(exc).__name__)


def _write_private(path: Path, data: bytes) -> None:
    """A new file, 0600, never one that exists or a link, flushed to disk with its folder."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def _private_directory(path: Path) -> None:
    """The folder, 0700, made if absent (``load_runtime`` requires exactly that). A link or a file is refused."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise BindRefused(503, "bind_failed")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def _set_aside(durable: Path, paths, *, label: str = "") -> None:
    """Move what is there into ``stale/<time>-<random>[-<label>]/``, by rename. Kept, never deleted, and never read
    again: nothing on the node looks in ``stale/``."""
    present = [path for path in paths if os.path.lexists(path)]
    if not present:
        return
    stale = durable / STALE_NAME
    _private_directory(stale)
    folder = stale / (f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
                      + (f"-{label}" if label else ""))
    folder.mkdir(mode=0o700)
    for path in present:
        os.rename(path, folder / path.name)
    _fsync_directory(folder)
    _fsync_directory(durable)


# --- the identity, the clock and the commit: steps 13 to 15 -----------------------------------------------------

def _leftover(pending: Path, key_path: Path, bind: SignedBind):
    """A pending identity left by an earlier bind for this same identity, with its key: (node id, key, key id).

    Never one when a ledger is in the folder (review N2 finding 1): a runtime has run here, so the pending key may be
    one a config committed and the control plane registered. Taken up again, it would present the registry's own key
    over a reset ledger, and the control plane would see no new key and skip its store reset. A new key is made
    instead, which the control plane resets for."""
    from .ledger import NodeIdentity
    from .runtime import _private_file
    if not os.path.lexists(pending):
        return None
    if any(os.path.lexists(pending.parent / name) for name in PREVIOUS_LEDGER_FILES):
        return None
    try:
        record = json.loads(_private_file(pending))
    except Exception:  # noqa: BLE001 -- unreadable or not private: set aside
        return None
    if not isinstance(record, dict) or set(record) != _PENDING_FIELDS:
        return None
    if ((record["environment_id"], record["resource_id"], record["owner_id"])
            != (bind.environment_id, bind.resource_id, bind.owner_id)
            or (bind.node_id is not None and record["node_id"] != bind.node_id)):
        return None
    try:
        NodeIdentity.parse({key: record[key] for key in ("environment_id", "node_id", "resource_id", "owner_id")})
    except PolicyError:
        return None
    key = _read_key(key_path)
    if key is None or bind_protocol.node_key_id(_public(key)) != record["kid"]:
        return None
    return record["node_id"], key, record["kid"]


def _make_identity(durable: Path, bind: SignedBind):
    """Step 13: the node id (the bind's, or a new one at a first bind) and a new node key, or the ones a stopped
    bind of this same identity left behind. The pending record is written before the key, so a key never exists
    without the identity it was made for."""
    try:
        _private_directory(durable)
        pending, key_path, temporary = durable / PENDING_NAME, durable / KEY_NAME, durable / CONFIG_TMP_NAME
        taken = _leftover(pending, key_path, bind)
        if taken is not None:
            _set_aside(durable, [temporary])
            return taken
        _set_aside(durable, [pending, key_path, temporary])
        node_id = bind.node_id if bind.node_id is not None else bind_protocol.mint_node_id()
        seed = secrets.token_bytes(32)
        key = Ed25519PrivateKey.from_private_bytes(seed)
        kid = bind_protocol.node_key_id(_public(key))
        record = {"node_id": node_id, "kid": kid, "environment_id": bind.environment_id,
                  "resource_id": bind.resource_id, "owner_id": bind.owner_id, "created_at": int(time.time())}
        _write_private(pending, (json.dumps(record, sort_keys=True) + "\n").encode("ascii"))
        _write_private(key_path, (seed.hex() + "\n").encode("ascii"))
        return node_id, key, kid
    except BindRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.warning("permissions v2 bind: the identity was not made (%s)", type(exc).__name__)
        raise BindRefused(503, "bind_failed") from None


def _install_clock(served: Path, owner_id: str) -> None:
    """Step 14: install the protection clock, or verify the one there, first watching any identity table the node
    gained since it was installed. It writes into the live database once, and an installed clock is never
    removed; an incomplete one is refused, never repaired."""
    from .protection_clock import ensure_protection_clock, repair_identity_coverage
    try:
        repair_identity_coverage(served, owner_id=owner_id)
        ensure_protection_clock(served, owner_id=owner_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning("permissions v2 bind: protection clock unavailable (%s)",
                     getattr(exc, "code", type(exc).__name__))
        raise BindRefused(409, "protection_unavailable") from None


def _set_aside_previous_ledger(durable: Path) -> None:
    """A2A-1 amendment 5.4: the config a bind commits names a key no ledger here ever served, so any ledger and share
    indexes already in the folder are moved aside first, whole (the ledger with its journal files; the indexes, their
    record keys and the refresh state). The new ledger then starts at epoch 0 with no share, the state the control
    plane's ``reset_for_new_node_key`` expects. Only a folder that lost its config while keeping its ledger has one
    here, or one an earlier attempt of this bind made before it failed to load. The owner's reviews, the floor, the
    snapshot lane and the rest of the folder stay where they are."""
    from .search_index import ROOT_NAME
    _set_aside(durable, [durable / name for name in (*PREVIOUS_LEDGER_FILES, ROOT_NAME)], label="previous-ledger")


def _snapshot_root(durable: Path) -> bool:
    """The snapshot lane's folder, ``ingest-snapshots``, a private directory before the config commits (T4 F1). The
    owner's standing iMessage proof writes its captures there and refuses without it, and on a node that bound
    itself nothing else ever made it. Made 0700 when it lacks (True: this bind made it); one that is there and too
    wide is narrowed; a link or a file there refuses the bind (``bind_failed``), never followed."""
    from .runtime import ensure_snapshot_root
    try:
        made = ensure_snapshot_root(durable)
        info = os.lstat(durable / SNAPSHOT_ROOT_NAME)
    except OSError as exc:
        _log.warning("permissions v2 bind: the snapshot folder was not made (%s)", type(exc).__name__)
        raise BindRefused(503, "bind_failed") from None
    if not stat.S_ISDIR(info.st_mode):
        _log.info("permissions v2 bind: something that is not a folder holds the snapshot folder's place")
        raise BindRefused(503, "bind_failed")
    if info.st_mode & 0o077:
        os.chmod(durable / SNAPSHOT_ROOT_NAME, 0o700)
    return made


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()               # only ever empty: nothing writes there before the config commits and loads
    except OSError as exc:
        _log.warning("permissions v2 bind: a folder the failed bind made was not removed (%s)", type(exc).__name__)


def _commit_config(durable: Path, served: Path, bind: SignedBind, node_id: str, kid: str) -> Path:
    """Step 15: the config, written beside and renamed into place. The rename is the moment the node is bound."""
    from .runtime import NodeProtocolConfig
    target, temporary = durable / CONFIG_NAME, durable / CONFIG_TMP_NAME
    config = {
        "version": CONFIG_VERSION,
        "identity": {"environment_id": bind.environment_id, "node_id": node_id, "resource_id": bind.resource_id,
                     "owner_id": bind.owner_id},
        "cp_issuer_id": bind.cp_issuer_id,
        "frontend_client_id": bind.frontend_client_id,
        "trusted_cp_keys": dict(bind.trusted_cp_keys),
        "node_signing_kid": kid,
        "node_signing_key_path": str(durable / KEY_NAME),
        "canonical_database_path": str(served),
        "ledger_path": str(durable / LEDGER_NAME),
    }
    try:
        NodeProtocolConfig.parse(config)
        _set_aside_previous_ledger(durable)
        _write_private(temporary, (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("ascii"))
        if os.path.lexists(target):
            raise FileExistsError(CONFIG_NAME)   # step 9 found nothing here; a config is never replaced
        os.rename(temporary, target)
    except Exception as exc:  # noqa: BLE001
        _log.warning("permissions v2 bind: the config was not committed (%s)", type(exc).__name__)
        _discard(temporary)
        raise BindRefused(503, "bind_failed") from None
    try:
        _fsync_directory(durable)
    except OSError as exc:
        _log.warning("permissions v2 bind: the sharing folder was not flushed (%s)", type(exc).__name__)
    switches.forget_bound()
    return target


# --- the runtime and the loops: step 16 -------------------------------------------------------------------------

def _unload(target: Path) -> None:
    """Close a runtime that loaded from ``target`` before the config is taken away again."""
    from . import runtime as runtime_module
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        return
    with runtime_module._lock:
        loaded = runtime_module._runtime
        if loaded is None or loaded.config_path != resolved:
            return
        runtime_module._runtime = None
    try:
        loaded.close()
    except Exception:  # noqa: BLE001
        pass


def _set_failed(durable: Path, target: Path) -> None:
    """The config that did not load becomes ``config.json.failed-<time>``: the node is unbound again."""
    try:
        os.rename(target, _unused(durable / f"{FAILED_PREFIX}{int(time.time())}"))
        _fsync_directory(durable)
    except OSError as exc:
        _log.warning("permissions v2 bind: a config that did not load was not set aside (%s)", type(exc).__name__)
    switches.forget_bound()


def _load_runtime(durable: Path, target: Path, bind: SignedBind, node_id: str):
    """Step 16: load the runtime now, through the same path every request takes (``get_runtime``): the ledger is
    made and pinned to the identity, the clock verified, the process lock taken, the review store enrolled."""
    from . import runtime as runtime_module
    expected = {"environment_id": bind.environment_id, "node_id": node_id, "resource_id": bind.resource_id,
                "owner_id": bind.owner_id}
    try:
        runtime = runtime_module.get_runtime()
        if (runtime.config_path != target.resolve(strict=True)
                or runtime.protocol.ledger.identity.model_dump() != expected):
            raise PolicyError("bind_load_mismatch")
        return runtime
    except Exception as exc:  # noqa: BLE001
        _log.warning("permissions v2 bind: the new config did not load (%s)", getattr(exc, "code", type(exc).__name__))
        _unload(target)
        _set_failed(durable, target)
        raise BindRefused(503, "bind_load_failed") from None


def _start_loops() -> None:
    """The protection doorbell and the search refresh loop, now: at start-up they try once, 60 s in, which on a
    node that was not bound yet found nothing. Each runs once per process (see their ``start_after_bind``)."""
    from . import protection_doorbell, refresh_loop
    for name, start in (("protection doorbell", protection_doorbell.start_after_bind),
                        ("search refresh", refresh_loop.start_after_bind)):
        try:
            start()
        except Exception as exc:  # noqa: BLE001 -- the node is bound; the loop is retried at the next start
            _log.warning("permissions v2 bind: %s not started (%s)", name, type(exc).__name__)


def _announce_key() -> None:
    """One heartbeat right after a bind commits and loads, built as the presence loop builds it, so it carries the
    new key id (T4 F2). The control plane routes nothing to a node until a heartbeat has advertised its key id, and
    the next regular one may be 30 s away. Sent from a short thread of its own, off the bind's answer, through the
    client's queue for messages from other threads. No client (a node with no control plane): nothing."""
    def send():
        try:
            from topos.core import state as engine_state
            enqueue = getattr(getattr(engine_state, "control_plane_client", None),
                              "enqueue_unsolicited_message_threadsafe", None)
            if not callable(enqueue):
                return
            from topos.engine.registration import build_engine_heartbeat_message
            enqueue(build_engine_heartbeat_message())
        except Exception as exc:  # noqa: BLE001 -- the next regular heartbeat carries the key id anyway
            _log.warning("permissions v2 bind: the heartbeat after the bind was not sent (%s)", type(exc).__name__)
    threading.Thread(target=send, name=HEARTBEAT_THREAD, daemon=True).start()


def _drop_pending(durable: Path) -> None:
    """Step 17: the pending record has done its job once the config is committed and loaded."""
    try:
        (durable / PENDING_NAME).unlink()
        _fsync_directory(durable)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("permissions v2 bind: the pending record was not removed (%s)", type(exc).__name__)


def _engine_version() -> str:
    from topos import __version__
    return str(__version__)


# --- the heartbeat's hint (A2A-1 §3.5) ---------------------------------------------------------------------------

def node_key_id_hint() -> str | None:
    """``permissions_v2_node_key_id``: the ``node_signing_kid`` of the private config where this node keeps it, when
    that file is private and parses; None otherwise. Read from disk on every call; no lock, no runtime load.

    Where the node keeps it: the path ``TOPOS_PERMISSIONS_V2_CONFIG_PATH`` names when that is set, otherwise beside
    the database it serves. The owner's existing node keeps its config where its own line names it, and its hint
    must be its configured key id (§4.7), or the control plane would ask for a new key it does not need. None while
    the kill switch is on (review N2 finding 7)."""
    if binding_switched_off():
        return None
    try:
        named = switches.explicit(switches.CONFIG_PATH)
        path = Path(named) if named is not None else switches.default_config_path()
        if path is None or not path.is_absolute():
            return None
        return _parse_config(path.resolve(strict=True)).node_signing_kid
    except Exception:  # noqa: BLE001 -- a hint never fails a heartbeat
        return None
