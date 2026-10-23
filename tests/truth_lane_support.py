"""A synthetic node for the inbound truth lane's tests: a migrated temp database with invented rows.

Every name and line here is made up. ``build_node`` returns the database path; ``connect`` gives a connection the
test can share across threads. ``bind`` points both connection accessors (``core.state`` and the handlers' hub) at
the temp database, one connection per thread, the way the embeddings catch-up lane test does.
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_TEMPLATE: dict = {}


def _template(directory: Path) -> Path:
    """Migrations run once per session; each test copies the result."""
    if "path" not in _TEMPLATE or not Path(_TEMPLATE["path"]).exists():
        from topos.storage.canonical.conversations_tables import ConversationsTablesManager
        from topos.storage.db.migrations import apply_all_migrations

        path = directory / "template.db"
        conn = sqlite3.connect(path)
        apply_all_migrations(conn)
        ConversationsTablesManager(conn).ensure_tables()
        conn.commit()
        conn.close()
        _TEMPLATE["path"] = str(path)
    return Path(_TEMPLATE["path"])


def build_node(tmp_path: Path, template_dir: Path) -> Path:
    path = tmp_path / "node.db"
    shutil.copy(_template(template_dir), path)
    return path


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), check_same_thread=False, timeout=30)


def bind(monkeypatch, path: Path):
    local = threading.local()

    def connection() -> sqlite3.Connection:
        conn = getattr(local, "conn", None)
        if conn is None:
            conn = local.conn = connect(path)
            conn.row_factory = sqlite3.Row  # as the node's own accessor hands it out
        return conn

    monkeypatch.setattr("topos.core.state.get_db_connection", connection)
    monkeypatch.setattr("topos.core.handlers.get_db_connection", connection)
    return connection


def ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_message(conn, message_id: str, content: str, *, at: Optional[str] = None, source_id: str = "imessage",
                conversation_id: str = "thread-1", from_self: bool = False, sender_id: str = "+15550000001") -> None:
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_type, sender_id, content, "
        "event_at, source_id, is_from_self) VALUES (?, ?, 'owner:default', ?, ?, ?, ?, ?, ?)",
        (message_id, conversation_id, "self" if from_self else "contact", "self" if from_self else sender_id,
         content, at or ago(hours=1), source_id, 1 if from_self else 0),
    )


def add_contact(conn, identifier: str, name: str, contact_id: str = "c1") -> None:
    conn.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name) VALUES (?, 'owner:default', "
                 "'imessage', ?)", (contact_id, name))
    conn.execute("INSERT INTO contact_identifiers (dataset_id, source_id, identifier, contact_id) VALUES "
                 "('owner:default', 'imessage', ?, ?)", (identifier, contact_id))


def add_journal(conn, entry_id: str, content: str, *, at: Optional[str] = None) -> None:
    conn.execute("INSERT INTO journal_entries (entry_id, entry_at, content, source_id) VALUES (?, ?, ?, 'demo_journal_file')",
                 (entry_id, at or ago(hours=2), content))


def add_segment(conn, segment_id: str, content: str, *, actor_role: str = "authored", at: Optional[str] = None,
                from_self: bool = True, transcript_id: str = "talk-1", speaker_label: str = "") -> None:
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, content, event_at, actor_role, is_from_self, "
        "source_id, speaker_label) VALUES (?, ?, ?, ?, ?, ?, 'youtube_transcripts', ?)",
        (segment_id, transcript_id, content, at or ago(hours=3), actor_role, 1 if from_self else 0, speaker_label),
    )
