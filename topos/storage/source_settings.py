"""Per-source settings: enabled, last_sync_at, last_error, posture (for local_sync sources)."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("topos.storage.source_settings")

TABLE = "user_ingestion_sources"

# PLAN_PROVENANCE_SPLIT P1.4: the per-connector posture OVERRIDE. NULL means
# "inherit the registry DataSourceDefinition.posture default"; a non-NULL value
# is the owner's explicit choice. Kept in lock-step with
# sources/definitions.POSTURE_VALUES (personal|mixed|ambient) — imported lazily
# to keep this storage module import-light.
_VALID_POSTURES = frozenset({"personal", "mixed", "ambient"})


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
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}
        return "posture" in cols
    except Exception:  # noqa: BLE001
        return False


def ensure_table(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_sync_at TEXT,
            last_error TEXT,
            posture TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (dataset_id, source_id)
        )
    """)
    # Idempotent, PRAGMA-guarded add for tables created before P1.4.
    if not _has_posture_column(conn):
        try:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN posture TEXT")
        except Exception as e:  # noqa: BLE001
            logger.warning("posture column add skipped: %s", e)
    conn.commit()


def get_source_settings(conn, dataset_id: str, source_id: str) -> Optional[dict]:
    """Return { enabled, last_sync_at, last_error, posture } or None.

    Defaults when no row exists: enabled true, no last_*, posture None
    (posture None => inherit the registry DataSourceDefinition default; see
    sources.registry.effective_posture)."""
    if not conn or not dataset_id or not source_id:
        return None
    try:
        ensure_table(conn)
        row = conn.execute(
            f"SELECT enabled, last_sync_at, last_error, posture FROM {TABLE} WHERE dataset_id = ? AND source_id = ?",
            (dataset_id, source_id),
        ).fetchone()
        if not row:
            return {"enabled": True, "last_sync_at": None, "last_error": None, "posture": None}
        return {
            "enabled": bool(row[0]),
            "last_sync_at": row[1],
            "last_error": row[2],
            "posture": row[3],
        }
    except Exception as e:
        logger.warning("get_source_settings failed: %s", e)
        return {"enabled": True, "last_sync_at": None, "last_error": None, "posture": None}


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
) -> None:
    """Update enabled and/or posture; leave last_sync_at/last_error unchanged.

    ``posture`` is validated to {personal,mixed,ambient} or None (None clears
    the override so the row inherits the registry default). Passing neither
    ``enabled`` nor ``posture`` is a no-op. Raises ValueError on an invalid
    posture value."""
    if not conn or not dataset_id or not source_id:
        return
    posture_provided = posture is not _UNSET
    normalized_posture = _normalize_posture(posture) if posture_provided else None
    if enabled is None and not posture_provided:
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
        if cur:
            set_clauses.append("updated_at = datetime('now')")
            params.extend([dataset_id, source_id])
            conn.execute(
                f"UPDATE {TABLE} SET {', '.join(set_clauses)} WHERE dataset_id = ? AND source_id = ?",
                params,
            )
        else:
            # New row: default enabled=1 when only posture was set.
            conn.execute(
                f"INSERT INTO {TABLE} (dataset_id, source_id, enabled, posture, updated_at) "
                f"VALUES (?, ?, ?, ?, datetime('now'))",
                (
                    dataset_id,
                    source_id,
                    1 if (enabled is None or enabled) else 0,
                    normalized_posture if posture_provided else None,
                ),
            )
        conn.commit()
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
        conn.commit()
    except Exception as e:
        logger.warning("update_sync_result failed: %s", e)
