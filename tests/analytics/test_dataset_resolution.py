"""One rule for "which dataset holds the messaging", shared by the Social and Luck screens.

`/v1/ingestion/datasets` returns ZERO rows on a node whose messages arrived by sync rather
than upload — measured on the live node 2026-08-27. Both screens resolve their dataset from
that list, so both rendered empty beside a database holding 7,668 messages. Two copies of
the fix would drift and the screens would then disagree about which dataset the node is.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics.dataset_resolution import (has_messaging_substrate,
                                                resolve_messaging_dataset,
                                                resolve_primary_dataset)

REAL = "9670043c-401a-4323-b092-c4724ca166eb:default:5b0940e7829907cf"
STUB = "user:default:device"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
      CREATE TABLE conversation_messages (dataset_id TEXT, message_id TEXT, is_from_self INTEGER);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER);
    """)
    for i in range(9):
        c.execute("INSERT INTO conversation_messages VALUES (?,?,1)", (REAL, f"m{i}"))
    c.execute("INSERT INTO conversation_messages VALUES (?,?,1)", (STUB, "s0"))
    return c


def test_the_busiest_dataset_wins(conn):
    assert resolve_primary_dataset(conn) == REAL


def test_a_named_dataset_with_messages_is_left_alone(conn):
    assert resolve_messaging_dataset(conn, REAL) == (REAL, False)


def test_a_named_dataset_without_messages_falls_back_and_says_so(conn):
    conn.execute("DELETE FROM conversation_messages WHERE dataset_id=?", (STUB,))
    assert resolve_messaging_dataset(conn, STUB) == (REAL, True)


def test_an_unnamed_dataset_resolves(conn):
    assert resolve_messaging_dataset(conn, "") == (REAL, True)


def test_substrate_probe_is_false_for_the_unknown(conn):
    assert has_messaging_substrate(conn, "never-seen") is False
    assert has_messaging_substrate(conn, "") is False


def test_no_messages_anywhere_resolves_to_nothing_rather_than_guessing():
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE conversation_messages (dataset_id TEXT, message_id TEXT,"
                    " is_from_self INTEGER); CREATE TABLE messenger_dyad_stats (dataset_id TEXT);")
    assert resolve_messaging_dataset(c, "whatever") == ("whatever", False)


def test_missing_tables_do_not_raise():
    c = sqlite3.connect(":memory:")
    assert resolve_primary_dataset(c) == ""
    assert has_messaging_substrate(c, "x") is False


def test_the_person_reads_resolve_their_dataset_too(conn):
    """Measured live: without this the person graph silently became mention-only and the
    naming queue reported "0 unnamed, 290 named" for a node with 121 unnamed people."""
    from topos.analytics.relationship_reads import read_naming_queue, read_person_graph

    conn.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,"
                 " canonical_name TEXT, normalized_name TEXT, aliases_json TEXT,"
                 " is_self INTEGER, contact_id TEXT)")
    conn.execute("CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,"
                 " record_id TEXT, source_id TEXT, authored_by_owner INTEGER)")
    conn.execute("CREATE TABLE entity_edges (edge_id TEXT, src_entity_id TEXT,"
                 " dst_entity_id TEXT, edge_type TEXT)")
    conn.execute("CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT)")
    conn.execute("CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT,"
                 " identifier_type TEXT)")
    conn.execute("INSERT INTO messenger_dyad_stats VALUES (?,?,?,?,?,?)",
                 (REAL, "self", "+15551230000", 1, "human", 12))
    assert read_person_graph(conn, dataset_id="")["dataset_resolved_by_engine"] is True
    assert read_naming_queue(conn, dataset_id="")["unnamed_count"] == 1
