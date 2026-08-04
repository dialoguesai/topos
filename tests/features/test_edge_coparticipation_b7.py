"""P3.2 / B7 — talked-to vs mentioned-only for communicates_with edges.

Hardens the entity spine beyond the IMB7 contacts-lane workaround:
mention-only third parties must not receive communicates_with edges;
thread co-participants must.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.maintenance import (
    fold_communicates_with_edges,
    lookup_person_entity,
    rebuild_evidence_edges,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "b7.db"))
    apply_all_migrations(c)
    c.execute(
        "CREATE TABLE IF NOT EXISTS conversation_messages ("
        "message_id TEXT PRIMARY KEY, conversation_id TEXT, sender_type TEXT, "
        "sender_id TEXT, is_from_self INTEGER, actor_role TEXT, event_at TEXT, "
        "content TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS conversation_participants ("
        "conversation_id TEXT NOT NULL, dataset_id TEXT NOT NULL, "
        "source_id TEXT NOT NULL, contact_id TEXT NOT NULL, role TEXT, "
        "PRIMARY KEY (conversation_id, dataset_id, source_id, contact_id))"
    )
    yield c
    c.close()


def _person(conn, *, entity_id, name, contact_id=None, identifiers=None, is_self=0):
    conn.execute(
        """INSERT INTO entities
           (entity_id, entity_type, canonical_name, normalized_name, mention_count,
            is_self, contact_id, identifiers_json)
           VALUES (?, 'person', ?, ?, 1, ?, ?, ?)""",
        (
            entity_id,
            name,
            name.lower(),
            is_self,
            contact_id,
            json.dumps(identifiers or []),
        ),
    )


def test_lookup_person_entity_never_mints(conn):
    _person(conn, entity_id="e-owner", name="Owner", contact_id="c-o", identifiers=["self"], is_self=1)
    _person(conn, entity_id="e-bram", name="Bram", contact_id="c-b", identifiers=["bram-7"])
    conn.commit()
    assert lookup_person_entity(conn, "self", is_from_self=True) == "e-owner"
    assert lookup_person_entity(conn, "bram-7") == "e-bram"
    assert lookup_person_entity(conn, "Odile Ferrant") is None
    assert lookup_person_entity(conn, "unknown-sender-99") is None


def test_mention_only_does_not_get_communicates_with(conn):
    _person(conn, entity_id="e-owner", name="Owner", contact_id="c-o", identifiers=["self"], is_self=1)
    _person(conn, entity_id="e-bram", name="Bram", contact_id="c-b", identifiers=["bram-7"])
    _person(conn, entity_id="e-odile", name="Odile Ferrant")  # no contact, no identifier
    conn.execute(
        "INSERT INTO entity_mentions "
        "(mention_id, entity_id, record_id, canonical_table, surface_text, event_at, source_id) "
        "VALUES ('m1', 'e-odile', 'msg-bram', 'conversation_messages', 'Odile Ferrant', "
        "'2026-01-01T00:00:00Z', 'imessage')"
    )
    conn.execute(
        "INSERT INTO conversation_messages "
        "(message_id, conversation_id, sender_id, is_from_self, actor_role, event_at, content) "
        "VALUES ('msg-owner', 'c1', 'self', 1, 'authored', '2026-01-01T00:00:00Z', 'hi')"
    )
    conn.execute(
        "INSERT INTO conversation_messages "
        "(message_id, conversation_id, sender_id, is_from_self, actor_role, event_at, content) "
        "VALUES ('msg-bram', 'c1', 'bram-7', 0, 'observed', '2026-01-01T00:01:00Z', "
        "'Odile Ferrant wrote a thread')"
    )
    conn.commit()

    stats = rebuild_evidence_edges(conn)
    assert stats["communicates_with"] >= 1
    # co_occurrence may still link Odile to whatever else is mentioned — that's fine.
    odile_comm = conn.execute(
        """SELECT COUNT(*) FROM entity_edges
           WHERE edge_type='communicates_with' AND valid_to IS NULL
             AND (src_entity_id='e-odile' OR dst_entity_id='e-odile')"""
    ).fetchone()[0]
    assert odile_comm == 0
    owner_bram = conn.execute(
        """SELECT COUNT(*) FROM entity_edges
           WHERE edge_type='communicates_with' AND valid_to IS NULL
             AND ((src_entity_id='e-owner' AND dst_entity_id='e-bram')
               OR (src_entity_id='e-bram' AND dst_entity_id='e-owner'))"""
    ).fetchone()[0]
    assert owner_bram >= 1


def test_fold_respects_conversation_filter(conn):
    _person(conn, entity_id="e-owner", name="Owner", contact_id="c-o", identifiers=["self"], is_self=1)
    _person(conn, entity_id="e-a", name="Ada", contact_id="c-a", identifiers=["ada-1"])
    _person(conn, entity_id="e-b", name="Bea", contact_id="c-b", identifiers=["bea-1"])
    for mid, conv, sender, self_flag in (
        ("m1", "c-keep", "self", 1),
        ("m2", "c-keep", "ada-1", 0),
        ("m3", "c-skip", "self", 1),
        ("m4", "c-skip", "bea-1", 0),
    ):
        conn.execute(
            "INSERT INTO conversation_messages "
            "(message_id, conversation_id, sender_id, is_from_self, actor_role, event_at) "
            "VALUES (?, ?, ?, ?, 'participated', '2026-01-01T00:00:00Z')",
            (mid, conv, sender, self_flag),
        )
    conn.commit()
    n, _ = fold_communicates_with_edges(conn, conversation_ids=["c-keep"])
    assert n >= 1
    assert (
        conn.execute(
            """SELECT COUNT(*) FROM entity_edges
               WHERE edge_type='communicates_with' AND valid_to IS NULL
                 AND (src_entity_id='e-b' OR dst_entity_id='e-b')"""
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            """SELECT COUNT(*) FROM entity_edges
               WHERE edge_type='communicates_with' AND valid_to IS NULL
                 AND ((src_entity_id='e-owner' AND dst_entity_id='e-a')
                   OR (src_entity_id='e-a' AND dst_entity_id='e-owner'))"""
        ).fetchone()[0]
        >= 1
    )
