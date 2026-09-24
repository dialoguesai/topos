"""The shadow audit's re-score finds what a p2a-v3 release released (C6; found on the beta stack, 24 Sep).

At e848f512 every p2a-v3 re-score came back `unresolved` / `records_unavailable`, and no labeler was ever asked.
`shadow_index.record_release` was handed the disclosure's own records, whose ids are opaque under p2a-v3, and it sealed
`record_id=opaque`: the pointer never held the row's id, so `resolve_records` looked up `message_id='r.…'` and found
nothing. Measured on the stack: 31 index rows, every pointer present and every key held, 30 of 30 samples unresolved.

The suite could not see it. The pointer test pinned the opaque id as the pointer's content, and every scoring test
stubbed `resolve_records` out, so the writer and the reader were never run against each other. These tests drive the
real release (a real ledger, real signatures, the production DDL) and then the real resolver:

  R1  a p2a-v3 release resolves to exactly the rows it released, with their content, and the index row still holds
      no canonical id in the clear
  R2  the row is keyed the way the evidence path keys it (dataset included); a message deleted since is a missing row
  R3  each hole has its own reason, in the control plane's reason grammar; a pointer written before this fix is
      named for what it is, never guessed at
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.production_node import Node
from tests.permissions_v2.test_bk3_opaque_ids import PINNED_ORDER_KEY, V3, v3_policy
from topos.permissions_v2 import release, shadow_index, shadow_labelers, shadow_rescore
from topos.permissions_v2.canonical import canonical_bytes
from topos.permissions_v2.evidence import ReviewedClassification
from topos.permissions_v2.opaque_ids import RecordKeys, opaque_record_id, seal_key

pytestmark = [pytest.mark.p0]

# control_plane/permissions_v2/shadow_rescore.py `_REASON`: anything else is filed as `unspecified`.
CP_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _Labeler:
    id, family = "local-test", "test"

    def __init__(self):
        self.seen = []

    def score(self, records, policy=None):
        self.seen.append(records)
        return "agree"


@pytest.fixture(autouse=True)
def _clean():
    shadow_labelers.clear()
    shadow_index.reset_failures()
    yield
    shadow_labelers.clear()
    shadow_index.reset_failures()


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SHADOW_INDEX_ENABLED", "true")
    corpus = pc.build(tmp_path / "corpus", seed=33, positives=3)
    (corpus.path.parent / "permissions-v2").mkdir(mode=0o700)
    return Node(corpus, tmp_path, policy=v3_policy())


def runtime(node):
    return SimpleNamespace(protocol=node.protocol)


def question(request_id, grant_id="grant-1"):
    return shadow_rescore.RescoreRequest.parse({
        "version": shadow_rescore.VERSION, "request_id": request_id, "grant_id": grant_id, "capability": V3,
        "output_sha256": "a" * 64, "labeler_mode": "local"})


def release_one(node, request_id="read-1", positive=0):
    fact = node.corpus.positives[positive]
    released, reason = node.read(fact, request_id=request_id)
    assert reason is None, reason
    return fact, released[1]


def rescored(node, request_id):
    shadow_labelers.register("local", _Labeler())
    result = shadow_rescore.rescore(runtime(node), question(request_id).model_dump())
    assert result.reason is None or CP_REASON.fullmatch(result.reason), result.reason
    return result


# --------------------------------------------------------------------------- R1


def test_R1_a_p2a_v3_release_resolves_to_the_rows_it_released(node):
    fact, output = release_one(node)
    assert all(re.fullmatch(r"r\.[0-9a-f]{64}", record["record_id"]) for record in output["records"])
    records = shadow_rescore.resolve_records(runtime(node), question("read-1"))
    assert [record["content"] for record in records] == [record["content"] for record in output["records"]]
    assert [record["record_id"] for record in records] == [node.corpus.messages[fact]]
    assert {record["canonical_table"] for record in records} == {"conversation_messages"}


def test_R1b_the_labeler_is_asked_and_the_reply_is_its_verdict(node, monkeypatch):
    release_one(node)
    monkeypatch.setattr(shadow_rescore, "now_seconds", lambda: node.now[0])     # the grant is valid on the node's clock
    labeler = _Labeler()
    shadow_labelers.register("local", labeler)
    result = shadow_rescore.rescore(runtime(node), question("read-1").model_dump())
    assert (result.verdict, result.reason, result.labeler) == ("agree", None, "local-test")
    assert len(labeler.seen) == 1 and labeler.seen[0][0]["content"]


def reordered_node(tmp_path):
    """test_O6's corpus: one fact, three leaves, and a pinned key under which the opaque order is NOT the canonical
    one. Only such a release can tell a record that kept its own row from one handed its neighbour's."""
    corpus = pc.build(tmp_path / "corpus", seed=36, positives=1)
    (corpus.path.parent / "permissions-v2").mkdir(mode=0o700)
    fact = corpus.positives[0]
    with sqlite3.connect(corpus.path) as conn:
        refs = json.loads(conn.execute("SELECT source_refs_json FROM signal_objects WHERE object_id=?", (fact,)).fetchone()[0])
        for record_id, event in (("imessage:0000001", 1), ("imessage:9999999", 2)):
            pc.insert_message(conn, message_id=record_id, content=f"extra leaf {event}", event_at=pc.NOW - 99 * event)
            refs.append({"table": "conversation_messages", "record_id": record_id, "source_id": pc.SOURCE,
                         "dataset_id": pc.DATASET})
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps(refs), fact))
    with pc.owner():
        snapshot = corpus.resolver.inspect_for_review(fact)
        corpus.reviews.record_review(resolver=corpus.resolver, review_id="review-reordered", expected_snapshot=snapshot,
            classifications=[ReviewedClassification(evidence=version, domains=["work"], sensitivity="none",
                subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
                independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves],
            reviewed_at=pc.NOW - 30)
    node = Node(corpus, tmp_path, policy=v3_policy())
    keys = RecordKeys(release.record_keys_root(corpus.path))
    with keys._db() as db:
        db.execute("INSERT INTO p2c_record_keys VALUES (?, ?)", ("grant-1", PINNED_ORDER_KEY))
    return node, fact, keys.get("grant-1", create=False)


def test_R1d_each_record_keeps_its_own_row_through_the_opaque_sort(tmp_path, monkeypatch):
    """The release re-orders its records by opaque id after building them. An identity list left in the canonical
    order would seal each pointer to ANOTHER record's row, and the labeler would score the wrong message."""
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SHADOW_INDEX_ENABLED", "true")
    node, fact, key = reordered_node(tmp_path)
    released, reason = node.read(fact, request_id="read-1")
    assert reason is None, reason
    output = released[1]
    opaque = [record["record_id"] for record in output["records"]]
    canonical_order = [opaque_record_id(key, grant_id="grant-1", table="conversation_messages", source_id=pc.SOURCE,
                                        dataset_id=pc.DATASET, record_id=record_id)
                       for record_id in sorted([node.corpus.messages[fact], "imessage:0000001", "imessage:9999999"])]
    assert len(opaque) == 3 and opaque != canonical_order          # the pin holds: the sort moved the records
    records = shadow_rescore.resolve_records(runtime(node), question("read-1"))
    assert [record["content"] for record in records] == [record["content"] for record in output["records"]]
    assert [opaque_record_id(key, grant_id="grant-1", table="conversation_messages", source_id=pc.SOURCE,
                             dataset_id=pc.DATASET, record_id=record["record_id"]) for record in records] == opaque


def test_R1c_the_index_row_still_holds_no_canonical_id_in_the_clear(node, tmp_path):
    fact, output = release_one(node)
    canonical = node.corpus.messages[fact]
    with node.ledger._transaction() as conn:
        [row] = shadow_index.released(conn, request_id="read-1")
    clear = [value for name, value in row.items() if name != "sealed_pointer"]
    assert row["opaque_record_id"] == output["records"][0]["record_id"]
    assert all(canonical not in str(value) and canonical.split(":")[1] not in str(value) for value in clear)
    assert canonical.encode() not in bytes(row["sealed_pointer"])


# --------------------------------------------------------------------------- R2


def test_R2_a_row_that_left_the_released_dataset_is_not_read(node):
    """`evidence._load` keys a conversation row by message, source AND dataset; the re-score reads the same row."""
    fact, _output = release_one(node)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("UPDATE conversation_messages SET dataset_id='another-dataset' WHERE message_id=?",
                     (node.corpus.messages[fact],))
    assert rescored(node, "read-1").reason == "records_unavailable_row_missing"


def test_R2b_a_row_gone_since_the_release_is_named(node):
    """Neither message table carries a deletion marker (the production DDL): a deleted message is a missing row."""
    fact, _output = release_one(node)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("DELETE FROM conversation_messages WHERE message_id=?", (node.corpus.messages[fact],))
    assert rescored(node, "read-1").reason == "records_unavailable_row_missing"


# --------------------------------------------------------------------------- R3


def test_R3_a_request_the_node_never_indexed_is_named_and_logged(node, caplog):
    """The hole that hid the pointer bug returned None and logged nothing. Now the code is logged, never an id."""
    with caplog.at_level(logging.INFO, logger="topos.permissions_v2.shadow_rescore"):
        assert rescored(node, "never-released").reason == "records_unavailable_not_indexed"
    assert "records_unavailable_not_indexed" in caplog.text and "never-released" not in caplog.text


def test_R3b_a_grant_whose_key_is_forgotten(node):
    """A revoked grant forgets its key, which is correct; its samples are holes, and say so."""
    release_one(node)
    RecordKeys(release.record_keys_root(node.corpus.path)).delete("grant-1")
    assert rescored(node, "read-1").reason == "records_unavailable_key_forgotten"


def test_R3c_a_pointer_that_will_not_open(node):
    release_one(node)
    with node.ledger._transaction() as conn:
        [row] = shadow_index.released(conn, request_id="read-1")
        damaged = bytes(row["sealed_pointer"][:-1]) + bytes([row["sealed_pointer"][-1] ^ 1])
        conn.execute("UPDATE p2a_shadow_released SET sealed_pointer=? WHERE request_id=?", (damaged, "read-1"))
    assert rescored(node, "read-1").reason == "records_unavailable_pointer_unopenable"


def test_R3d_a_pointer_written_before_this_fix_is_named_not_guessed(node):
    """e848f512 sealed the opaque id itself. Such a row can never resolve: no key recovers an id never written."""
    release_one(node)
    key = RecordKeys(release.record_keys_root(node.corpus.path)).get("grant-1", create=False)
    with node.ledger._transaction() as conn:
        [row] = shadow_index.released(conn, request_id="read-1")
        opaque = row["opaque_record_id"]
        # e848f512's construction, byte for byte: the two-field body, the record's own id in it.
        nonce = bytes(12)
        body = canonical_bytes({"record_id": opaque, "canonical_table": row["canonical_table"]})
        legacy = nonce + AESGCM(seal_key(key)).encrypt(nonce, body, opaque.encode("ascii"))
        conn.execute("UPDATE p2a_shadow_released SET sealed_pointer=? WHERE request_id=?", (legacy, "read-1"))
    assert rescored(node, "read-1").reason == "records_unavailable_pointer_opaque"


def test_R3d2_a_pointer_that_disagrees_with_its_index_row_is_not_followed(node):
    """The row's source is in the clear and the pointer's is sealed; a pair that disagrees points somewhere this
    release did not read."""
    release_one(node)
    with node.ledger._transaction() as conn:
        conn.execute("UPDATE p2a_shadow_released SET source_id='another-source' WHERE request_id=?", ("read-1",))
    assert rescored(node, "read-1").reason == "records_unavailable_index_integrity"


def test_R3e_a_canonical_database_it_cannot_read_is_a_hole_with_a_name(node):
    """The key store sits beside the database and stays readable: only the row lookup fails."""
    release_one(node)
    path = node.corpus.path
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        assert rescored(node, "read-1").reason == "records_unavailable_lookup_failed"
    finally:
        path.chmod(mode)


def test_R3e2_a_canonical_database_that_opens_but_cannot_answer_is_the_same_hole(node):
    """Schema drift, a locked or damaged file: the query itself fails, after the database opened."""
    release_one(node)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("ALTER TABLE conversation_messages RENAME TO conversation_messages_moved")
    assert rescored(node, "read-1").reason == "records_unavailable_lookup_failed"


def test_R3f_anything_else_is_a_hole_named_by_its_stage_never_by_its_text(node, monkeypatch, caplog):
    release_one(node)

    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE DETAIL")
    monkeypatch.setattr(shadow_index, "released", broken)
    with caplog.at_level(logging.INFO, logger="topos.permissions_v2.shadow_rescore"):
        assert rescored(node, "read-1").reason == "records_unavailable_resolver_failed"
    assert "RuntimeError" in caplog.text and "PRIVATE DETAIL" not in caplog.text


def test_R3g_every_reason_is_one_the_control_plane_files_as_itself():
    for reason in shadow_rescore.RECORDS_UNAVAILABLE_REASONS:
        assert CP_REASON.fullmatch(reason), reason
