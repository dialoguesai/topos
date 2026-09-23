"""F3/F4 (node): per-read ledger rows compact to hash-only tombstones past the envelope window.

`p2a_requests.envelope_json` (a whole signed envelope, ~2.8 KB) is needed only until
the request is checkpointed or its envelope expires. Past `expires_at + SKEW` the row
keeps request_id, envelope_hash and status: the replay tombstone. Receipts, the owner's
audit, are never touched.

  L1  expired rows compact, a bounded batch per admission
  L2  a compacted request id still refuses a replay, even with the clock stepped back
  L3  unexpired rows and every receipt are untouched
  L4  a compacted row is small
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.production_node import Node
from tests.permissions_v2.test_release import dispatch
from topos.permissions_v2 import ledger_retention
from topos.permissions_v2.canonical import PolicyError


@pytest.fixture
def node(tmp_path):
    corpus = pc.build(tmp_path / "corpus", seed=41, positives=2)
    return Node(corpus, tmp_path)


def rows(node):
    with sqlite3.connect(node.ledger.path) as conn:
        return conn.execute("SELECT request_id, envelope_hash, status, envelope_json FROM p2a_requests ORDER BY request_id").fetchall()


def admit(node, request_id):
    envelope, payload = node.issue(node.corpus.positives[0], request_id=request_id)
    try:
        dispatch(node.setup, envelope, payload, request_id=request_id, send=lambda *_: None)
    except PolicyError:
        pass
    return envelope, payload


def test_L1_expired_rows_compact_in_bounded_batches(node, monkeypatch):
    monkeypatch.setattr(ledger_retention, "BATCH", 3)
    for number in range(7):
        admit(node, f"old-{number}")
    node.now[0] += 100 + ledger_retention.SKEW + 1
    admit(node, "new-1")
    compacted = [row for row in rows(node) if row[3] == ""]
    assert len(compacted) == 3
    admit(node, "new-2")
    assert len([row for row in rows(node) if row[3] == ""]) == 6


def test_L2_a_compacted_id_still_refuses_a_replay_with_the_clock_stepped_back(node):
    envelope, payload = admit(node, "old-1")
    node.now[0] += 100 + ledger_retention.SKEW + 1
    admit(node, "new-1")
    assert dict((row[0], row[3]) for row in rows(node))["old-1"] == ""
    node.now[0] -= 100 + ledger_retention.SKEW + 1
    with pytest.raises(PolicyError, match="request_replay"):
        dispatch(node.setup, envelope, payload, request_id="old-1", send=lambda *_: None)


def test_L3_unexpired_rows_and_receipts_are_untouched(node):
    admit(node, "old-1")
    with sqlite3.connect(node.ledger.path) as conn:
        receipts = conn.execute("SELECT * FROM p2a_receipts").fetchall()
    node.now[0] += 50
    admit(node, "new-1")
    assert all(row[3] != "" for row in rows(node))
    node.now[0] += 100 + ledger_retention.SKEW
    admit(node, "new-2")
    with sqlite3.connect(node.ledger.path) as conn:
        assert conn.execute("SELECT * FROM p2a_receipts").fetchall()[:len(receipts)] == receipts
    assert {row[0]: row[3] == "" for row in rows(node)} == {"old-1": True, "new-1": False, "new-2": False}


def test_L4_a_compacted_row_is_small(node):
    admit(node, "old-1")
    node.now[0] += 100 + ledger_retention.SKEW + 1
    admit(node, "new-1")
    row = dict((row[0], row) for row in rows(node))["old-1"]
    assert sum(len(str(value)) for value in row) <= 200


def test_L1_the_prune_reads_the_expiry_index_not_the_table(node):
    with sqlite3.connect(node.ledger.path) as conn:
        plan = [row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM p2a_requests WHERE envelope_json<>'' "
            "AND json_extract(envelope_json,'$.expires_at') < ? LIMIT ?", (0, 1))]
    assert any("p2a_requests_expiry" in step for step in plan), plan
