"""Rename the legacy global contact sentinel to the canonical address book."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

from ....sources.definitions import CANONICAL_ADDRESS_BOOK_SOURCE_ID

MIGRATION_ID = "canonical_address_book_v1"
LEGACY_SOURCE_ID = "global"
LEGACY_DISPLAY_NAME = "Address Book (merged)"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _is_legacy_scope_manifest(raw_definition: Any) -> bool:
    try:
        definition = (
            json.loads(raw_definition)
            if isinstance(raw_definition, str)
            else raw_definition
        )
    except (TypeError, ValueError):
        return False
    if not isinstance(definition, dict):
        return False
    return (
        str(definition.get("source_id") or "").strip() == LEGACY_SOURCE_ID
        and str(definition.get("display_name") or "").strip() == LEGACY_DISPLAY_NAME
        and str(definition.get("default_scope_id") or "").strip() == "contacts"
        and not any(
            str(definition.get(field) or "").strip()
            for field in ("source_type", "schema_id", "parser_id")
        )
    )


def apply_canonical_address_book_v1_up(conn: sqlite3.Connection) -> Dict[str, int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone():
        return {"contacts_migrated": 0, "legacy_installs_removed": 0}

    contacts_migrated = 0
    legacy_installs_removed = 0
    with conn:
        if _table_exists(conn, "contacts"):
            cursor = conn.execute(
                "UPDATE contacts SET source_id=? WHERE source_id=?",
                (CANONICAL_ADDRESS_BOOK_SOURCE_ID, LEGACY_SOURCE_ID),
            )
            contacts_migrated = max(0, int(cursor.rowcount or 0))

        if _table_exists(conn, "source_runtime_installs"):
            rows = conn.execute(
                """
                SELECT install_id, source_definition_json
                FROM source_runtime_installs
                WHERE source_id=?
                """,
                (LEGACY_SOURCE_ID,),
            ).fetchall()
            install_ids = [
                str(row[0])
                for row in rows
                if row and _is_legacy_scope_manifest(row[1])
            ]
            for install_id in install_ids:
                cursor = conn.execute(
                    "DELETE FROM source_runtime_installs WHERE install_id=?",
                    (install_id,),
                )
                legacy_installs_removed += max(0, int(cursor.rowcount or 0))

        conn.execute(
            "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )

    return {
        "contacts_migrated": contacts_migrated,
        "legacy_installs_removed": legacy_installs_removed,
    }

