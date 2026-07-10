"""Minimal query-eval seed rows for Q5 (illustration) and Q6 (git raw)."""

from __future__ import annotations

import sqlite3

from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations


def apply_query_eval_seed(conn: sqlite3.Connection) -> None:
    """Insert illustration + git ai_chat rows when absent (idempotent)."""
    apply_all_migrations(conn)
    CanonicalTablesManager(conn)

    # No presence short-circuit: INSERT OR REPLACE below is idempotent AND
    # self-healing — stale seed rows from the pre-install-gate era carried an
    # uninstalled source_id (chatgpt_ingestion) that retrieval rightly filters.
    conn.execute(
        """
        INSERT OR REPLACE INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES (
            'eval-q5-ill', 'eval-conv', 'user', 'chatgpt_ui_conversation',
            'pencil sketch illustration of a mountain landscape', '2026-01-02'
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES (
            'eval-q6-git', 'eval-conv', 'user', 'chatgpt_ui_conversation',
            'git push to GitHub repository main branch', '2026-01-03'
        )
        """
    )
    conn.commit()
