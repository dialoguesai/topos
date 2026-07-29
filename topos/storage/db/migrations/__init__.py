"""Wiki MVP storage migration runner — single registry (PLAN §4a)."""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

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
    "max_migration_order",
]


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


def ensure_migrations_applied(
    conn: sqlite3.Connection,
    *,
    skip_backup: bool = False,
) -> Optional[str]:
    """Apply pending schema migrations; fail loud on error.

    Returns the backup path string when a pre-migration backup was written,
    otherwise None. Raises ``DowngradeGuardError`` / ``MigrationError`` on
    failure — callers must not serve a half-migrated database.
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

    pending = pending_ledger_migrations(conn)
    backup_path: Optional[str] = None

    # Fast path: fully stamped DB with nothing pending — only always_run steps.
    if current >= max_order and not pending:
        for spec in MIGRATIONS:
            if not spec.always_run:
                continue
            try:
                spec.fn(conn)
            except Exception as exc:  # noqa: BLE001
                raise MigrationError(
                    f"always-run migration {spec.id!r} failed: {exc}"
                ) from exc
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
            spec.fn(conn)
        except Exception as exc:  # noqa: BLE001
            hint = f" Restore from {backup_path}." if backup_path else ""
            raise MigrationError(
                f"schema migration {spec.id!r} (order={spec.order}) failed: {exc}.{hint}"
            ) from exc

    _stamp_user_version(conn, max_order)
    return backup_path
