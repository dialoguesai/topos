"""Wiki MVP storage migration runner — single registry (PLAN §4a)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

from .backup import (
    InsufficientDiskForBackup,
    backup_database_before_migrations,
    connection_db_path,
)
# Re-export migration IDs + registry used by tests/tools.
from .registry import *  # noqa: F403
from .registry import MIGRATIONS, MigrationSpec, max_migration_order  # noqa: F401
from .canonical_disclosure_v1 import apply_canonical_disclosure_v1_up  # noqa: F401
from .canonical_nsfw_v1 import apply_canonical_nsfw_v1_up  # noqa: F401
from .complexity_v1 import apply_complexity_v1_up  # noqa: F401
from .activity_events_content_v1 import apply_activity_events_content_v1_up  # noqa: F401
from .actor_role_v1 import apply_actor_role_v1_up  # noqa: F401
from .entity_blackhole_v1 import apply_entity_blackhole_v1_up  # noqa: F401
from .attention_triage_v1 import apply_attention_triage_v1_up  # noqa: F401
from .attention_triage_v2 import apply_attention_triage_v2_up  # noqa: F401
from .documents_v1 import apply_documents_v1_up  # noqa: F401
from .transcripts_v1 import apply_transcripts_v1_up  # noqa: F401
from .canonical_address_book_v1 import apply_canonical_address_book_v1_up  # noqa: F401

logger = logging.getLogger("topos.storage.db.migrations")

__all__ = [
    "MigrationError",
    "DowngradeGuardError",
    "MigrationSpec",
    "MIGRATIONS",
    "apply_all_migrations",
    "ensure_migrations_applied",
    "pending_ledger_migrations",
    "read_user_version",
    "reset_ensured_connections",
    "max_migration_order",
]


#: Connections this process has already migrated to the current registry head.
#:
#: This runner is NOT startup-only: ``AdapterFactory.create`` calls it, and that
#: sits on the ingest hot path — once per batch, per worker thread, per
#: connection. Each call re-applied every ``always_run`` step, and each step
#: takes the write gate and commits, so a single ingest issued hundreds of gated
#: write transactions only to re-assert schema that was already present. Any
#: connection holding SQLite's write lock meanwhile turns the next one into a
#: full 30s ``busy_timeout`` expiry — the "database is locked" that killed the
#: statistics job's fold on repeat ingests.
#:
#: Keyed by ``(id(conn), database file)``. ``sqlite3.Connection`` on 3.10
#: supports neither weak references nor attributes, so identity is the only
#: handle available; the path pins it, because a recycled ``id`` can only
#: collide with a cached entry naming the same file — which is by then already
#: migrated, making the hit harmless. Databases with no file (``:memory:``) are
#: never cached: their schema dies with the connection, so a recycled id there
#: would skip migrations on an empty database.
#:
#: The value is the ``PRAGMA schema_version`` observed once the run settled, and
#: the memo only holds while it still matches. That is what keeps skipping SAFE:
#: the ``always_run`` steps are not merely re-assertions, they ALTER tables that
#: legacy DDL creates *after* the first run — ``CanonicalTablesManager`` builds
#: ``ai_chat_conversations`` without the provenance columns and
#: ``wiki_mvp_phase1`` adds them on the next pass. SQLite bumps
#: ``schema_version`` on exactly those events (any DDL, from any connection) and
#: leaves it alone when a run changes nothing, so the hot path still skips while
#: a late CREATE re-arms the runner.
_ENSURED_CONNECTIONS: Dict[Tuple[int, str], int] = {}
_ENSURED_LOCK = threading.Lock()


def _ensured_key(conn: sqlite3.Connection) -> Optional[Tuple[int, str]]:
    path = connection_db_path(conn)
    if path is None:
        return None
    return (id(conn), str(path))


def _schema_version(conn: sqlite3.Connection) -> Optional[int]:
    try:
        row = conn.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None else None


def _mark_ensured(conn: sqlite3.Connection) -> None:
    key = _ensured_key(conn)
    version = _schema_version(conn)
    if key is None or version is None:
        return
    with _ENSURED_LOCK:
        _ENSURED_CONNECTIONS[key] = version


def reset_ensured_connections() -> None:
    """Forget which connections have been migrated. For tests."""
    with _ENSURED_LOCK:
        _ENSURED_CONNECTIONS.clear()


class MigrationError(RuntimeError):
    """Schema migration failed; the database must not be served half-migrated."""


class DowngradeGuardError(MigrationError):
    """Database ``user_version`` is newer than this build knows how to open."""


def _migration_applied(conn: sqlite3.Connection, migration_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
            (migration_id,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _stamp_user_version(conn: sqlite3.Connection, order: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(order)}")


def _shipped_version() -> str:
    try:
        from ....__version__ import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _needs_apply(conn: sqlite3.Connection, spec: MigrationSpec) -> bool:
    if spec.always_run:
        return True
    if not _migration_applied(conn, spec.id):
        return True
    if spec.also_if_missing_table and not _table_exists(conn, spec.also_if_missing_table):
        return True
    return False


def pending_ledger_migrations(conn: sqlite3.Connection) -> List[MigrationSpec]:
    """Ledger-guarded migrations that have not yet been recorded as applied.

    Used to decide whether a pre-migration backup is required. ``always_run``
    steps alone do not trigger a backup.
    """
    pending: List[MigrationSpec] = []
    for spec in MIGRATIONS:
        if spec.always_run:
            continue
        if not _migration_applied(conn, spec.id):
            pending.append(spec)
            continue
        if spec.also_if_missing_table and not _table_exists(
            conn, spec.also_if_missing_table
        ):
            pending.append(spec)
    return pending


def apply_all_migrations(conn: sqlite3.Connection) -> None:
    """Apply every migration unconditionally (tests / offline tools)."""
    for spec in MIGRATIONS:
        spec.fn(conn)
    _stamp_user_version(conn, max_migration_order())


#: Inside the gate, wait this long for SQLite rather than the connection's full
#: busy_timeout. Long enough to ride out an ordinary concurrent commit, short
#: enough that a stuck writer does not pin the process-wide gate.
_MIGRATION_LOCK_WAIT_MS = 2_000

#: Attempts per migration. Total worst-case wait is unchanged (~30s), but it is
#: spent with the gate RELEASED between tries instead of held throughout.
_MIGRATION_LOCK_ATTEMPTS = 15
_MIGRATION_RETRY_SLEEP_S = 0.1


def _apply_one_migration(conn: sqlite3.Connection, spec) -> None:
    """Run one migration under the gate, without pinning it on a locked database.

    The gate is released between attempts. Holding it across the full SQLite
    busy_timeout made a blocked migration block every other writer in the
    process for 30s — including app startup, which then failed its own lifespan
    budget and reported itself as the problem.
    """
    # Local, matching the existing import inside ensure_migrations_applied.
    from ..write_gate import bounded_busy_timeout, is_busy_error, with_db_write

    last_busy: Optional[Exception] = None
    for attempt in range(_MIGRATION_LOCK_ATTEMPTS):
        try:
            with with_db_write():
                with bounded_busy_timeout(conn, _MIGRATION_LOCK_WAIT_MS):
                    spec.fn(conn)
            return
        except Exception as exc:  # noqa: BLE001
            if not is_busy_error(exc):
                raise
            last_busy = exc
            if attempt + 1 < _MIGRATION_LOCK_ATTEMPTS:
                logger.debug(
                    "migration %s: database locked, retrying with the gate released "
                    "(attempt %d/%d)",
                    spec.id, attempt + 1, _MIGRATION_LOCK_ATTEMPTS,
                )
                time.sleep(_MIGRATION_RETRY_SLEEP_S)
    raise last_busy if last_busy is not None else RuntimeError("migration retry loop exhausted")


def ensure_migrations_applied(
    conn: sqlite3.Connection,
    *,
    skip_backup: bool = False,
    force: bool = False,
) -> Optional[str]:
    """Apply pending schema migrations; fail loud on error.

    Returns the backup path string when a pre-migration backup was written,
    otherwise None. Raises ``DowngradeGuardError`` / ``MigrationError`` on
    failure — callers must not serve a half-migrated database.

    A connection already at the registry head whose database has not changed
    shape since returns immediately (see ``_ENSURED_CONNECTIONS``): re-running
    the ``always_run`` steps against unchanged schema bought nothing but
    write-lock contention. Any DDL re-arms them, so the late-CREATE repairs
    those steps exist for still happen. ``force=True`` runs unconditionally.
    """
    max_order = max_migration_order()
    current = read_user_version(conn)
    if current > max_order:
        raise DowngradeGuardError(
            f"database was upgraded by a newer topos-node "
            f"(PRAGMA user_version={current} > {max_order} known to this build); "
            f"upgrade the package or restore the pre-upgrade backup under "
            f"~/.topos/backups/"
        )

    # Gated on the DB's own stamp and its live schema_version, not the memo
    # alone: a recycled id naming a database this process never migrated reads
    # back unstamped, and any DDL since the last run bumps schema_version. Both
    # fall through to the run below.
    if not force and current >= max_order:
        ensured_key = _ensured_key(conn)
        schema_version = _schema_version(conn)
        if ensured_key is not None and schema_version is not None:
            with _ENSURED_LOCK:
                if _ENSURED_CONNECTIONS.get(ensured_key) == schema_version:
                    return None

    pending = pending_ledger_migrations(conn)
    backup_path: Optional[str] = None

    # Migrations write DDL/DML and commit internally (raw conn.commit()), and
    # this runner is invoked from runtime paths (AdapterFactory.create, graph
    # refresh) — not just startup. Holding the write gate across each step
    # keeps those internal commits gated (write_gate lock-order inversion);
    # the gate is reentrant, so migrations that take it themselves still work.
    from ..write_gate import with_db_write

    # Fast path: fully stamped DB with nothing pending — only always_run steps.
    if current >= max_order and not pending:
        for spec in MIGRATIONS:
            if not spec.always_run:
                continue
            try:
                _apply_one_migration(conn, spec)
            except Exception as exc:  # noqa: BLE001
                raise MigrationError(
                    f"always-run migration {spec.id!r} failed: {exc}"
                ) from exc
        _mark_ensured(conn)
        return None

    if pending and not skip_backup and connection_db_path(conn) is not None:
        try:
            path = backup_database_before_migrations(
                conn, shipped_version=_shipped_version()
            )
            if path is not None:
                backup_path = str(path)
        except InsufficientDiskForBackup as exc:
            logger.error("%s", exc)
            raise MigrationError(str(exc)) from exc

    for spec in MIGRATIONS:
        if not _needs_apply(conn, spec):
            continue
        try:
            _apply_one_migration(conn, spec)
        except Exception as exc:  # noqa: BLE001
            hint = f" Restore from {backup_path}." if backup_path else ""
            raise MigrationError(
                f"schema migration {spec.id!r} (order={spec.order}) failed: {exc}.{hint}"
            ) from exc

    with with_db_write():
        _stamp_user_version(conn, max_order)
    # Keyed after the run, not before: a database with no file yet when we
    # started has one by the time the first migration has written to it.
    _mark_ensured(conn)
    return backup_path
