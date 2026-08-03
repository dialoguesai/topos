"""Wave B6 / P3.1: entity_mentions.authored_by_owner column + stamp + backfill."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.entity_mentions_authored_v1 import (
    MIGRATION_ID,
    apply_entity_mentions_authored_v1_up,
    backfill_entity_mentions_authored,
    lookup_authored_by_owner,
)

pytestmark = [pytest.mark.check("C-quality-prov-followups")]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "mentions_authored.db"))
    apply_all_migrations(c)
    c.executescript(
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
        CREATE TABLE IF NOT EXISTS journal_entries (
            entry_id TEXT PRIMARY KEY,
            source_id TEXT
        );
        CREATE TABLE IF NOT EXISTS activity_events (
            event_id TEXT PRIMARY KEY,
            activity_type TEXT,
            source_id TEXT
        );
        """
    )
    # Clear ledger so unit tests can re-exercise the one-shot backfill.
    c.execute(
        "DELETE FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    )
    c.commit()
    yield c
    c.close()


def _seed_conversation(conn, message_id: str, *, is_from_self: bool) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO conversation_messages
            (message_id, conversation_id, source_id, sender_type, sender_id, is_from_self)
        VALUES (?, 'c1', 'imessage', ?, ?, ?)
        """,
        (
            message_id,
            "human",
            "self" if is_from_self else "contact-1",
            1 if is_from_self else 0,
        ),
    )
    conn.commit()


def test_migration_adds_column(conn) -> None:
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(entity_mentions)").fetchall()
    }
    assert "authored_by_owner" in cols


def test_write_path_stamps_from_parent(conn) -> None:
    _seed_conversation(conn, "m_auth", is_from_self=True)
    _seed_conversation(conn, "m_obs", is_from_self=False)
    resolver = EntityResolver(conn)
    ent = resolver._create_entity("Ada Lovelace", "person")
    conn.commit()
    resolver.record_mention(
        ent,
        record_id="m_auth",
        surface_text="Ada",
        canonical_table="conversation_messages",
    )
    resolver.record_mention(
        ent,
        record_id="m_obs",
        surface_text="Ada",
        canonical_table="conversation_messages",
    )
    conn.commit()
    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT record_id, authored_by_owner FROM entity_mentions ORDER BY record_id"
        ).fetchall()
    }
    assert rows == {"m_auth": 1, "m_obs": 0}


def test_explicit_authored_flag_wins(conn) -> None:
    _seed_conversation(conn, "m1", is_from_self=False)
    resolver = EntityResolver(conn)
    ent = resolver._create_entity("Grace Hopper", "person")
    conn.commit()
    resolver.record_mention(
        ent,
        record_id="m1",
        surface_text="Grace",
        canonical_table="conversation_messages",
        authored_by_owner=1,  # explicit override
    )
    conn.commit()
    flag = conn.execute(
        "SELECT authored_by_owner FROM entity_mentions WHERE record_id='m1'"
    ).fetchone()[0]
    assert flag == 1


def test_backfill_stamps_null_from_parents(conn) -> None:
    _seed_conversation(conn, "m_auth", is_from_self=True)
    _seed_conversation(conn, "m_obs", is_from_self=False)
    conn.execute(
        "INSERT INTO journal_entries (entry_id, source_id) VALUES ('j1', 'dayone')"
    )
    conn.execute(
        "INSERT INTO activity_events (event_id, activity_type, source_id) "
        "VALUES ('a1', 'visit', 'demo_browser_file')"
    )
    # Insert legacy-shaped mentions with NULL authored_by_owner.
    for mid, rid, table in (
        ("men_auth", "m_auth", "conversation_messages"),
        ("men_obs", "m_obs", "conversation_messages"),
        ("men_j", "j1", "journal_entries"),
        ("men_a", "a1", "activity_events"),
        ("men_orphan", "missing", "conversation_messages"),
    ):
        conn.execute(
            """
            INSERT INTO entity_mentions
                (mention_id, entity_id, record_id, canonical_table, surface_text,
                 authored_by_owner)
            VALUES (?, 'e1', ?, ?, 'x', NULL)
            """,
            (mid, rid, table),
        )
    conn.commit()

    report = backfill_entity_mentions_authored(conn)
    conn.commit()
    assert report["updated"] == 4
    assert report["authored"] == 2  # m_auth + journal
    assert report["not_authored"] == 2  # m_obs + activity
    assert report["still_null"] == 1  # orphan

    flags = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT mention_id, authored_by_owner FROM entity_mentions ORDER BY mention_id"
        ).fetchall()
    }
    assert flags == {
        "men_a": 0,
        "men_auth": 1,
        "men_j": 1,
        "men_obs": 0,
        "men_orphan": None,
    }


def test_backfill_idempotent_and_preserves_existing(conn) -> None:
    _seed_conversation(conn, "m1", is_from_self=True)
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, canonical_table, surface_text,
             authored_by_owner)
        VALUES ('men1', 'e1', 'm1', 'conversation_messages', 'x', 0)
        """
    )
    conn.commit()
    first = backfill_entity_mentions_authored(conn)
    second = backfill_entity_mentions_authored(conn)
    assert first["updated"] == 0
    assert second["updated"] == 0
    flag = conn.execute(
        "SELECT authored_by_owner FROM entity_mentions WHERE mention_id='men1'"
    ).fetchone()[0]
    assert flag == 0  # preserved explicit/legacy stamp


def test_migration_ledger_guards_one_shot(conn) -> None:
    _seed_conversation(conn, "m2", is_from_self=True)
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, canonical_table, surface_text,
             authored_by_owner)
        VALUES ('men2', 'e1', 'm2', 'conversation_messages', 'x', NULL)
        """
    )
    conn.commit()
    apply_entity_mentions_authored_v1_up(conn)
    flag = conn.execute(
        "SELECT authored_by_owner FROM entity_mentions WHERE mention_id='men2'"
    ).fetchone()[0]
    assert flag == 1
    # Flip back to NULL and re-run — ledger should skip backfill.
    conn.execute(
        "UPDATE entity_mentions SET authored_by_owner = NULL WHERE mention_id='men2'"
    )
    conn.commit()
    apply_entity_mentions_authored_v1_up(conn)
    flag = conn.execute(
        "SELECT authored_by_owner FROM entity_mentions WHERE mention_id='men2'"
    ).fetchone()[0]
    assert flag is None


def test_lookup_ai_chat_user_is_authored(conn) -> None:
    conn.execute(
        """
        INSERT INTO ai_chat_messages
            (message_id, conversation_id, source_id, sender_type, sender_id)
        VALUES ('ai1', 'chat1', 'chatgpt_ingestion', 'user', 'user')
        """
    )
    conn.commit()
    assert lookup_authored_by_owner(conn, "ai1", canonical_table="ai_chat_messages") == 1
    conn.execute(
        """
        INSERT INTO ai_chat_messages
            (message_id, conversation_id, source_id, sender_type, sender_id)
        VALUES ('ai2', 'chat1', 'chatgpt_ingestion', 'assistant', 'assistant')
        """
    )
    conn.commit()
    assert lookup_authored_by_owner(conn, "ai2", canonical_table="ai_chat_messages") == 0
