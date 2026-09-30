"""The sweep's exact-copy count reads the migration-76 content key, and answers what the scan answered.

`_lineage_fingerprint` is sealed into every index member at build time and re-derived by every
deep sweep, every 10 s, under the node write gate. Its copy count used to be a bare `content=?`
over both message tables, a scan of all message text per member per sweep. It is now the floor's
own statement (`evidence._COPY_COUNT`), which the planner answers from `idx_<table>_content_key`.
These tests pin that the sweep issues that statement, that the fingerprint bytes equal the
pre-change formula's on the rows where a key and a scan could disagree (so an index sealed
before the change stays current after it), and that a later copy is still drift.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus
from topos.permissions_v2 import evidence as evidence_module
from topos.permissions_v2.evidence import LEAF_TABLES
from topos.permissions_v2.search_index import _lineage_fingerprint, index_path

LONG = "a" * 64


def fingerprint_before(conn, member, content):
    """The pre-change formula, verbatim but for the name: the bare-content count."""
    conn.row_factory = sqlite3.Row
    citing = sorted((row["object_id"], row["payload_json"] or "") for row in conn.execute(
        "SELECT object_id, payload_json FROM signal_objects WHERE object_type='fact' AND (instr(source_refs_json, ?)>0"
        r" OR source_refs_json GLOB '*\u00*' OR source_refs_json GLOB '*\/*')",
        (member["record_id"],)))
    copies = sum(conn.execute(f"SELECT count(*) FROM {table} WHERE content=?", (content,)).fetchone()[0]
                 for table in ("conversation_messages", "ai_chat_messages")) if isinstance(content, str) else -1
    return hashlib.sha256(json.dumps([citing, copies], ensure_ascii=True).encode("ascii")).hexdigest()


# Rows where a key and the full text could disagree: twins, a shared 64-character prefix and length
# with a different tail, NUL-bearing text (SQLite's `length` stops at the NUL), case, multi-byte text
# (`substr` counts characters, not bytes), whitespace-only, a BLOB beside equal text, and one copy in
# each table. Both `content` columns are declared TEXT, so an equal row is the same bytes.
ROWS = {
    "conversation_messages": [("twin-1", "twins"), ("twin-2", "twins"), ("tail-1", LONG + "x"), ("tail-2", LONG + "y"),
                              ("nul-1", "ab\x00c"), ("nul-2", "ab\x00c"), ("nul-3", "ab\x00d"), ("case-1", "Twins"),
                              ("wide-1", "é" * 70), ("wide-2", "é" * 69 + "e"), ("blank-1", "   "),
                              ("cross-1", "I enjoy reading history books."), ("blob-1", b"twins")],
    "ai_chat_messages": [("cross-2", "I enjoy reading history books."), ("chat-twin", "twins")],
}
PROBES = ["twins", "Twins", LONG + "x", LONG + "z", "ab\x00c", "ab\x00d", "ab\x00", "é" * 70, "é" * 69 + "e",
          "   ", "", "I enjoy reading history books.", "never written", None, 7, b"twins"]


def insert_chat(conn, message_id, content):
    """One ai_chat_messages row in the production shape (its NOT NULL columns filled)."""
    conn.execute("INSERT INTO ai_chat_messages(message_id, conversation_id, sender_type, event_at, content, source_id) "
                 "VALUES (?, 'chat-1', 'user', '2027-01-10T00:00:00Z', ?, 'chatgpt')", (message_id, content))


@pytest.fixture
def corpus(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=11, counts={"clean_positive_C": 2})
    with sqlite3.connect(corpus.path) as conn:
        for message_id, content in ROWS["conversation_messages"]:
            mc.insert_message(conn, message_id=message_id, source_id=mc.SOURCE, content=content,
                              event_at="2027-01-10T00:00:00Z")
        for message_id, content in ROWS["ai_chat_messages"]:
            insert_chat(conn, message_id, content)
    return corpus


def drop_content_keys(path):
    with sqlite3.connect(path) as conn:
        for table in LEAF_TABLES:
            conn.execute(f"DROP INDEX IF EXISTS idx_{table}_content_key")


@pytest.mark.parametrize("indexed", [True, False], ids=["content_key", "no_index"])
def test_the_fingerprint_is_the_one_the_bare_count_gave(corpus, indexed):
    if not indexed:
        drop_content_keys(corpus.path)
    with sqlite3.connect(corpus.path) as conn:
        members = [{"table": "conversation_messages", "record_id": unit.message_id} for unit in corpus.units]
        members += [{"table": "conversation_messages", "record_id": message_id}
                    for message_id, _content in ROWS["conversation_messages"]]
        compared = 0
        for member in members:
            for content in PROBES:
                assert _lineage_fingerprint(conn, member, content) == fingerprint_before(conn, member, content), \
                    (member["record_id"], repr(content))
                compared += 1
    assert compared == len(members) * len(PROBES)


def test_the_sweep_issues_the_indexed_statement_and_the_planner_answers_it_from_the_key(corpus):
    executed = []
    with sqlite3.connect(corpus.path) as conn:
        conn.set_trace_callback(executed.append)
        _lineage_fingerprint(conn, {"table": "conversation_messages", "record_id": "twin-1"}, "twins")
        conn.set_trace_callback(None)
        counts = [statement for statement in executed if "count(*)" in statement]
        assert counts == [evidence_module._COPY_COUNT.format(table=table).replace("?1", "'twins'")
                          for table in ("conversation_messages", "ai_chat_messages")], counts
        for table in LEAF_TABLES:
            steps = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + evidence_module._COPY_COUNT.format(table=table),
                                                    ("twins",))]
            assert steps == [f"SEARCH {table} USING INDEX idx_{table}_content_key (<expr>=? AND <expr>=?)"], steps


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=5, counts={name: 2 for name in mc.KINDS})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.rebuild()
    return node


@pytest.mark.parametrize("table", LEAF_TABLES)
def test_a_later_exact_copy_is_dropped_by_the_owner_sweep_not_the_request(node, table):
    member = next(unit for unit in node.corpus.units if unit.search_release)
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    with sqlite3.connect(node.corpus.path) as conn:
        if table == "conversation_messages":
            mc.insert_message(conn, message_id="copy-1", source_id=mc.OTHER_SOURCE, content=member.text,
                              event_at="2027-01-10T00:00:00Z", is_from_self=0, conversation_id="conversation-2")
        else:
            insert_chat(conn, "copy-1", member.text)
    output, refused = node.search_request("roadmap deploy review", k=5)
    assert refused is None and member.text not in json.dumps(output)  # the re-check refuses the member ...
    assert path.exists()                                                # ... and the request path scans no lineage
    node.index.sweep(now=mc.NOW)
    assert not path.exists()


def test_an_unrelated_new_message_is_not_drift(node):
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    with sqlite3.connect(node.corpus.path) as conn:
        mc.insert_message(conn, message_id="unrelated-1", source_id=mc.OTHER_SOURCE, content="a text no member has",
                          event_at="2027-01-10T00:00:00Z", is_from_self=0, conversation_id="conversation-2")
    node.index.sweep(now=mc.NOW)
    assert path.exists()
