"""The sweep's lineage fingerprint hashes exactly the facts the floors read, asked of the migration-78 keys.

`_lineage_fingerprint` is sealed into each index member at build time and re-derived by every
deep sweep, every 10 s, under the node write gate. Its fact net was a whole-table walk: every fact
whose reference text contains the record id anywhere, plus every fact whose references carry a
JSON escape. That read every fact per member per sweep, and it put each escape-bearing fact into
every member's hash, so one such write anywhere on the node dropped the whole index. The net is
now `_lineage_net`, built on `message_evidence.facts_naming`: the facts `_names_a_leaf` says name
the record, which is what `_floors` and the sibling floor read. These tests pin that the net is
exactly that set (with the keys installed, partly completed or absent, and never by comparing
two exceptions); that the hash covers it; that every change to which facts name the record, or
to a naming fact's payload, is still drift for p2c-v1 and direct members alike; and that facts
the floors never read no longer are.
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus
from tests.permissions_v2.test_bk3_lineage_keys import LEAVES, db, random_payload, random_refs, write_fact  # noqa: F401
from tests.permissions_v2.test_direct_message_search import node_for
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 (fixture)
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2.evidence import _COPY_COUNT, EvidenceResolver
from topos.permissions_v2.search_index import _lineage_fingerprint, _lineage_net, index_path
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lk

# These cases were written on the p2c-v1 profile, which a node no longer serves (N8): they run with its
# retirement lifted (conftest.py `retired_search_profile`), for the search code p2c-v3 shares with it.
pytestmark = pytest.mark.usefixtures("retired_search_profile")

TABLES = ("conversation_messages", "ai_chat_messages")


def floors_net(conn, member):
    """The definition, brute force: every fact, decided by the floors' predicate."""
    leaves = {member["record_id"]: {member["table"]}}
    return sorted((row[0], row[1] or "") for row in conn.execute(
        "SELECT object_id, payload_json, source_refs_json FROM signal_objects WHERE object_type='fact'")
        if EvidenceResolver._names_a_leaf(row[2], leaves))


def fingerprint_by_the_floors(conn, member, content):
    copies = sum(conn.execute(_COPY_COUNT.format(table=table), (content,)).fetchone()[0]
                 for table in TABLES) if isinstance(content, str) else -1
    return hashlib.sha256(json.dumps([floors_net(conn, member), copies], ensure_ascii=True).encode("ascii")).hexdigest()


def fingerprint_before(conn, member, content):
    """The pre-change net (instr or any escape), with the N1 copy count."""
    conn.row_factory = sqlite3.Row
    citing = sorted((row["object_id"], row["payload_json"] or "") for row in conn.execute(
        "SELECT object_id, payload_json FROM signal_objects WHERE object_type='fact' AND (instr(source_refs_json, ?)>0"
        r" OR source_refs_json GLOB '*\u00*' OR source_refs_json GLOB '*\/*')", (member["record_id"],)))
    copies = sum(conn.execute(_COPY_COUNT.format(table=table), (content,)).fetchone()[0]
                 for table in TABLES) if isinstance(content, str) else -1
    return hashlib.sha256(json.dumps([citing, copies], ensure_ascii=True).encode("ascii")).hexdigest()


def fuzzed(conn, seed, keys):
    rng = random.Random(seed)
    for number in range(900):
        write_fact(conn, f"fact-{number}", refs=random_refs(rng), payload=random_payload(rng))
    conn.commit()
    if keys == "absent":
        conn.execute("DROP TRIGGER fact_lineage_keys_ai")
    else:
        lk.complete_pending(conn, limit=None if keys == "complete" else 5)
    conn.commit()
    assert lk.installed(conn) is (keys != "absent")


@pytest.mark.parametrize("keys", ["complete", "partial", "absent"])
def test_the_net_is_exactly_the_facts_the_floors_read(db, keys):
    """Compared as fact lists, so a BLOB payload is data here, not an exception on both sides."""
    fuzzed(db, {"complete": 1, "partial": 2, "absent": 3}[keys], keys)
    nonempty = 0
    for leaf in LEAVES:
        for table in TABLES:
            member = {"table": table, "record_id": leaf}
            expected = floors_net(db, member)
            assert _lineage_net(db, member) == expected, (leaf, table)
            nonempty += bool(expected)
    assert nonempty >= len(LEAVES), "the fuzz must give most members naming facts"


@pytest.mark.parametrize("keys", ["complete", "partial", "absent"])
def test_the_fingerprint_hashes_that_net(db, keys):
    """On the same fuzz with every payload stored as text, so each comparison is a real hash."""
    fuzzed(db, {"complete": 4, "partial": 5, "absent": 6}[keys], keys)
    db.execute("UPDATE signal_objects SET payload_json=CAST(payload_json AS TEXT) WHERE typeof(payload_json)='blob'")
    db.commit()
    compared = 0
    for leaf in LEAVES:
        for table in TABLES:
            member = {"table": table, "record_id": leaf}
            assert floors_net(db, member)
            assert _lineage_fingerprint(db, member, "some text") == fingerprint_by_the_floors(db, member, "some text")
            compared += 1
    assert compared == len(LEAVES) * len(TABLES)


def test_a_naming_fact_with_a_blob_payload_still_fails_closed(db):
    """The hash cannot serialize a BLOB payload; before and after, the sweep's error path purges."""
    write_fact(db, "blob-payload", refs=json.dumps([{"record_id": LEAVES[0]}]),
               payload=json.dumps({"disclosure": "scoped"}).encode())
    db.commit()
    member = {"table": "conversation_messages", "record_id": LEAVES[0]}
    for fingerprint in (_lineage_fingerprint, fingerprint_before):
        with pytest.raises(TypeError):
            fingerprint(db, member, "some text")


# --- p2c-v1 members (the message-search corpus) ----------------------------------------------------


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=5, counts={name: 2 for name in mc.KINDS})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.rebuild()
    return node


def index_file(node):
    return index_path(node.index.root, node.search_raw["binding"]["grant_id"])


def add_fact(path, object_id, refs, *, payload=None):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                     "source_refs_json, valid_from, created_at, updated_at) VALUES (?,'profile','fact',?,?,?,"
                     "'2027-01-01T00:00:00Z','2027-01-01T00:00:00Z','2027-01-01T00:00:00Z')",
                     (object_id, "key-" + object_id, json.dumps(payload or {"disclosure": "scoped"}), refs))


def test_on_a_corpus_without_escapes_the_hash_is_the_one_sealed_before(node):
    """Where the two nets hold the same facts the bytes are the same, so an index sealed before stays current."""
    compared = 0
    with sqlite3.connect(node.corpus.path) as conn:
        for unit in node.corpus.units:
            member = {"table": "conversation_messages", "record_id": unit.message_id}
            assert _lineage_fingerprint(conn, member, unit.text) == fingerprint_before(conn, member, unit.text)
            compared += bool(_lineage_net(conn, member))
    assert compared, "some members must have naming facts"


def test_an_escape_bearing_fact_naming_nothing_here_is_no_longer_drift(node):
    add_fact(node.corpus.path, "unrelated-escaped", json.dumps([{"table": "conversation_messages",
                                                                 "record_id": "elsewhere:1", "note": "café"}]))
    with sqlite3.connect(node.corpus.path) as conn:
        assert conn.execute(r"SELECT count(*) FROM signal_objects WHERE source_refs_json GLOB '*\u00*'").fetchone()[0] == 1
    node.index.sweep(now=mc.NOW)
    assert index_file(node).exists()


def test_a_record_id_seen_only_inside_a_longer_id_is_no_longer_drift(node):
    member = next(unit for unit in node.corpus.units if unit.search_release)
    add_fact(node.corpus.path, "longer-id", json.dumps([{"table": "conversation_messages", "record_id": member.message_id + "0"}]))
    node.index.sweep(now=mc.NOW)
    assert index_file(node).exists()


def test_the_id_under_the_other_evidence_table_is_no_longer_drift(node):
    """`_names_a_leaf` rules a matching id out when its table names a different evidence table."""
    member = next(unit for unit in node.corpus.units if unit.search_release)
    add_fact(node.corpus.path, "other-table", json.dumps([{"table": "ai_chat_messages", "record_id": member.message_id}]))
    node.index.sweep(now=mc.NOW)
    assert index_file(node).exists()


NAMING_SHAPES = [
    lambda rid: json.dumps([{"table": "conversation_messages", "record_id": rid}]),
    lambda rid: json.dumps([{"record_id": rid}]),                                      # no table: counts
    lambda rid: '[{"table": "conversation_messages", "record_id": "' + rid.replace(":", "\\u003a") + '"}]',  # JSON escape
    lambda rid: "not json " + rid,                                                     # unreadable: substring counts
    lambda rid: json.dumps([{"source": "note", "text": "see " + rid}]),               # no usable record_id
]
NAMING_IDS = ["exact", "no_table", "escaped", "unreadable", "no_record_id"]


@pytest.mark.parametrize("refs", NAMING_SHAPES, ids=NAMING_IDS)
def test_every_fact_the_floors_read_is_still_drift(node, refs):
    member = next(unit for unit in node.corpus.units if unit.search_release)
    add_fact(node.corpus.path, "naming", refs(member.message_id))
    node.index.sweep(now=mc.NOW)
    assert not index_file(node).exists()


SIBLING = {"subject_entity_id": mc.OWNER_ENTITY, "predicate": "likes", "object_value": "a sibling claim",
           "disclosure": "scoped", "asserted_by": "owner"}


@pytest.mark.parametrize("change", [
    "UPDATE signal_objects SET payload_json=json_set(payload_json, '$.disclosure', 'unknown') WHERE object_id='sibling'",
    "UPDATE signal_objects SET source_refs_json=json_set(source_refs_json, '$[0].record_id', 'elsewhere:1') "
    "WHERE object_id='sibling'",
    "DELETE FROM signal_objects WHERE object_id='sibling'",
], ids=["disclosure", "repointed", "deleted"])
def test_a_change_to_a_non_witness_sibling_is_drift_for_the_sweep_alone(node, caplog, change):
    """A fact naming the record that is not its witness: only the lineage net can see it change."""
    member = next(unit for unit in node.corpus.units if unit.search_release)
    add_fact(node.corpus.path, "sibling", json.dumps([{"table": "conversation_messages", "record_id": member.message_id}]),
             payload=SIBLING)
    node.rebuild()
    assert index_file(node).exists()
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute(change)
    node.search_request("roadmap deploy review", k=5)
    assert index_file(node).exists()                                    # the request path scans no lineage ...
    with caplog.at_level("WARNING", logger="topos.permissions_v2.search_index"):
        node.index.sweep(now=mc.NOW)
    assert not index_file(node).exists()                                # ... the sweep drops it, as lineage
    assert "message search index stale (lineage)" in caplog.text


# --- direct members (p2c-v2; the live p2c-v3 grant's members are the same shape) ---------------------


def direct_node(legacy, tmp_path, monkeypatch, keys, sibling=False):
    conn = legacy[1]
    if keys:
        lk.apply_permissions_fact_lineage_keys_v1_up(conn)
    if sibling:
        add_fact_conn(conn, "sibling", json.dumps([{"table": "conversation_messages", "record_id": "imessage:1"}]), SIBLING)
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    assert node.rebuild()["grant-search"] == "ready"
    assert lk.installed(conn) is keys
    return node, conn


def add_fact_conn(conn, object_id, refs, payload=None):
    conn.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                 "source_refs_json, valid_from, created_at, updated_at) VALUES (?,'profile','fact',?,?,?,'t','t','t')",
                 (object_id, "key-" + object_id, json.dumps(payload or {"disclosure": "scoped"}), refs))
    conn.commit()


@pytest.mark.parametrize("keys", [True, False], ids=["lineage_keys", "table_walk"])
@pytest.mark.parametrize("refs", NAMING_SHAPES, ids=NAMING_IDS)
def test_a_direct_member_gaining_a_naming_fact_is_drift(legacy, tmp_path, monkeypatch, caplog, keys, refs):
    node, conn = direct_node(legacy, tmp_path, monkeypatch, keys)
    add_fact_conn(conn, "naming", refs("imessage:1"))
    with caplog.at_level("WARNING", logger="topos.permissions_v2.search_index"):
        node.index.sweep(now=node.now[0])
    assert not index_file(node).exists()
    assert "message search index stale (lineage)" in caplog.text


@pytest.mark.parametrize("keys", [True, False], ids=["lineage_keys", "table_walk"])
def test_a_direct_member_ignores_an_escape_bearing_fact_naming_nothing_here(legacy, tmp_path, monkeypatch, keys):
    node, conn = direct_node(legacy, tmp_path, monkeypatch, keys)
    add_fact_conn(conn, "unrelated-escaped", json.dumps([{"record_id": "elsewhere:1", "note": "café"}]))
    node.index.sweep(now=node.now[0])
    assert index_file(node).exists()


@pytest.mark.parametrize("keys", [True, False], ids=["lineage_keys", "table_walk"])
@pytest.mark.parametrize("change", [
    "UPDATE signal_objects SET payload_json=json_set(payload_json, '$.disclosure', 'unknown') WHERE object_id='sibling'",
    "UPDATE signal_objects SET source_refs_json=json_set(source_refs_json, '$[0].record_id', 'elsewhere:1') "
    "WHERE object_id='sibling'",
    "DELETE FROM signal_objects WHERE object_id='sibling'",
], ids=["disclosure", "repointed", "deleted"])
def test_a_direct_member_sibling_changing_is_drift(legacy, tmp_path, monkeypatch, caplog, keys, change):
    node, conn = direct_node(legacy, tmp_path, monkeypatch, keys, sibling=True)
    conn.execute(change)
    conn.commit()
    with caplog.at_level("WARNING", logger="topos.permissions_v2.search_index"):
        node.index.sweep(now=node.now[0])
    assert not index_file(node).exists()
    assert "message search index stale (lineage)" in caplog.text
