"""L1-1 — the two directed tables, and the privacy invariant they must keep.

`messenger_social_edges` says two people share a conversation. It cannot say who spoke first
or who has gone quiet: its edges are undirected and its primary key has no room for a
direction. These two tables carry the half that does.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics.messenger_communities import ensure_messenger_analytics_tables
from topos.analytics.messenger_directed import (
    DEFAULT_SESSION_GAP_SECONDS,
    MESSENGER_DIRECTED_EDGES_TABLE,
    MESSENGER_DYAD_STATS_TABLE,
    PEER_CLASS_AUTOMATED,
    PEER_CLASS_HUMAN,
    classify_peer,
    create_directed_tables,
    dyad_key,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "d.db"))
    yield c
    c.close()


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_both_tables_arrive_through_the_existing_ensure(conn):
    """L1 must ride the messenger lane's own DDL site, not a registry migration — bumping
    user_version past the installed engine fences the node out of every write (2026-08-25)."""
    ensure_messenger_analytics_tables(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert MESSENGER_DIRECTED_EDGES_TABLE in names
    assert MESSENGER_DYAD_STATS_TABLE in names


def test_ensure_does_not_touch_user_version(conn):
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    ensure_messenger_analytics_tables(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == before


def test_creation_is_idempotent(conn):
    create_directed_tables(conn)
    create_directed_tables(conn)
    assert MESSENGER_DIRECTED_EDGES_TABLE in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- the privacy invariant ---

_FORBIDDEN = ("content", "snippet", "subject", "body", "text", "message", "hash", "preview")


@pytest.mark.parametrize("table", [MESSENGER_DIRECTED_EDGES_TABLE, MESSENGER_DYAD_STATS_TABLE])
def test_no_content_column_may_ever_exist(conn, table):
    """Schema-level, not a convention.

    A directed-edge table is exactly the shape that tempts someone to cache "the last thing
    they said" beside the counts. Counts and timestamps are aggregate; a snippet is the
    message itself, and storing it would put message bodies into an analytics table that no
    disclosure rule covers.
    """
    create_directed_tables(conn)
    for col in _cols(conn, table):
        for bad in _FORBIDDEN:
            assert bad not in col.lower(), f"{table}.{col} looks like content storage"


def test_the_directed_pk_actually_admits_direction(conn):
    """The reason this is a new table rather than a column on messenger_social_edges: that
    table's PK is (dataset_id, period_key, source_scope, source_id, target_id), so a
    direction column could not be part of the key and A->B would collide with B->A."""
    create_directed_tables(conn)
    pk = [r[1] for r in conn.execute(f"PRAGMA table_info({MESSENGER_DIRECTED_EDGES_TABLE})") if r[5]]
    assert "from_key" in pk and "to_key" in pk


def test_edge_kind_is_in_the_key_so_broadcast_cannot_swamp_dm(conn):
    """One message to a 10-person room would mint 9 directed edges. If those shared a key
    with DM rows, group broadcast would outweigh every real correspondence in any ranking
    reading this table — which is the exact failure the undirected lane already has."""
    from topos.analytics.messenger_directed import (
        EDGE_KIND_DM, EDGE_KIND_GROUP_BROADCAST, EDGE_KIND_GROUP_REPLY)

    create_directed_tables(conn)
    pk = [r[1] for r in conn.execute(f"PRAGMA table_info({MESSENGER_DIRECTED_EDGES_TABLE})") if r[5]]
    assert "edge_kind" in pk
    assert {EDGE_KIND_DM, EDGE_KIND_GROUP_REPLY, EDGE_KIND_GROUP_BROADCAST} == {
        "dm", "group_reply", "group_broadcast"}


def test_the_dyad_pk_is_unordered_and_the_edge_pk_is_ordered(conn):
    create_directed_tables(conn)
    dyad_pk = [r[1] for r in conn.execute(f"PRAGMA table_info({MESSENGER_DYAD_STATS_TABLE})") if r[5]]
    assert dyad_pk == ["dataset_id", "a_key", "b_key"]


def test_the_connector_is_a_first_class_column_not_a_partition(conn):
    """P0-4's lesson, inherited. `messenger_social_edges` partitioned on a joined
    `source_scope` string, which cost 2^n partitions of one corpus and still could not say
    which connector produced a given edge."""
    create_directed_tables(conn)
    assert "connector" in _cols(conn, MESSENGER_DIRECTED_EDGES_TABLE)
    assert "source_scope" not in _cols(conn, MESSENGER_DIRECTED_EDGES_TABLE)


def test_the_session_threshold_rides_on_every_row(conn):
    """So a later recalibration can tell which rows were produced under which threshold."""
    create_directed_tables(conn)
    assert "session_gap_seconds" in _cols(conn, MESSENGER_DIRECTED_EDGES_TABLE)
    assert DEFAULT_SESSION_GAP_SECONDS == 6 * 3600


def test_person_ids_are_nullable_so_l1_never_blocks_on_l0(conn):
    """L1 keys on a normalized identity key and carries nullable person ids for L0 to
    backfill. Blocking L1 on the person spine would trade the cheapest value in the plan for
    its most expensive prerequisite."""
    create_directed_tables(conn)
    notnull = {r[1] for r in conn.execute(
        f"PRAGMA table_info({MESSENGER_DIRECTED_EDGES_TABLE})") if r[3]}
    assert "from_person_id" not in notnull and "to_person_id" not in notnull


# --- the two helpers the whole layer keys on ---

def test_the_dyad_key_is_canonical_in_both_argument_orders():
    assert dyad_key("b", "a") == dyad_key("a", "b") == ("a", "b")


def test_a_shortcode_is_not_a_relationship():
    """29 of 179 DM peers on the first live corpus checked are shortcodes."""
    assert classify_peer("262966") == PEER_CLASS_AUTOMATED
    assert classify_peer("+15125551234") == PEER_CLASS_HUMAN
    assert classify_peer("someone@example.com") == PEER_CLASS_HUMAN
