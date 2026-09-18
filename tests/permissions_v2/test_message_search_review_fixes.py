"""Guards added after the adversarial review of the p2c-v1 build (18 Sep)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner
from tests.permissions_v2.test_message_search_refusals import PAYLOAD, Socket, relay_message, signed
from topos.features.facts.store import FactStore
from topos.permissions_v2 import search_transport
from topos.permissions_v2.evidence import ReviewedClassification
from topos.permissions_v2.search_index import index_path


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=61, counts={"clean_positive_C": 3})
    embed_corpus(corpus)
    return Node(corpus, tmp_path)


def add_multi_leaf_fact(node, contents):
    """One reviewed work fact over several messages, written as the snapshot lane would."""
    refs = []
    with sqlite3.connect(node.corpus.path) as conn:
        for number, content in enumerate(contents):
            message_id = f"imessage:{700_000 + number}"
            mc.insert_message(conn, message_id=message_id, source_id=mc.SOURCE, content=content,
                              event_at="2027-01-10T08:00:00Z")
            refs.append({"table": "conversation_messages", "dataset_id": mc.DATASET, "source_id": mc.SOURCE,
                         "record_id": message_id})
        fact = FactStore(conn).assert_fact(subject_entity_id=mc.OWNER_ENTITY, predicate="works_on",
            object_value="multi leaf unit", disclosure="scoped", source_refs=refs, asserted_by="owner")
    with owner():
        snapshot = node.corpus.resolver.inspect_for_review(fact["object_id"])
        node.corpus.reviews.record_review(resolver=node.corpus.resolver, review_id="multi", expected_snapshot=snapshot,
            classifications=[ReviewedClassification(evidence=version, domains=["work"], sensitivity="none",
                subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
                independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves],
            reviewed_at=mc.NOW - 5)
    return fact["object_id"]


def test_search_never_releases_a_leaf_of_a_fact_the_locator_door_cannot_disclose(node):
    # Three permitted leaves of ~90k characters each: every leaf fits a search record, the
    # whole fact is over the locator door's 256,000-byte budget, so that door refuses it.
    contents = [f"quasarword{number} " + "x" * 90_000 for number in range(3)]
    fact_id = add_multi_leaf_fact(node, contents)
    node.rebuild()
    assert node.locator_read(fact_id) is None
    for number in range(3):
        output, refused = node.search_request(f"quasarword{number}", k=25)
        assert refused is None
        assert all(not record["content"].startswith("quasarword") for record in output["records"])


def test_a_small_multi_leaf_fact_is_searchable_leaf_by_leaf(node):
    fact_id = add_multi_leaf_fact(node, ["pulsarword alpha deploy", "pulsarword beta roadmap"])
    node.rebuild()
    assert node.locator_read(fact_id) is not None
    output, _ = node.search_request("pulsarword", k=25)
    assert {record["content"] for record in output["records"]} == {"pulsarword alpha deploy", "pulsarword beta roadmap"}


def test_a_refusal_after_admission_still_leaves_a_deny_receipt(node):
    node.rebuild()
    (node.index.root / index_path(node.index.root, "grant-search").name).unlink()
    output, refused = node.search_request("roadmap", k=5, request_id="after-admission")
    assert output is None
    with sqlite3.connect(node.ledger.path) as conn:
        receipt = conn.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id='after-admission'").fetchone()
        status = conn.execute("SELECT status FROM p2a_requests WHERE request_id='after-admission'").fetchone()[0]
    assert status == "checkpointed" and json.loads(receipt[0])["verdict"] == "deny"


def test_a_corrupt_index_is_a_refusal_not_an_exception(node):
    node.rebuild()
    with sqlite3.connect(index_path(node.index.root, "grant-search")) as conn:
        conn.execute("UPDATE members SET terms_json='{not json'")
    assert node.search_request("roadmap", k=5) == (None, "permission_denied")


def test_a_request_sweep_error_refuses_without_emptying_other_grants(node, monkeypatch):
    node.activate(mc.search_policy(grant="grant-other", actor="actor-9", client="client-9"))
    node.rebuild()
    files = sorted(node.index.root.glob("grant-*.db"))
    assert len(files) == 2
    monkeypatch.setattr(type(node.index), "_current", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("transient")))
    assert node.search_request("roadmap", k=5) == (None, "permission_denied")
    assert sorted(node.index.root.glob("grant-*.db")) == files
    # The owner-side sweep still fails closed.
    node.index.sweep(now=mc.NOW)
    assert not list(node.index.root.glob("grant-*.db"))


def test_rebuilding_a_non_search_grant_never_rotates_its_record_key(node):
    key = node.index.keys.get("grant-p2a", create=True)
    with owner():
        assert node.index.rebuild("grant-p2a", now=mc.NOW)["state"] == "removed"
    assert node.index.keys.get("grant-p2a", create=False) == key


@pytest.mark.asyncio
async def test_a_revoke_between_checkpoint_and_send_stops_the_send(node, monkeypatch):
    node.rebuild()
    message = relay_message(node, signed(node), PAYLOAD, monkeypatch)
    real = node.search.dispatch

    def dispatch_then_revoke(**kwargs):
        answer = real(**kwargs)
        with owner():
            node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="late-revoke")
        return answer
    monkeypatch.setattr(node.search, "dispatch", dispatch_then_revoke)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    assert [json.loads(value)["status"] for value in socket.sent] == ["error"]


@pytest.mark.parametrize("labels", [(["work", "health"], "none"), (["work"], "special"), (["hobbies"], "none")],
                         ids=["now_health", "now_special", "no_longer_work"])
def test_a_re_review_after_the_build_is_enforced_by_the_release_recheck_alone(node, labels):
    """The owner re-labels a permitted fact after the index was built and no rebuild runs.

    The review store is not a canonical row, so no fingerprint sees it and the index still
    offers the record: only the release re-check, re-deciding with p2a's own function under
    the new review, stands between it and the recipient.
    """
    node.rebuild()
    target = next(unit for unit in node.corpus.units if unit.search_release)
    words = " ".join(target.text.split()[:3])
    output, _ = node.search_request(words, k=25)
    assert target.text in {record["content"] for record in output["records"]}
    domains, sensitivity = labels
    with owner():
        snapshot = node.corpus.resolver.inspect_for_review(target.fact_id)
        node.corpus.reviews.record_review(resolver=node.corpus.resolver, review_id="relabel", expected_snapshot=snapshot,
            classifications=[ReviewedClassification(evidence=version, domains=domains, sensitivity=sensitivity,
                subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
                independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves],
            reviewed_at=mc.NOW - 1)
    assert node.locator_read(target.fact_id) is None
    output, refused = node.search_request(words, k=25)
    assert refused is None and target.text not in {record["content"] for record in output["records"]}


@pytest.mark.asyncio
async def test_a_re_policy_between_checkpoint_and_send_stops_the_send(node, monkeypatch):
    """Not revoked, re-policied: the authority read before the send still succeeds, so only the
    comparison with the checkpointed authority can refuse it."""
    node.rebuild()
    message = relay_message(node, signed(node), PAYLOAD, monkeypatch)
    real = node.search.dispatch

    def dispatch_then_repolicy(**kwargs):
        answer = real(**kwargs)
        node.activate({**node.search_raw, "policy_version_id": "policy-late"}, generation=2)
        return answer
    monkeypatch.setattr(node.search, "dispatch", dispatch_then_repolicy)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    assert [json.loads(value)["status"] for value in socket.sent] == ["error"]


def test_the_window_bound_comes_from_the_gated_read_not_the_ranking(node):
    """A record ranked in-window that ages out before the gated re-check is not released."""
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    oldest = min((unit for unit in node.corpus.units if unit.search_release), key=lambda unit: unit.event_at)
    age = mc.NOW - canonical_utc_microseconds(oldest.event_at) // 1_000_000
    # A grant whose rolling window ends 30 s beyond the oldest record.
    node.activate({**mc.search_policy(max_age_seconds=age + 30), "policy_version_id": "policy-tight"}, generation=2)
    node.rebuild()
    query = " ".join(oldest.text.split()[:4])
    assert oldest.text in {record["content"] for record in node.search_request(query, k=25)[0]["records"]}
    # Ranking sees it inside the window; 60 s pass before the gated read.
    node.search.observe = lambda name, _v: node.now.__setitem__(0, node.now[0] + 60) if name == "rank" else None
    output, refused = node.search_request(query, k=25)
    assert refused is None and oldest.text not in {record["content"] for record in output["records"]}


def test_one_failed_rebuild_purges_that_grant_and_the_others_still_build(node, monkeypatch):
    node.activate(mc.search_policy(grant="grant-other", actor="actor-9", client="client-9"))
    node.rebuild()
    real = type(node.index)._rebuild

    def failing(self, grant_id, *, now=None):
        if grant_id == "grant-search":
            raise RuntimeError("storage hiccup")
        return real(self, grant_id, now=now)
    monkeypatch.setattr(type(node.index), "_rebuild", failing)
    states = node.rebuild()
    assert states == {"grant-search": "failed", "grant-other": "ready"}
    assert not index_path(node.index.root, "grant-search").exists()
    assert index_path(node.index.root, "grant-other").exists()
