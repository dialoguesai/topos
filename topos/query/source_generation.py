"""Scope/source generation watermark for retrieval fingerprint invalidation."""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

logger = logging.getLogger(__name__)

GENERATION_TABLE = "scope_source_generation"


def ensure_source_generation_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {GENERATION_TABLE} (
            scope_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (scope_id, source_id)
        )
        """
    )


def bump_source_generation(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    scope_ids: Optional[List[str]] = None,
) -> None:
    """Increment generation for source_id across relevant scopes after ingest/sync."""
    sid = (source_id or "").strip()
    if not conn or not sid:
        return
    ensure_source_generation_schema(conn)
    targets = list(scope_ids or [])
    if not targets:
        try:
            from ..sources.registry import REGISTRY

            defn = REGISTRY.get(sid)
            if defn:
                default_scope = (defn.default_scope_id or "").strip()
                if default_scope:
                    targets.append(default_scope if ":" in default_scope else f"{default_scope}:read")
        except Exception:
            pass
    if not targets:
        targets = ["*"]
    for scope_id in targets:
        conn.execute(
            f"""
            INSERT INTO {GENERATION_TABLE} (scope_id, source_id, generation, updated_at)
            VALUES (?, ?, 1, datetime('now'))
            ON CONFLICT(scope_id, source_id) DO UPDATE SET
                generation = {GENERATION_TABLE}.generation + 1,
                updated_at = datetime('now')
            """,
            (scope_id, sid),
        )
    conn.commit()


def get_data_health_version(
    scope_id: str,
    source_ids: List[str],
    conn: Optional[sqlite3.Connection],
) -> str:
    """Compact version string for fingerprint; changes when ingest bumps generation."""
    if not conn or not source_ids:
        return "mvp"
    ensure_source_generation_schema(conn)
    ids = sorted({str(s).strip() for s in source_ids if str(s).strip()})
    if not ids:
        return "mvp"
    placeholders = ",".join("?" for _ in ids)
    params: tuple = (scope_id, *ids)
    rows = conn.execute(
        f"""
        SELECT source_id, generation FROM {GENERATION_TABLE}
        WHERE scope_id=? AND source_id IN ({placeholders})
        """,
        params,
    ).fetchall()
    gen_by_source = {str(r[0]): int(r[1]) for r in rows}
    wildcard = conn.execute(
        f"SELECT source_id, generation FROM {GENERATION_TABLE} WHERE scope_id='*'"
    ).fetchall()
    for source_id, generation in wildcard:
        gen_by_source.setdefault(str(source_id), int(generation))
    parts = [f"{sid}:{gen_by_source.get(sid, 0)}" for sid in ids]
    return "|".join(parts)


def list_installed_source_ids(conn: Optional[sqlite3.Connection]) -> List[str]:
    if conn is None:
        return []
    try:
        from ..sources.install_service import INSTALL_TABLE, ensure_install_schema

        ensure_install_schema()
        rows = conn.execute(
            f"""
            SELECT DISTINCT source_id FROM {INSTALL_TABLE}
            WHERE is_active=1 AND status IN ('installed', 'active', 'ready')
            """
        ).fetchall()
        return [str(row[0]).strip() for row in rows if row and row[0]]
    except Exception as exc:
        logger.debug("list_installed_source_ids skipped: %s", exc)
        return []
