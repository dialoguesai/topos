"""F1: the locator view's record ids are opaque per grant (p2a-v3, canonical.message_disclosure.v2).

`imessage:<ROWID>` counts the owner's whole store, so two released ids told the
recipient how many messages lay between them (design §6.4 channel 11). p2a-v3
releases `r.` + HMAC-SHA256 under a per-grant key from `opaque_ids`, the module
the search stream owns, imported unchanged.

  O1  ids are opaque, stable within a grant, unrelated across grants, and equal to
      the id message search derives for the same grant and record
  O2  nothing of the canonical id reaches the recipient
  O3  the node refuses p2a-v1 and p2a-v2 releases (their view is ordinal)
  O4  deleting the grant's key (what a revoke does) changes every id
  O5  opaque_ids.py is byte-identical to the search stream's blob
  O6  the ORDER of the released records is the opaque order, not the canonical one, and
      this door admits only the capabilities whose view it knows
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.production_node import Node, work_policy
from topos.permissions_v2 import release
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.opaque_ids import RecordKeys, opaque_record_id

V3 = "permissions-beta/p2a-v3"
# A record key whose opaque order for O6's three leaves is not their canonical order.
PINNED_ORDER_KEY = bytes(range(200, 232))
OPAQUE = re.compile(r"r\.[0-9a-f]{64}")
# git blob of topos/permissions_v2/opaque_ids.py on beta/p2c-search at e140b145.
SEARCH_STREAM_BLOB = "5c19f7f07f001815d2267f36fd648f8ab8d23388"


def v3_policy(grant_id="grant-1"):
    def build(binding):
        raw = work_policy(binding, capability=V3, evaluator="hard-rules/p2a-v3", view="canonical.message_disclosure.v2",
                          grant_id=grant_id)
        raw["binding"]["assignment_id"] = "assignment-" + grant_id
        raw["policy_version_id"] = "policy-" + grant_id
        return raw
    return build


@pytest.fixture
def node(tmp_path):
    corpus = pc.build(tmp_path / "corpus", seed=33, positives=3)
    (corpus.path.parent / "permissions-v2").mkdir(mode=0o700)
    return Node(corpus, tmp_path, policy=v3_policy())


def released_ids(node, fact, request_id, grant_id="grant-1"):
    released, reason = node.read(fact, request_id=request_id, grant_id=grant_id)
    assert reason is None, reason
    return [record["record_id"] for record in released[1]["records"]], released[1]


def test_O1_ids_are_opaque_stable_and_per_grant(node):
    fact = node.corpus.positives[0]
    first, output = released_ids(node, fact, "read-1")
    again, _ = released_ids(node, fact, "read-2")
    assert output["view_id"] == "canonical.message_disclosure.v2"
    assert first == again and all(OPAQUE.fullmatch(record_id) for record_id in first)
    node.activate(v3_policy("grant-2")(node.corpus.resolver.binding))
    other, _ = released_ids(node, fact, "read-3", grant_id="grant-2")
    assert other != first


def test_O1_equal_to_the_search_streams_derivation(node):
    fact = node.corpus.positives[1]
    ids, _ = released_ids(node, fact, "read-1")
    key = RecordKeys(release.record_keys_root(node.corpus.path)).get("grant-1", create=False)
    assert ids == [opaque_record_id(key, grant_id="grant-1", table="conversation_messages", source_id=pc.SOURCE,
                                    dataset_id=pc.DATASET, record_id=node.corpus.messages[fact])]


def test_O1_the_key_root_is_the_search_streams_root():
    try:
        from topos.permissions_v2.search_index import root_for
    except ImportError:
        pytest.skip("search stream not merged yet")
    assert release.record_keys_root("/n/database.db") == root_for(Path("/n/database.db"))


def test_O2_no_part_of_the_canonical_id_is_released(node):
    fact = node.corpus.positives[2]
    _ids, output = released_ids(node, fact, "read-1")
    canonical = node.corpus.messages[fact]
    wire = json.dumps({"records": [{k: v for k, v in r.items() if k != "content"} for r in output["records"]]})
    assert canonical not in wire and canonical.split(":")[1] not in wire


@pytest.mark.ordinal_ids_retired
@pytest.mark.parametrize("capability", ["permissions-beta/p2a-v1", "permissions-beta/p2a-v2"])
def test_O3_the_node_refuses_ordinal_capabilities(tmp_path, capability):
    corpus = pc.build(tmp_path / "corpus", seed=34, positives=1)
    if capability.endswith("v1"):
        from tests.permissions_v2.test_contract_and_ledger import sample_policy

        def policy(binding):
            raw = work_policy(binding)
            raw["versions"] = {"vocabulary": release.VOCABULARY, "capability": capability}
            raw["evaluator"] = sample_policy()["evaluator"]
            return raw
    else:
        policy = work_policy
    node = Node(corpus, tmp_path, policy=policy)
    released, reason = node.read(corpus.positives[0], request_id="read-1")
    assert released is None and reason == "capability_retired"


def test_O4_deleting_the_key_changes_every_id(node):
    fact = node.corpus.positives[0]
    before, _ = released_ids(node, fact, "read-1")
    RecordKeys(release.record_keys_root(node.corpus.path)).delete("grant-1")
    after, _ = released_ids(node, fact, "read-2")
    assert before != after and all(OPAQUE.fullmatch(record_id) for record_id in after)


@pytest.mark.parametrize("damage", ["missing_directory", "loose_key_directory", "unreadable_store"])
def test_O4_a_key_store_it_cannot_use_refuses_rather_than_falling_back(tmp_path, damage):
    """No id is better than the canonical one: every failure to reach the key refuses.

    `opaque_ids` guards its own directory (0700) and key file (0600); the durable parent's
    mode is the runtime's business, so a loose parent alone is not a refusal and is not
    claimed to be one here.
    """
    corpus = pc.build(tmp_path / "corpus", seed=35, positives=1)
    durable = corpus.path.parent / "permissions-v2"
    if damage == "loose_key_directory":
        (durable / "message-search").mkdir(mode=0o755, parents=True)
    elif damage == "unreadable_store":
        (durable / "message-search").mkdir(mode=0o700, parents=True)
        (durable / "message-search" / "keys.db").write_text("not a database")
    node = Node(corpus, tmp_path, policy=v3_policy())
    released, reason = node.read(corpus.positives[0], request_id="read-1")
    assert released is None and reason in {"record_key_unavailable", "private_directory_required",
                                           "private_file_required", "record_key_invalid"}


def test_O6_the_record_order_is_the_opaque_order_not_the_canonical_one(tmp_path):
    """Ids alone are not the whole channel: a list ordered by canonical id ranks the owner's
    records. Under p2a-v3 the order is the opaque one."""
    corpus = pc.build(tmp_path / "corpus", seed=36, positives=1)
    (corpus.path.parent / "permissions-v2").mkdir(mode=0o700)
    fact = corpus.positives[0]
    first = corpus.messages[fact]
    with sqlite3.connect(corpus.path) as conn:
        # Two more leaves on the same fact, with canonical ids that sort before and after it.
        refs = json.loads(conn.execute("SELECT source_refs_json FROM signal_objects WHERE object_id=?", (fact,)).fetchone()[0])
        for record_id, event in (("imessage:0000001", 1), ("imessage:9999999", 2)):
            pc.insert_message(conn, message_id=record_id, content=f"extra leaf {event}", event_at=pc.NOW - 99 * event)
            refs.append({"table": "conversation_messages", "record_id": record_id, "source_id": pc.SOURCE,
                         "dataset_id": pc.DATASET})
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps(refs), fact))
    with pc.owner():   # the fact changed, so it needs its review again
        snapshot = corpus.resolver.inspect_for_review(fact)
        from topos.permissions_v2.evidence import ReviewedClassification
        corpus.reviews.record_review(resolver=corpus.resolver, review_id="review-reordered", expected_snapshot=snapshot,
            classifications=[ReviewedClassification(evidence=version, domains=["work"], sensitivity="none",
                subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
                independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves],
            reviewed_at=pc.NOW - 30)
    node = Node(corpus, tmp_path, policy=v3_policy())
    canonical = sorted([first, "imessage:0000001", "imessage:9999999"])
    # The key is pinned, not left to `secrets.token_bytes`. With three records a random key
    # puts the opaque order in canonical order once every 3! = 6 runs, and the inequality
    # below then fails: measured 6 failures in 30 runs before this pin. A flaky gate on the
    # enforcement core gets re-run rather than read, so the key is fixed and the assertion
    # is exact. The guard on the line after it keeps the pin honest if the derivation moves.
    keys = RecordKeys(release.record_keys_root(corpus.path))
    with keys._db() as db:
        db.execute("INSERT INTO p2c_record_keys VALUES (?, ?)", ("grant-1", PINNED_ORDER_KEY))
    key = keys.get("grant-1", create=False)
    by_canonical = [opaque_record_id(key, grant_id="grant-1", table="conversation_messages", source_id=pc.SOURCE,
                                     dataset_id=pc.DATASET, record_id=record) for record in canonical]
    assert by_canonical != sorted(by_canonical), (
        "PINNED_ORDER_KEY no longer reorders these three records; pick another key rather than "
        "deleting this assertion, or the test below passes without testing anything")
    ids, _output = released_ids(node, fact, "read-1")
    assert len(ids) == 3 and ids == sorted(ids)
    assert by_canonical != ids, "the wire order still follows the canonical ids"
    assert sorted(by_canonical) == ids


def test_O6_this_door_admits_only_the_capabilities_whose_view_it_knows(node, monkeypatch):
    """`source_view` answers the locator view for ANY capability, because another stream's
    evaluator asks it about its own. This door must not take that default and build
    canonical ids for a capability it does not know."""
    from topos.permissions_v2.identity import ATTESTED_CONTRACT, SUBJECT_CONTRACT_BY_CAPABILITY
    other = "permissions-beta/p2c-v1"
    monkeypatch.setitem(SUBJECT_CONTRACT_BY_CAPABILITY, other, ATTESTED_CONTRACT)   # as a merge would
    assert release.source_view(other)[0] == "canonical.message_disclosure.v1"
    envelope, payload = node.issue(node.corpus.positives[0], request_id="read-1")
    forged = {**envelope.model_dump(), "capability_version": other}
    from tests.permissions_v2.test_release import dispatch
    with pytest.raises(PolicyError):
        dispatch(node.setup, type("E", (), {"model_dump": lambda self: forged})(), payload, request_id="read-1",
                 send=lambda *_: None)


def test_O5_opaque_ids_is_the_search_streams_module_byte_for_byte():
    path = Path(release.__file__).with_name("opaque_ids.py")
    blob = subprocess.run(["git", "hash-object", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    assert blob == SEARCH_STREAM_BLOB
