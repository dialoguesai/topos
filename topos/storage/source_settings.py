"""Per-source settings: enabled, last_sync_at, last_error, posture (for local_sync sources)."""

from __future__ import annotations

import logging
from typing import Optional

from .db.schema_probe import UnusableConnection, describe_unusable, probe_bool
from .db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.storage.source_settings")

TABLE = "user_ingestion_sources"

# PLAN_PROVENANCE_SPLIT P1.4: the per-connector posture OVERRIDE. NULL means
# "inherit the registry DataSourceDefinition.posture default"; a non-NULL value
# is the owner's explicit choice. Kept in lock-step with
# sources/definitions.POSTURE_VALUES (personal|mixed|ambient) — imported lazily
# to keep this storage module import-light.
_VALID_POSTURES = frozenset({"personal", "mixed", "ambient"})
_REQUIRED_COLUMNS = frozenset({"posture", "exclude_spam"})
_DEFAULT_SETTINGS = {
    "enabled": True,
    "last_sync_at": None,
    "last_error": None,
    "posture": None,
    "exclude_spam": True,
}


def _normalize_posture(posture) -> Optional[str]:
    """Return a validated posture string, or None to inherit the registry
    default. Raises ValueError on an unrecognized non-empty value so bad input
    fails loudly at the write boundary instead of silently persisting."""
    if posture is None:
        return None
    value = str(posture).strip().lower()
    if not value:
        return None
    if value not in _VALID_POSTURES:
        raise ValueError(
            f"posture must be one of {sorted(_VALID_POSTURES)} or null, got {posture!r}"
        )
    return value


def _has_posture_column(conn) -> bool:
    """True once the table exists AND carries the posture column.

    Raises :class:`UnusableConnection` when the PRAGMA itself cannot run — a
    missing table returns zero rows rather than raising, so a raise here means
    the connection is broken and the DDL below would only take the write gate to
    discover that.
    """
    return _schema_ready(conn)


def _schema_ready(conn) -> bool:
    """True once the table exists and carries every required settings column."""

    def _read() -> bool:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall() if r[1]}
        return _REQUIRED_COLUMNS <= cols

    return probe_bool(_read, what=f"{TABLE}.schema")


def _column_names(conn) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall() if r[1]}


def ensure_table(conn) -> None:
    """Create the settings table, skipping the gate once it is there.

    Every read and write here calls this first, so re-running idempotent DDL put
    a plain settings lookup behind the write gate — a blocking OS lock — on
    whatever thread asked, the event loop included. The probe is a PRAGMA read,
    and it doubles as the required-column check, so a fully-migrated table costs
    no gate at all.

    A probe that cannot RUN is not a missing table. Falling through to the DDL
    on a broken connection takes the blocking gate — on the event loop, if that
    is who asked — to reach a failure that was already certain.
    """
    try:
        if _schema_ready(conn):
            return
    except UnusableConnection as exc:
        logger.warning("source settings DDL skipped: %s", describe_unusable(exc))
        raise
    # DDL takes SQLite's write lock at execute time — gate it with the commit.
    with with_db_write():
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                dataset_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sync_at TEXT,
                last_error TEXT,
                posture TEXT,
                exclude_spam INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (dataset_id, source_id)
            )
        """)
        cols = _column_names(conn)
        if "posture" not in cols:
            try:
                conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN posture TEXT")
            except Exception as e:  # noqa: BLE001
                logger.warning("posture column add skipped: %s", e)
        if "exclude_spam" not in cols:
            try:
                conn.execute(
                    f"ALTER TABLE {TABLE} ADD COLUMN exclude_spam INTEGER NOT NULL DEFAULT 1"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("exclude_spam column add skipped: %s", e)
        commit_connection(conn)


def get_source_settings(conn, dataset_id: str, source_id: str) -> Optional[dict]:
    """Return { enabled, last_sync_at, last_error, posture, exclude_spam } or None.

    Defaults when no row exists: enabled true, exclude_spam true, no last_*,
    posture None (posture None => inherit the registry DataSourceDefinition
    default; see sources.registry.effective_posture)."""
    if not conn or not dataset_id or not source_id:
        return None
    try:
        ensure_table(conn)
        row = conn.execute(
            f"SELECT enabled, last_sync_at, last_error, posture, exclude_spam "
            f"FROM {TABLE} WHERE dataset_id = ? AND source_id = ?",
            (dataset_id, source_id),
        ).fetchone()
        if not row:
            return dict(_DEFAULT_SETTINGS)
        return {
            "enabled": bool(row[0]),
            "last_sync_at": row[1],
            "last_error": row[2],
            "posture": row[3],
            "exclude_spam": bool(row[4]) if row[4] is not None else True,
        }
    except Exception as e:
        logger.warning("get_source_settings failed: %s", e)
        return dict(_DEFAULT_SETTINGS)


# Sentinel so callers can distinguish "leave posture unchanged" (default) from
# "clear the override back to inherit" (posture=None passed explicitly).
_UNSET = object()


def put_source_settings(
    conn,
    dataset_id: str,
    source_id: str,
    *,
    enabled: Optional[bool] = None,
    posture=_UNSET,
    exclude_spam=_UNSET,
) -> None:
    """Update enabled, posture, and/or exclude_spam; leave last_sync_at/last_error unchanged.

    ``posture`` is validated to {personal,mixed,ambient} or None (None clears
    the override so the row inherits the registry default). Passing none of
    ``enabled``, ``posture``, or ``exclude_spam`` is a no-op. Raises ValueError
    on an invalid posture value."""
    if not conn or not dataset_id or not source_id:
        return
    posture_provided = posture is not _UNSET
    exclude_spam_provided = exclude_spam is not _UNSET
    normalized_posture = _normalize_posture(posture) if posture_provided else None
    if enabled is None and not posture_provided and not exclude_spam_provided:
        return
    try:
        ensure_table(conn)
        cur = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE dataset_id = ? AND source_id = ?",
            (dataset_id, source_id),
        ).fetchone()
        set_clauses = []
        params: list = []
        if enabled is not None:
            set_clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        if posture_provided:
            set_clauses.append("posture = ?")
            params.append(normalized_posture)
        if exclude_spam_provided:
            set_clauses.append("exclude_spam = ?")
            params.append(1 if bool(exclude_spam) else 0)
        with with_db_write():
            if cur:
                set_clauses.append("updated_at = datetime('now')")
                params.extend([dataset_id, source_id])
                conn.execute(
                    f"UPDATE {TABLE} SET {', '.join(set_clauses)} WHERE dataset_id = ? AND source_id = ?",
                    params,
                )
            else:
                # New row: default enabled=1 and exclude_spam=1 when only
                # posture/exclude_spam was set.
                conn.execute(
                    f"INSERT INTO {TABLE} (dataset_id, source_id, enabled, posture, exclude_spam, updated_at) "
                    f"VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (
                        dataset_id,
                        source_id,
                        1 if (enabled is None or enabled) else 0,
                        normalized_posture if posture_provided else None,
                        1 if (not exclude_spam_provided or bool(exclude_spam)) else 0,
                    ),
                )
            commit_connection(conn)
    except ValueError:
        raise
    except Exception as e:
        logger.warning("put_source_settings failed: %s", e)


def update_sync_result(
    conn,
    dataset_id: str,
    source_id: str,
    *,
    success: bool,
    last_sync_at: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    """After sync: set last_sync_at (and clear last_error) on success, or set last_error on failure."""
    if not conn or not dataset_id or not source_id:
        return
    try:
        ensure_table(conn)
        with with_db_write():
            if success:
                conn.execute(
                    f"""
                    INSERT INTO {TABLE} (dataset_id, source_id, enabled, last_sync_at, last_error, updated_at)
                    VALUES (?, ?, 1, ?, NULL, datetime('now'))
                    ON CONFLICT(dataset_id, source_id) DO UPDATE SET
                        last_sync_at = ?,
                        last_error = NULL,
                        updated_at = datetime('now')
                    """,
                    (dataset_id, source_id, last_sync_at or "", last_sync_at or ""),
                )
            else:
                conn.execute(
                    f"""
                    INSERT INTO {TABLE} (dataset_id, source_id, enabled, last_sync_at, last_error, updated_at)
                    VALUES (?, ?, 1, NULL, ?, datetime('now'))
                    ON CONFLICT(dataset_id, source_id) DO UPDATE SET
                        last_error = ?,
                        updated_at = datetime('now')
                    """,
                    (dataset_id, source_id, last_error or "", last_error or ""),
                )
            commit_connection(conn)
    except Exception as e:
        logger.warning("update_sync_result failed: %s", e)
