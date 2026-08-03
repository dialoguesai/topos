"""Wave B5: backfill role on legacy message_emotions NULL rows."""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.derived_tables import (
    DerivedTablesManager,
    reset_ensured_tables_cache,
)
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.message_emotions_role_backfill_v1 import (
    MIGRATION_ID,
    apply_message_emotions_role_backfill_v1_up,
    backfill_message_emotions_role,
)
from topos.storage.db.migrations.message_emotions_role_v1 import (
    apply_message_emotions_role_v1_up,
)

pytestmark = [pytest.mark.check("C-quality-prov-followups")]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "emo_backfill.db"))
    apply_all_migrations(c)
    c.execute("DROP TABLE IF EXISTS message_emotions")
    c.commit()
    reset_ensured_tables_cache()
    DerivedTablesManager(conn=c)._ensure_tables()
    apply_message_emotions_role_v1_up(c)
    # Clear the one-shot ledger so we can exercise the backfill function
    # explicitly (full migrate may have already stamped the migration id
    # against an empty/wiki-shaped table).
    c.execute(
        "DELETE FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    )
    c.commit()
    yield c
    c.close()


def _ensure_message_tables(conn) -> None:
    # Canonical message tables come from ingestion, not wiki migrations.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            source_id TEXT,
            sender_type TEXT,
            sender_id TEXT,
            is_from_self INTEGER,
            actor_role TEXT
        );
        CREATE TABLE IF NOT EXISTS ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            source_id TEXT,
            sender_type TEXT,
            sender_id TEXT,
            actor_role TEXT
        );
        """
    )
    conn.commit()


def _seed_conversation_message(conn, message_id: str, actor_role: str) -> None:
    _ensure_message_tables(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO conversation_messages
            (message_id, conversation_id, source_id, sender_type, actor_role)
        VALUES (?, 'c1', 'imessage', ?, ?)
        """,
        (
            message_id,
            "self" if actor_role == "authored" else "contact",
            actor_role,
        ),
    )
    conn.commit()


def _seed_emotion(conn, message_id: str, *, role=None) -> None:
    writer = DerivedTablesManager(conn=conn)
    row = {
        "message_id": message_id,
        "source_id": "imessage",
        "emotion_label": "joy",
        "confidence": 0.9,
        "model_name": "fake-emo",
        "all_emotions": [],
    }
    if role is not None:
        row["role"] = role
    writer.write_enrichment_batch([row], "message_emotions")
    if role is None:
        conn.execute(
            "UPDATE message_emotions SET role = NULL WHERE message_id=?",
            (message_id,),
        )
        conn.commit()


def test_backfill_stamps_null_from_actor_role(conn) -> None:
    _seed_conversation_message(conn, "m_auth", "authored")
    _seed_conversation_message(conn, "m_obs", "observed")
    _seed_emotion(conn, "m_auth", role=None)
    _seed_emotion(conn, "m_obs", role=None)

    report = backfill_message_emotions_role(conn)
    conn.commit()
    assert report["updated"] == 2

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT message_id, role FROM message_emotions ORDER BY message_id"
        ).fetchall()
    }
    assert rows == {"m_auth": "authored", "m_obs": "observed"}

    kept = conn.execute(
        """
        SELECT message_id FROM message_emotions
        WHERE role IS NULL OR role IN ('authored', 'addressed')
        ORDER BY message_id
        """
    ).fetchall()
    assert kept == [("m_auth",)]


def test_backfill_idempotent_and_preserves_existing(conn) -> None:
    _seed_conversation_message(conn, "m1", "authored")
    _seed_emotion(conn, "m1", role="authored")
    first = backfill_message_emotions_role(conn)
    second = backfill_message_emotions_role(conn)
    assert first["updated"] == 0
    assert second["updated"] == 0
    role = conn.execute(
        "SELECT role FROM message_emotions WHERE message_id='m1'"
    ).fetchone()[0]
    assert role == "authored"


def test_orphan_emotion_left_null(conn) -> None:
    _seed_emotion(conn, "m_orphan", role=None)
    report = backfill_message_emotions_role(conn)
    assert report["updated"] == 0
    assert report["still_null"] >= 1
    role = conn.execute(
        "SELECT role FROM message_emotions WHERE message_id='m_orphan'"
    ).fetchone()[0]
    assert role is None


def test_migration_ledger_guards_one_shot(conn) -> None:
    _seed_conversation_message(conn, "m2", "observed")
    _seed_emotion(conn, "m2", role=None)
    apply_message_emotions_role_backfill_v1_up(conn)
    # Flip back to NULL and re-run migration — ledger should skip backfill.
    conn.execute("UPDATE message_emotions SET role = NULL WHERE message_id='m2'")
    conn.commit()
    apply_message_emotions_role_backfill_v1_up(conn)
    role = conn.execute(
        "SELECT role FROM message_emotions WHERE message_id='m2'"
    ).fetchone()[0]
    assert role is None  # second apply skipped the scan
