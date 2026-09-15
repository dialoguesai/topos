"""Owner-attested immutable iMessage snapshots, confined to the isolated beta.

An attestation is not provider account verification or a release grant. Only a
new row written inside a live, durable job claim may acquire this provenance.
Legacy syncs, imported fields, old rows and arbitrary paths cannot mint it.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, digest
from .evidence import EvidenceBinding, EvidenceResolver, _checked_file, _owner
from .protection_clock import current_protection_revision

OWNER_ATTESTATION = "I attest that this snapshot contains my iMessage account and that native sent-by-me messages are mine."
READER_CONTRACT = "imessage-owner-snapshot/v1"
OWNERSHIP_BASIS = "owner_attested_snapshot"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
LEASE_SECONDS = 300
_PREFIX = "ingest_provenance_"
_SOURCE_TABLES = ("engine_config", "user_ingestion_sources", "source_settings", "source_runtime_installs")
_SCHEMA = {
    "ingest_provenance_state": "CREATE TABLE ingest_provenance_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), store_id TEXT NOT NULL, binding_json TEXT NOT NULL, file_revision TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0))",
    "ingest_provenance_enrollments": "CREATE TABLE ingest_provenance_enrollments (enrollment_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, dataset_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL CHECK(revision>0), state TEXT NOT NULL CHECK(state IN ('active','revoked')), source_generation INTEGER NOT NULL, attestation TEXT NOT NULL, authorized_at INTEGER NOT NULL, channel TEXT NOT NULL)",
    "ingest_provenance_jobs": "CREATE TABLE ingest_provenance_jobs (job_id TEXT PRIMARY KEY, enrollment_id TEXT NOT NULL UNIQUE, enrollment_revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('queued','running','done','failed')), claim_token TEXT, lease_until INTEGER, result_json TEXT)",
    "ingest_provenance_records": "CREATE TABLE ingest_provenance_records (message_id TEXT PRIMARY KEY, enrollment_id TEXT NOT NULL, enrollment_revision INTEGER NOT NULL, job_id TEXT NOT NULL, row_identity TEXT NOT NULL)",
    "ingest_provenance_commands": "CREATE TABLE ingest_provenance_commands (command_id TEXT PRIMARY KEY, command_hash TEXT NOT NULL)",
}


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value) is None:
        raise PolicyError("ingest_identifier_invalid")
    return value


def _read_json(value):
    try:
        data = json.loads(value)
        if type(data) is not dict or canonical_bytes(data).decode("ascii") != value:
            raise ValueError()
        return data
    except (ValueError, TypeError):
        raise PolicyError("ingest_ledger_invalid") from None


def _json(value):
    return canonical_bytes(value).decode("ascii")


def _record_identity(conn, message_id):
    columns = ("message_id", "conversation_id", "dataset_id", "source_id", "source_record_id", "owner_user_id", "sender_id", "sender_type", "is_from_self", "event_at", "content", "metadata_json", "actor_role")
    row = conn.execute(f"SELECT {','.join(columns)} FROM conversation_messages WHERE message_id=?", (message_id,)).fetchone()
    if row is None:
        raise PolicyError("ingest_canonical_collision")
    return digest(dict(zip(columns, row)))


@dataclass(frozen=True, eq=False)
class VerifiedIngestContext:
    """Opaque in-process capability, always checked against its durable claim."""
    service: "IngestProvenanceService"
    job_id: str
    enrollment_id: str
    enrollment_revision: int
    owner_id: str
    source_id: str
    dataset_id: str
    claim_token: str

    def assert_current(self, conn, *, source_id, dataset_id):
        self.service.assert_current(conn, self, source_id=source_id, dataset_id=dataset_id)

    def require_batch(self, conn):
        self.service._require_batch(conn, self)

    def batch(self, conn):
        return self.service._batch(conn, self)

    def record_insert(self, conn, message_id):
        self.require_batch(conn)
        self.assert_current(conn, source_id=self.source_id, dataset_id=self.dataset_id)
        conn.execute("INSERT INTO ingest_provenance_records VALUES (?,?,?,?,?)", (message_id, self.enrollment_id, self.enrollment_revision, self.job_id, _record_identity(conn, message_id)))

    def existing_record(self, conn, message_id):
        self.require_batch(conn)
        self.assert_current(conn, source_id=self.source_id, dataset_id=self.dataset_id)
        row = conn.execute("SELECT enrollment_id,enrollment_revision,job_id,row_identity FROM ingest_provenance_records WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            return False
        if tuple(row) != (self.enrollment_id, self.enrollment_revision, self.job_id, _record_identity(conn, message_id)):
            raise PolicyError("ingest_canonical_collision")
        return True


class IngestProvenanceService:
    def __init__(self, *, canonical_database: Path, binding: EvidenceBinding, snapshot_root: Path):
        self.resolver = EvidenceResolver(canonical_database, binding=binding)
        self.binding = self.resolver.binding
        if not self.binding.environment_id.startswith("permissions-beta-"):
            raise PolicyError("beta_configuration_required")
        self.root = Path(snapshot_root)
        if self.root != Path(canonical_database).parent / "permissions-v2" / "ingest-snapshots":
            raise PolicyError("ingest_snapshot_root_binding")
        self.marker = self.root.parent / "ingest-snapshots.enrollment.json"
        self._root_identity = None
        self._marker = None
        self._contexts = {}
        self._batches = {}
        self._finished = set()
        self._transactions = set()

    def _connection(self, conn):
        self.resolver._file_revision()
        paths = [(row[1], row[2]) for row in conn.execute("PRAGMA database_list")]
        if paths != [("main", str(self.resolver.path))]:
            raise PolicyError("ingest_canonical_binding")
        current_protection_revision(conn, owner_id=self.binding.owner_id)

    def _snapshot(self, snapshot_id):
        _identifier(snapshot_id)
        # A basename identifier is not a filesystem path, including on Windows.
        if ":" in snapshot_id or ".." in snapshot_id:
            raise PolicyError("ingest_identifier_invalid")
        path = self.root / (snapshot_id + ".db")
        try:
            for directory in (self.root, self.root.parent):
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                    raise PolicyError("ingest_snapshot_private_required")
            info = self.root.lstat()
            identity = (info.st_dev, info.st_ino)
            if self._root_identity is not None and identity != self._root_identity:
                raise PolicyError("ingest_snapshot_root_binding")
            self._root_identity = identity
            _checked_file(path, code="ingest_snapshot_invalid")
            if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink() for suffix in ("-wal", "-shm", "-journal")):
                raise PolicyError("ingest_snapshot_not_closed")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o400 or before.st_nlink != 1 or not 100 <= before.st_size <= MAX_SNAPSHOT_BYTES:
                    raise PolicyError("ingest_snapshot_invalid")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(MAX_SNAPSHOT_BYTES + 1)
                after = os.fstat(fd)
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                if any(getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(path.lstat(), field) for field in fields):
                    raise PolicyError("ingest_snapshot_changed")
            finally:
                os.close(fd)
            if len(data) != before.st_size or data[:16] != b"SQLite format 3\x00" or data[18:20] != b"\x01\x01":
                raise PolicyError("ingest_snapshot_not_closed")
            # Durable snapshot identity is the exact bytes. Device/inode were
            # compared above only within this read; a remount renumbers them.
            descriptor = {"snapshot_id": snapshot_id, "snapshot_sha256": hashlib.sha256(data).hexdigest(), "snapshot_bytes": len(data), "reader_contract": READER_CONTRACT, "ownership_basis": OWNERSHIP_BASIS}
            return descriptor, data
        except OSError:
            raise PolicyError("ingest_snapshot_unavailable") from None

    def describe_snapshot(self, conn, *, snapshot_id):
        _owner(self.binding)
        self._connection(conn)
        snapshot, _ = self._snapshot(snapshot_id)
        return {key: snapshot[key] for key in ("snapshot_id", "snapshot_sha256", "snapshot_bytes", "reader_contract", "ownership_basis")}

    def _schema(self, conn):
        result = dict(_SCHEMA)
        for table in _SOURCE_TABLES:
            found = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
            if found is None:
                continue
            if found[0] != "table":
                raise PolicyError("ingest_source_schema_invalid")
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = f"{_PREFIX}{table}_{operation.lower()}"
                timing, condition = "AFTER", ""
                if table == "engine_config":
                    # Startup repeats INSERT OR REPLACE of the SAME owner.
                    # BEFORE INSERT sees the old row even with recursive
                    # triggers disabled. Only a real owner change invalidates.
                    timing = "BEFORE"
                    condition = {"INSERT": " WHEN NEW.key='user_id' AND NOT EXISTS(SELECT 1 FROM engine_config WHERE key='user_id' AND value IS NEW.value)",
                        "UPDATE": " WHEN (OLD.key='user_id' OR NEW.key='user_id') AND (OLD.key IS NOT NEW.key OR OLD.value IS NOT NEW.value)",
                        "DELETE": " WHEN OLD.key='user_id'"}[operation]
                result[name] = f"CREATE TRIGGER {name} {timing} {operation} ON {table}{condition} BEGIN UPDATE ingest_provenance_state SET generation=generation+1 WHERE singleton=1; SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'ingest source clock unavailable') END; END"
        return result

    def _marker_read(self):
        info = _checked_file(self.marker, code="ingest_enrollment_required")
        if info.st_mode & 0o077 or info.st_size > 16384:
            raise PolicyError("ingest_ledger_invalid")
        marker = _read_json(self.marker.read_text())
        if marker.get("state") != "active" or marker.get("binding") != self.binding.model_dump() or marker.get("file_revision") != self.resolver._file_revision():
            raise PolicyError("ingest_ledger_binding")
        if type(marker.get("revision")) is not int or marker["revision"] < 1 or type(marker.get("generation")) is not int or marker["generation"] < 0:
            raise PolicyError("ingest_ledger_binding")
        if self._marker is not None:
            immutable = {"store_id", "binding", "file_revision", "schema_digest"}
            if (any(marker.get(key) != self._marker.get(key) for key in immutable)
                or marker["revision"] < self._marker["revision"] or marker["generation"] < self._marker["generation"]
                or (marker["revision"] == self._marker["revision"] and marker != self._marker)):
                raise PolicyError("ingest_ledger_binding")
        self._marker = marker
        return marker

    @staticmethod
    def _authority_digest(conn):
        # The external marker detects an in-place restore of earlier canonical
        # bytes too. Source generation is a separately increasing floor because
        # native configuration writers legitimately advance it outside us.
        tables = {}
        for table in _SCHEMA:
            if table == "ingest_provenance_state":
                continue
            rows = [list(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            tables[table] = rows
        return digest(tables)

    def _publish_marker(self, marker):
        temporary = self.marker.with_name(self.marker.name + "." + secrets.token_hex(16))
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_bytes(marker))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.marker)
            self._sync_directory()
        finally:
            if temporary.exists():
                temporary.unlink()
        if marker["state"] == "active":
            self._marker = marker

    def _check(self, conn):
        with with_db_write():
            return self._check_locked(conn)

    def _check_locked(self, conn):
        self._connection(conn)
        marker = self._marker_read()
        found = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'ingest_provenance_*'"))
        if found != self._schema(conn) or digest(found) != marker.get("schema_digest"):
            raise PolicyError("ingest_ledger_invalid")
        rows = conn.execute("SELECT store_id,binding_json,file_revision,generation FROM ingest_provenance_state").fetchall()
        if len(rows) != 1 or tuple(rows[0][:3]) != (marker.get("store_id"), _json(self.binding.model_dump()), self.resolver._file_revision()):
            raise PolicyError("ingest_ledger_binding")
        generation = rows[0][3]
        if type(generation) is not int or generation < marker["generation"]:
            raise PolicyError("ingest_source_clock_invalid")
        if conn not in self._transactions:
            if self._authority_digest(conn) != marker.get("authority_digest"):
                raise PolicyError("ingest_ledger_rollback")
            if generation > marker["generation"]:
                # Callers hold the node gate. Persist observed revocation even
                # when the following operation is withheld and writes no rows.
                self._publish_marker({**marker, "generation": generation, "revision": marker["revision"] + 1})
        return generation

    @contextmanager
    def _transaction(self, conn, *, _install=False):
        with with_db_write():
            if conn.in_transaction:
                raise PolicyError("ingest_transaction_required")
            self._connection(conn)
            if not _install:
                self._check(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._transactions.add(conn)
            try:
                yield
                self._connection(conn)
                if not _install:
                    marker = self._marker_read()
                    next_marker = {**marker, "state": "pending", "revision": marker["revision"] + 1,
                        "generation": conn.execute("SELECT generation FROM ingest_provenance_state WHERE singleton=1").fetchone()[0],
                        "authority_digest": self._authority_digest(conn)}
                    # Pending is durable BEFORE the canonical commit. Neither
                    # crash ordering can resurrect consumed commands/claims.
                    self._publish_marker(next_marker)
                conn.commit()
                if not _install:
                    self._publish_marker({**next_marker, "state": "active"})
            except BaseException:
                conn.rollback()
                raise
            finally:
                self._transactions.discard(conn)

    def _install(self, conn):
        """First explicit owner enrollment only. A torn install stays closed."""
        _owner(self.binding)
        with with_db_write():
            for directory in (self.root, self.root.parent):
                try:
                    info = directory.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                        raise PolicyError("ingest_snapshot_private_required")
                except OSError:
                    raise PolicyError("ingest_snapshot_unavailable") from None
            if self.marker.exists() or self.marker.is_symlink():
                self._check(conn)
                return
            if self._marker is not None or conn.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'ingest_provenance_*'").fetchone():
                raise PolicyError("ingest_enrollment_required")
            schema = self._schema(conn)
            marker = {"state": "pending", "store_id": secrets.token_hex(32), "binding": self.binding.model_dump(), "file_revision": self.resolver._file_revision(), "schema_digest": digest(schema), "revision": 1, "generation": 0}
            fd = os.open(self.marker, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_bytes(marker))
                stream.flush()
                os.fsync(stream.fileno())
            self._sync_directory()
            with self._transaction(conn, _install=True):
                for sql in schema.values():
                    conn.execute(sql)
                conn.execute("INSERT INTO ingest_provenance_state VALUES(1,?,?,?,0)", (marker["store_id"], _json(self.binding.model_dump()), marker["file_revision"]))
            marker["state"] = "active"
            marker["authority_digest"] = self._authority_digest(conn)
            self._publish_marker(marker)
            self._check(conn)

    def consume_command(self, conn, *, command_id, command_hash, allow_install=False):
        """Burn an already signature-verified owner command before dispatch.

        Only the first explicit enrollment may initialize durable state. A
        failed/uncertain dispatch needs a fresh command and a status lookup.
        """
        _owner(self.binding)
        _identifier(command_id)
        if type(command_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", command_hash) is None:
            raise PolicyError("ingest_command_invalid")
        if allow_install:
            self._install(conn)
        with self._transaction(conn):
            self._check(conn)
            if conn.execute("SELECT 1 FROM ingest_provenance_commands WHERE command_id=?", (command_id,)).fetchone():
                raise PolicyError("ingest_command_replayed")
            conn.execute("INSERT INTO ingest_provenance_commands VALUES(?,?)", (command_id, command_hash))

    def _sync_directory(self):
        fd = os.open(self.marker.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _enrollment(self, conn, enrollment_id, *, active=False):
        _identifier(enrollment_id)
        generation = self._check(conn)
        row = conn.execute("SELECT enrollment_id,snapshot_json,dataset_id,revision,state,source_generation,attestation FROM ingest_provenance_enrollments WHERE enrollment_id=?", (enrollment_id,)).fetchone()
        if row is None or row[6] != OWNER_ATTESTATION:
            raise PolicyError("ingest_enrollment_unknown")
        if active and (row[4] != "active" or row[5] != generation):
            raise PolicyError("ingest_enrollment_stale")
        if active:
            self._source_enabled(conn, row[2])
        return dict(zip(("enrollment_id", "snapshot_json", "dataset_id", "revision", "state", "source_generation", "attestation"), row))

    @staticmethod
    def _source_enabled(conn, dataset_id):
        # Mirror only the native enable switch, never its error-to-enabled
        # fallback. Posture remains an independent, unchanged release ceiling.
        found = conn.execute("SELECT type FROM sqlite_master WHERE name='user_ingestion_sources'").fetchone()
        if found is None:
            return
        if found[0] != "table" or not {"dataset_id", "source_id", "enabled"} <= {row[1] for row in conn.execute("PRAGMA table_info(user_ingestion_sources)")}:
            raise PolicyError("ingest_source_schema_invalid")
        rows = conn.execute("SELECT enabled FROM user_ingestion_sources WHERE source_id='imessage' AND dataset_id=?", (dataset_id,)).fetchmany(2)
        if len(rows) > 1 or (rows and (type(rows[0][0]) is not int or rows[0][0] != 1)):
            raise PolicyError("ingest_source_disabled")

    @staticmethod
    def _enrollment_metadata(row):
        return {key: row[key] for key in ("enrollment_id", "dataset_id", "revision", "state")} | {"source_id": "imessage", "ownership_basis": OWNERSHIP_BASIS}

    def enroll(self, conn, *, snapshot_id, dataset_id, snapshot_sha256, owner_attestation):
        _owner(self.binding)
        _identifier(dataset_id)
        if owner_attestation != OWNER_ATTESTATION:
            raise PolicyError("ingest_owner_attestation_required")
        self._connection(conn)
        self._source_enabled(conn, dataset_id)
        snapshot, _ = self._snapshot(snapshot_id)
        if snapshot_sha256 != snapshot["snapshot_sha256"]:
            raise PolicyError("ingest_snapshot_changed")
        self._install(conn)
        with self._transaction(conn):
            generation = self._check(conn)
            # One dataset means one immutable source enrollment. Re-enrollment
            # requires a fresh dataset; native row-ID collisions still fail.
            existing = conn.execute("SELECT enrollment_id FROM ingest_provenance_enrollments WHERE dataset_id=?", (dataset_id,)).fetchone()
            if existing:
                old = self._enrollment(conn, existing[0])
                if old["snapshot_json"] != _json(snapshot):
                    raise PolicyError("ingest_dataset_already_enrolled")
                if old["state"] == "active" and old["source_generation"] != generation:
                    raise PolicyError("ingest_enrollment_stale")
                # Recover an uncertain acknowledgement, including a previously
                # revoked result. This never creates a replacement enrollment.
                return self._enrollment_metadata(old)
            enrollment_id = "ingest-enrollment-" + secrets.token_hex(16)
            from topos.principal import current_principal
            conn.execute("INSERT INTO ingest_provenance_enrollments VALUES(?,?,?,1,'active',?,?,?,?)", (enrollment_id, _json(snapshot), dataset_id, generation, OWNER_ATTESTATION, int(time.time()), current_principal().channel))
            row = self._enrollment(conn, enrollment_id, active=True)
        return self._enrollment_metadata(row)

    def revoke(self, conn, *, enrollment_id):
        _owner(self.binding)
        with self._transaction(conn):
            row = self._enrollment(conn, enrollment_id)
            if row["state"] != "revoked":
                conn.execute("UPDATE ingest_provenance_enrollments SET state='revoked',revision=revision+1 WHERE enrollment_id=?", (enrollment_id,))
            row = self._enrollment(conn, enrollment_id)
        return self._enrollment_metadata(row)

    def _job_metadata(self, conn, job_id):
        row = conn.execute("SELECT job_id,enrollment_id,status,result_json FROM ingest_provenance_jobs WHERE job_id=?", (_identifier(job_id),)).fetchone()
        if row is None:
            raise PolicyError("ingest_job_unknown")
        return {"job_id": row[0], "enrollment_id": row[1], "status": row[2], "result": _read_json(row[3]) if row[3] is not None else None}

    def enqueue(self, conn, *, enrollment_id):
        _owner(self.binding)
        with self._transaction(conn):
            row = self._enrollment(conn, enrollment_id, active=True)
            expected = _read_json(row["snapshot_json"])
            if self._snapshot(expected["snapshot_id"])[0] != expected:
                raise PolicyError("ingest_snapshot_changed")
            found = conn.execute("SELECT job_id FROM ingest_provenance_jobs WHERE enrollment_id=?", (enrollment_id,)).fetchone()
            job_id = found[0] if found else "ingest-job-" + secrets.token_hex(16)
            if found is None:
                conn.execute("INSERT INTO ingest_provenance_jobs VALUES(?,?,?,'queued',NULL,NULL,NULL)", (job_id, enrollment_id, row["revision"]))
            return self._job_metadata(conn, job_id)

    def status(self, conn, *, job_id):
        _owner(self.binding)
        self._check(conn)
        return self._job_metadata(conn, job_id)

    def claim(self, conn, job_id):
        with self._transaction(conn):
            self._check(conn)
            row = conn.execute("SELECT enrollment_id,enrollment_revision,status,lease_until FROM ingest_provenance_jobs WHERE job_id=?", (_identifier(job_id),)).fetchone()
            if row is None:
                raise PolicyError("ingest_job_unknown")
            enrollment = self._enrollment(conn, row[0], active=True)
            if row[1] != enrollment["revision"]:
                raise PolicyError("ingest_enrollment_stale")
            if row[2] == "done" or (row[2] == "running" and (type(row[3]) is not int or row[3] > int(time.time()))):
                raise PolicyError("ingest_job_not_claimable")
            token = secrets.token_hex(32)
            conn.execute("UPDATE ingest_provenance_jobs SET status='running',claim_token=?,lease_until=?,result_json=NULL WHERE job_id=?", (token, int(time.time()) + LEASE_SECONDS, job_id))
            context = VerifiedIngestContext(self, job_id, row[0], row[1], self.binding.owner_id, "imessage", enrollment["dataset_id"], token)
        self._contexts[token] = context
        return context

    def assert_current(self, conn, context, *, source_id, dataset_id, _allow_done=False):
        if type(context) is not VerifiedIngestContext or context.service is not self or self._contexts.get(context.claim_token) is not context:
            raise PolicyError("ingest_context_invalid")
        if (source_id, dataset_id, context.owner_id) != ("imessage", context.dataset_id, self.binding.owner_id):
            raise PolicyError("ingest_context_invalid")
        enrollment = self._enrollment(conn, context.enrollment_id, active=True)
        row = conn.execute("SELECT enrollment_id,enrollment_revision,status,claim_token,lease_until FROM ingest_provenance_jobs WHERE job_id=?", (context.job_id,)).fetchone()
        if row is None or tuple(row[:2]) != (context.enrollment_id, context.enrollment_revision) or row[3] != context.claim_token or enrollment["revision"] != context.enrollment_revision or enrollment["dataset_id"] != context.dataset_id:
            raise PolicyError("ingest_claim_stale")
        if row[2] != "running" and not (_allow_done and context.claim_token in self._finished and row[2] == "done"):
            raise PolicyError("ingest_claim_stale")
        if type(row[4]) is not int or row[4] <= int(time.time()):
            raise PolicyError("ingest_claim_expired")

    def snapshot_bytes(self, conn, context):
        self.assert_current(conn, context, source_id=context.source_id, dataset_id=context.dataset_id)
        enrollment = self._enrollment(conn, context.enrollment_id, active=True)
        expected = _read_json(enrollment["snapshot_json"])
        actual, data = self._snapshot(expected["snapshot_id"])
        if expected != actual:
            raise PolicyError("ingest_snapshot_changed")
        return data

    def validate_record_origin(self, conn, *, message_id, origin):
        """Read-only native evidence floor for rows from this new ingest lane.

        The embedded hint is not authority. It requires the private enrollment
        marker, intact schema, exact record link and completed job; revocation
        or source replacement withholds already-reviewed outputs as well.
        """
        if type(origin) is not dict or set(origin) != {"version", "enrollment_id", "job_id"} or origin.get("version") != "owner-attested-snapshot/v1":
            raise PolicyError("ingest_origin_invalid")
        enrollment = self._enrollment(conn, origin["enrollment_id"], active=True)
        job = conn.execute("SELECT enrollment_id,enrollment_revision,status FROM ingest_provenance_jobs WHERE job_id=?", (_identifier(origin["job_id"]),)).fetchone()
        if job is None or tuple(job) != (enrollment["enrollment_id"], enrollment["revision"], "done"):
            raise PolicyError("ingest_origin_invalid")
        link = conn.execute("SELECT enrollment_id,enrollment_revision,job_id,row_identity FROM ingest_provenance_records WHERE message_id=?", (_identifier(message_id),)).fetchone()
        if link is None or tuple(link) != (enrollment["enrollment_id"], enrollment["revision"], origin["job_id"], _record_identity(conn, message_id)):
            raise PolicyError("ingest_origin_invalid")
        snapshot = _read_json(enrollment["snapshot_json"])
        if self._snapshot(snapshot["snapshot_id"])[0] != snapshot:
            raise PolicyError("ingest_snapshot_changed")

    def _require_batch(self, conn, context):
        if self._batches.get(context.claim_token) is not conn or not conn.in_transaction:
            raise PolicyError("ingest_transaction_required")

    @contextmanager
    def _batch(self, conn, context):
        if context.claim_token in self._batches:
            self._require_batch(conn, context)
            self.assert_current(conn, context, source_id=context.source_id, dataset_id=context.dataset_id)
            yield
            return
        with self._transaction(conn):
            self.snapshot_bytes(conn, context)
            self._batches[context.claim_token] = conn
            try:
                yield
                self._require_batch(conn, context)
                self.assert_current(conn, context, source_id=context.source_id, dataset_id=context.dataset_id, _allow_done=True)
                enrollment = self._enrollment(conn, context.enrollment_id, active=True)
                expected = _read_json(enrollment["snapshot_json"])
                if self._snapshot(expected["snapshot_id"])[0] != expected:
                    raise PolicyError("ingest_snapshot_changed")
            finally:
                self._batches.pop(context.claim_token, None)
                self._finished.discard(context.claim_token)

    def finish(self, conn, context, result):
        self._require_batch(conn, context)
        self.assert_current(conn, context, source_id=context.source_id, dataset_id=context.dataset_id)
        counts = {"messages_created", "conversations_created", "messages_processed", "historical_skipped"}
        if type(result) is not dict or set(result) != counts | {"status"} or result["status"] != "ok" or any(type(result[key]) is not int or not 0 <= result[key] <= 1000 for key in counts):
            raise PolicyError("ingest_result_invalid")
        conn.execute("UPDATE ingest_provenance_jobs SET status='done',result_json=? WHERE job_id=? AND claim_token=?", (_json(result), context.job_id, context.claim_token))
        self._finished.add(context.claim_token)

    def fail(self, conn, context, reason):
        # CAS only: a stale worker must never fail a newer claim or a done job.
        if type(context) is not VerifiedIngestContext or self._contexts.get(context.claim_token) is not context:
            raise PolicyError("ingest_context_invalid")
        if type(reason) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason) is None:
            reason = "ingest_execution_failed"
        with self._transaction(conn):
            self._check(conn)
            conn.execute("UPDATE ingest_provenance_jobs SET status='failed',result_json=? WHERE job_id=? AND claim_token=? AND status='running'", (_json({"status": "error", "reason_code": reason}), context.job_id, context.claim_token))
