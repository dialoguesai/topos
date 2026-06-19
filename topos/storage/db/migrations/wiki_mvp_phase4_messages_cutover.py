"""Wiki MVP Phase 4: legacy `messages` table deprecation catalog."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_mvp_phase4_messages_cutover"

DEPRECATED_TABLES = (
    ("messages", "conversation_messages", "Messenger UMA reads use conversation_messages (EN-P4-S1-02)."),
)


def up(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase4_messages_cutover_up(conn)


def down(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase4_messages_cutover_down(conn)


def apply_wiki_mvp_phase4_messages_cutover_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wiki_table_catalog (
            table_name TEXT PRIMARY KEY,
            authoritative_table TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            deprecation_note TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    for legacy, authoritative, note in DEPRECATED_TABLES:
        conn.execute(
            """
            INSERT INTO wiki_table_catalog (table_name, authoritative_table, status, deprecation_note)
            VALUES (?, ?, 'deprecated', ?)
            ON CONFLICT(table_name) DO UPDATE SET
                authoritative_table=excluded.authoritative_table,
                status=excluded.status,
                deprecation_note=excluded.deprecation_note,
                updated_at=datetime('now')
            """,
            (legacy, authoritative, note),
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_mvp_phase4_messages_cutover_down(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM wiki_table_catalog WHERE table_name = 'messages'")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
