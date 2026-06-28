"""Minimal query-eval seed rows for Q5 (illustration) and Q6 (git raw)."""

from __future__ import annotations

import sqlite3

from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations


def apply_query_eval_seed(conn: sqlite3.Connection) -> None:
    """Insert illustration + git ai_chat rows when absent (idempotent)."""
    apply_all_migrations(conn)
    CanonicalTablesManager(conn)

    existing = conn.execute(
        "SELECT COUNT(*) FROM ai_chat_messages WHERE message_id IN ('eval-q5-ill', 'eval-q6-git')"
    ).fetchone()[0]
    if existing >= 2:
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES (
            'eval-q5-ill', 'eval-conv', 'user', 'chatgpt_ingestion',
            'pencil sketch illustration of a mountain landscape', '2026-01-02'
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES (
            'eval-q6-git', 'eval-conv', 'user', 'chatgpt_ingestion',
            'git push to GitHub repository main branch', '2026-01-03'
        )
        """
    )
    conn.commit()
