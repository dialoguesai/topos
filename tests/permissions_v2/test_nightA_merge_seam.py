"""Night review A: the merge seam itself -- the rollback floor, the capability door
and request consumption, each exercised through the door that ships after both merges.

The bookkeeping stream proved these against the floor store and the locator door it
owned; the search stream added a second recipient door beside them. Nothing tested
the pair. Each test below drives the assertion through `MessageSearchRelease` as well
as through the p2a-v3 locator read, which is the only source capability the merged
node still releases under.
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, recipient
from tests.permissions_v2.test_nightA_discovery_subset_access import locator_read_v3, p2a_v3_policy
from topos.permissions_v2 import release
from topos.permissions_v2.canonical import PolicyError


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=515, counts={"clean_positive_C": 3})
    embed_corpus(corpus)
    built = Node(corpus, tmp_path)
    built.p2a_v3_raw = p2a_v3_policy()
    built.activate(built.p2a_v3_raw)
    built.rebuild()
    return built


def a_query(node):
    return node.corpus.queries[0]


# ---------------------------------------------------------------- (f) the rollback floor

def _roll_back(node, tmp_path):
    """Take a copy, advance the protection log past it, then put the copy back in place.

    Same inode, lower sequence: the copy-over restore C4 describes. The search index and
    the ledger are untouched by the restore, which is the point -- they are what a restored
    node would otherwise be served from.
    """
    backup = tmp_path / "older.db"
    shutil.copyfile(node.corpus.path, backup)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) "
                     "VALUES('conversation_messages','imessage:99999')")
        conn.commit()
    return backup


def test_F1_a_copy_over_restore_is_refused_at_the_next_search(node, tmp_path):
    """C4's case, through the search door.

    The envelope is issued BEFORE the restore, as the control plane would issue it, so the
    request that arrives afterwards is a well-formed one against a rolled-back file rather
    than one that fails to be built. Search must refuse it, and must not serve the answer
    out of the index, which was built against the newer file and survives the restore.
    """
    query = a_query(node)
    assert node.search_request(query, k=5)[1] is None, "vacuous: search refused before the restore"
    backup = _roll_back(node, tmp_path)
    request_id = node.next_id("rollback")
    payload = {"query": query, "k": 5}
    envelope = node._envelope(node.search_raw["binding"]["grant_id"], "permissions.v2.search", payload, request_id)
    shutil.copyfile(backup, node.corpus.path)       # in place: same inode, lower sequence
    with recipient("actor-1", "client-2"):
        with pytest.raises(PolicyError) as caught:
            node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id)
    assert caught.value.code, "refused, but with no code"


def test_F1_a_copy_over_restore_is_refused_at_the_next_locator_read(node, tmp_path):
    """The same restore against p2a-v3, so the two doors are shown to agree."""
    fact_id = node.corpus.units[0].fact_id
    assert locator_read_v3(node, fact_id)[1] is None, "vacuous: the locator refused before the restore"
    backup = _roll_back(node, tmp_path)
    request_id = node.next_id("rollback")
    payload = {"query": "fact:" + fact_id}
    envelope = node._envelope(node.p2a_v3_raw["binding"]["grant_id"], "permissions.v2.read", payload, request_id)
    shutil.copyfile(backup, node.corpus.path)
    sent = []
    with recipient("actor-1", "client-1"):
        with pytest.raises(PolicyError) as caught:
            node.locator.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id,
                                  send=lambda result, output: sent.append(output))
    assert not sent, "the locator door released from a restored older canonical file"
    assert caught.value.code, "refused, but with no code"


# ------------------------------------------------- (b) the locator door's capability set

def test_B1_the_locator_door_refuses_a_p2c_envelope(node):
    """B4's merge hazard, now the real post-merge state rather than a monkeypatched one:
    `SUBJECT_CONTRACT_BY_CAPABILITY` really does carry p2c-v1 after this merge, so the
    contract lookup no longer refuses it and something further in must."""
    from topos.permissions_v2.identity import SUBJECT_CONTRACT_BY_CAPABILITY
    assert SUBJECT_CONTRACT_BY_CAPABILITY.get("permissions-beta/p2c-v1") is not None, (
        "the merge did not land p2c-v1's subject contract; this test is not testing the merged state")
    assert release.source_view("permissions-beta/p2c-v1")[0] == "canonical.message_disclosure.v1", (
        "source_view no longer defaults, so the allowlist guards nothing")
    assert "permissions-beta/p2c-v1" not in release.SOURCE_VIEWS

    fact_id = node.corpus.units[0].fact_id
    request_id = node.next_id("cross")
    payload = {"query": "fact:" + fact_id}
    envelope = node._envelope(node.p2a_v3_raw["binding"]["grant_id"], "permissions.v2.read", payload, request_id)
    forged = {**envelope.model_dump(), "capability_version": "permissions-beta/p2c-v1"}
    with recipient("actor-1", "client-1"):
        with pytest.raises(PolicyError):
            node.locator.dispatch(envelope=forged, payload=payload, request_id=request_id, send=lambda *_: None)


def test_B1_the_capability_allowlist_itself_refuses_not_only_the_parser(node, monkeypatch):
    """The allowlist must be load-bearing, not decoration behind the envelope parser.

    A p2c-v1 envelope never gets past `parse_source_envelope` today: `SignedEnvelope`'s
    `capability_version` is a p2a-v1 literal and the two branches above it name p2a-v2 and
    p2a-v3, so a dict naming p2c-v1 fails to parse. That makes the existing guard test pass
    for the parser's reason rather than the allowlist's -- deleting the allowlist entirely
    kills no test in this suite (checked by mutation, night review A).

    It matters because `parse_source_envelope` gains a branch every time a capability is
    added -- p2a-v3 added one in this very merge. The next one that forgets to keep the
    parser closed leaves the allowlist as the only thing between a foreign capability and
    `source_view`'s deliberate locator-view default, which builds CANONICAL record ids.
    So this test reaches the allowlist directly, by standing in for that future parser.
    """
    from topos.permissions_v2 import release as release_module
    fact_id = node.corpus.units[0].fact_id
    request_id = node.next_id("allowlist")
    payload = {"query": "fact:" + fact_id}
    envelope = node._envelope(node.p2a_v3_raw["binding"]["grant_id"], "permissions.v2.read", payload, request_id)
    # A parser that accepts the foreign capability, as a later capability's branch would.
    foreign = envelope.model_copy(update={"capability_version": "permissions-beta/p2c-v1"})
    assert foreign.capability_version == "permissions-beta/p2c-v1"
    monkeypatch.setattr(release_module, "parse_source_envelope", lambda raw: foreign)
    sent = []
    with recipient("actor-1", "client-1"):
        with pytest.raises(PolicyError) as caught:
            node.locator.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id,
                                  send=lambda result, output: sent.append(output))
    assert caught.value.code == "unsupported_capability", caught.value.code
    assert not sent, "the locator door released under a capability whose view it does not know"


@pytest.mark.parametrize("capability", ["permissions-beta/p2a-v1", "permissions-beta/p2a-v2"])
@pytest.mark.ordinal_ids_retired
def test_B2_the_retired_capabilities_release_nothing(node, capability):
    """The suite-wide conftest lift is off here, so this is the node's own behaviour."""
    assert capability in release.RETIRED_SOURCE_CAPABILITIES
    fact_id = node.corpus.units[0].fact_id
    request_id = node.next_id("retired")
    payload = {"query": "fact:" + fact_id}
    envelope = node._envelope(node.p2a_raw["binding"]["grant_id"], "permissions.v2.read", payload, request_id)
    with recipient("actor-1", "client-1"):
        with pytest.raises(PolicyError):
            node.locator.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id,
                                  send=lambda *_: None)


# ------------------------------------------------- (d) the request is consumed in every branch

def test_D1_a_search_request_id_is_consumed_even_when_the_decision_refuses(node, monkeypatch):
    """Admission binds the request id before any branch, so a refusal cannot hand the
    id back. Without this a recipient could retry a refused id until a racing owner
    write made it permit, and the ledger would have no record of the first attempt."""
    from topos.permissions_v2.search_release import MessageSearchRelease
    monkeypatch.setattr(MessageSearchRelease, "_decide",
                        lambda self, *a, **k: (_ for _ in ()).throw(PolicyError("forced")))
    request_id = "search-consumed-1"
    assert node.search_request(a_query(node), k=5, request_id=request_id)[1] == "permission_denied"
    monkeypatch.undo()
    output, refused = node.search_request(a_query(node), k=5, request_id=request_id)
    assert output is None and refused == "request_replay", (refused, output)


def test_D2_a_search_request_id_is_consumed_on_a_permit(node):
    request_id = "search-consumed-2"
    output, refused = node.search_request(a_query(node), k=5, request_id=request_id)
    assert refused is None and output["records"], "vacuous: the first request did not release"
    again, reason = node.search_request(a_query(node), k=5, request_id=request_id)
    assert again is None and reason == "request_replay", (reason, again)
