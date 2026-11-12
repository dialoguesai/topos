"""A fact read WITH other records is not shared through a grant (DR-62, owner, 26 Oct 2026).

The two Moments packs read an owner's message with the turns before it. The fact rests on the owner's message
alone (`source_refs_json`); the turns are kept beside it as `payload["context_refs"]` and only protect it. Its
description may name what only the other person's turn said.

The all-pack fact projection qualifies `source_refs_json` and nothing else, so on its own it would release such
a fact whatever became of the turns: gone, or marked owner-only. `expanded_facts.authorize` refuses it instead,
for the fact itself and for any fact that depends on it. Whether a fact read with context may ever be shared,
and with which checks on its turns, is a ruling nobody has made.

Synthetic rows in a temporary database.
"""
import json
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_all_fact_grants import clone_derived, expanded_node, pack_fact
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401  (fixture)
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401  (fixture)
from topos.permissions_v2 import selective_facts as sf
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.expanded_facts import authorize

TURN = [{"table": "conversation_messages", "record_id": "turn-before-1"}]


def _payload(conn, fid):
    return json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?", (fid,)).fetchone()[0])


def _set(conn, fid, **change):
    payload = {**_payload(conn, fid), **change}
    conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (json.dumps(payload), fid))
    conn.commit()


def _released(node):
    node.rebuild()
    result, error = node.search_request("owner", k=10)
    return [] if error is not None else [r for r in result["records"] if r.get("kind") == "fact"]


@pytest.mark.parametrize("oid", ["moments.joy", "moments.tension"])
def test_a_moments_fact_read_alone_is_shared_and_one_read_with_turns_is_not(legacy, tmp_path, monkeypatch, oid):  # noqa: F811
    fid = pack_fact(legacy, oid)
    node = expanded_node(legacy, tmp_path, monkeypatch, oid)
    assert len(_released(node)) == 1, "the control: the grant does release this pack's facts"
    _set(legacy[1], fid, context_refs=[])
    assert len(_released(node)) == 1, "an empty list is a fact read alone"
    _set(legacy[1], fid, context_refs=TURN)
    assert _released(node) == []
    output, error = node.search_request("River", k=10)
    assert error is not None or not output["records"]


def test_the_refusal_does_not_depend_on_what_became_of_the_turn(legacy, tmp_path, monkeypatch):  # noqa: F811
    """A turn still there, one gone, one the owner marked owner-only, a value that is no list at all: the turn
    is never looked up here, so nothing about it can let the fact through."""
    fid = pack_fact(legacy, "moments.joy")
    conn = legacy[1]
    cursor = conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (fid,))
    row = dict(zip((column[0] for column in cursor.description), cursor.fetchone()))
    policy = SimpleNamespace(search=SimpleNamespace(selective_facts=sf.parse_selection(
        sf.selection_v2(["moments.joy"], assertions=["owner_stated", "inferred"]))))
    assert authorize(row, _payload(conn, fid), policy) == "moments.joy"
    assert authorize(row, {**_payload(conn, fid), "context_refs": []}, policy) == "moments.joy"
    conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages','turn-kept-private')")
    conn.commit()
    for refs in (TURN, [{"table": "conversation_messages", "record_id": "imessage:1"}],
                 [{"table": "conversation_messages", "record_id": "turn-kept-private"}], "not a list", {"a": 1}):
        with pytest.raises(PolicyError) as refused:
            authorize(row, {**_payload(conn, fid), "context_refs": refs}, policy)
        assert refused.value.code == "fact_lineage_unsupported", refs
    node = expanded_node(legacy, tmp_path, monkeypatch, "moments.joy")
    _set(conn, fid, context_refs=[{"table": "conversation_messages", "record_id": "turn-kept-private"}])
    assert _released(node) == []


def test_a_fact_that_depends_on_one_read_with_turns_is_not_shared_either(legacy, tmp_path, monkeypatch):  # noqa: F811
    oid = "communication.style"
    fid = pack_fact(legacy, oid)
    clone_derived(legacy, fid)
    node = expanded_node(legacy, tmp_path, monkeypatch, oid)
    assert len(_released(node)) == 2, "the control: a plain fact and the one derived from it are both released"
    _set(legacy[1], fid, context_refs=TURN)
    assert _released(node) == [], "the dependency is refused, and the derived fact with it"
