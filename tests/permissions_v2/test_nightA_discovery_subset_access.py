"""Night review A: discovery is a subset of access ON THE DOOR THAT SHIPS AFTER THE MERGE.

`test_message_search_invariant.py` proves the invariant against a **p2a-v2** locator
grant, and every test in this suite runs with the ordinal-id retirement lifted by
`conftest.py`. After the bookkeeping merge the node refuses p2a-v1 and p2a-v2
(`release.RETIRED_SOURCE_CAPABILITIES`), so the locator door a recipient can actually
reach is **p2a-v3**, whose view is `canonical.message_disclosure.v2`.

The two views are not the same size. p2a-v3's `record_id` is the opaque id, a fixed
66 characters ("r." + 64 hex); p2a-v2's is the canonical id, typically ~15. The
locator door's disclosure-budget check (`release.py:259`) runs over the view the
signed capability names, so for the same fact the v2 shape is up to 51 bytes per
record larger -- 5,100 bytes over a 100-leaf closure, against a 256,000-byte budget.

`search_release._locator_disclosable` (`search_release.py:64-76`) claims in its own
docstring to make "exactly the output checks the locator door makes after a permit",
but it builds the **v1** shape and measures that. So there is a band in which the
locator door refuses a fact for `disclosure_budget` and search, having measured the
smaller shape, still releases one of that fact's records.

Neither branch's own suite can see this: on `beta/p2c-search` the locator door built
the v1 shape, so the check was exact; on `beta/v2-bookkeeping-3` there is no search.
It appears only in the merge -- the same shape as the stream's own departure 4.

These tests are marked `ordinal_ids_retired`, so they run against the node's real
setting rather than the suite-wide lift.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner, recipient
from topos.features.facts.store import FactStore
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.contract import VIEW, MessageDisclosure
from topos.permissions_v2.opaque_ids import opaque_record_id
from topos.permissions_v2.registry import OpaqueMessageDisclosure
from topos.permissions_v2.release import MAX_DISCLOSURE_BYTES

V3 = "permissions-beta/p2a-v3"
# A token that appears in exactly one short leaf of the long fact, and nowhere else.
NEEDLE = "zzneedlezz"
LONG_LEAVES = 9
SHORT_TEXT = f"quarterly budget {NEEDLE} review"


def _disclosure_bytes(contents, record_ids, view, model):
    records = [{"record_id": record_id, "source_id": mc.SOURCE, "canonical_table": "conversation_messages",
                "content": content} for record_id, content in zip(record_ids, contents)]
    return len(canonical_bytes(model.parse({"family": "canonical_record", "operation": "read",
                                            "view_id": view, "records": records}).model_dump()))


def _leaf_text(number: int, words: int) -> str:
    """Unique per leaf: identical leaves trip the independent-copy floor first."""
    return " ".join(f"roadmap{number}x{word} deploy sprint" for word in range(words))


def calibrate_leaf_words(message_ids) -> int:
    """The largest leaf size whose v1 disclosure still fits the budget.

    Measured, not guessed, so the test still straddles the band if the canonical
    encoding or the budget moves. The band it lands in is exactly the defect:
    v1 fits (search's copy of the check passes) while v2 does not (the shipping
    locator door refuses).
    """
    canonical_ids = list(message_ids)
    opaque_ids = ["r." + "0" * 64] * len(canonical_ids)  # every opaque id is 66 chars

    def v1(words):
        return _disclosure_bytes([_leaf_text(n, words) for n in range(LONG_LEAVES)] + [SHORT_TEXT],
                                 canonical_ids, VIEW, MessageDisclosure)
    low, high = 1, 4_000
    while low < high:
        mid = (low + high + 1) // 2
        if v1(mid) <= MAX_DISCLOSURE_BYTES:
            low = mid
        else:
            high = mid - 1
    contents = [_leaf_text(n, low) for n in range(LONG_LEAVES)] + [SHORT_TEXT]
    assert _disclosure_bytes(contents, canonical_ids, VIEW, MessageDisclosure) <= MAX_DISCLOSURE_BYTES
    assert _disclosure_bytes(contents, opaque_ids, "canonical.message_disclosure.v2",
                             OpaqueMessageDisclosure) > MAX_DISCLOSURE_BYTES, (
        "the two views measure the same; the premise of this test no longer holds")
    return low


def p2a_v3_policy(grant: str = "grant-p2a-v3") -> dict:
    """The cell-C locator grant a recipient can still reach after the merge (plan section 17.1)."""
    raw = mc.p2a_v2_policy(grant=grant)
    raw["versions"]["capability"] = V3
    raw["evaluator"] = {"kind": "hard_rules", "version": "hard-rules/p2a-v3"}
    for rule in raw["rules"]:
        for form in rule["release"]["forms"]:
            form["view_id"] = "canonical.message_disclosure.v2"
    return raw


def add_long_fact(corpus: mc.Corpus) -> tuple[str, str]:
    """One scoped, owner-asserted fact over many long leaves plus one short leaf.

    Written with the corpus's own production-DDL writers and the real `FactStore`,
    so the closure is the shape the resolver walks in production.
    """
    event_at = mc._iso(mc.NOW - 7_200)
    rowid = 5_000_000
    message_ids = [f"imessage:{rowid + n}" for n in range(LONG_LEAVES + 1)]
    words = calibrate_leaf_words(message_ids)
    refs = []
    for number, message_id in enumerate(message_ids[:LONG_LEAVES]):
        with sqlite3.connect(corpus.path) as conn:
            mc.insert_message(conn, message_id=message_id, source_id=mc.SOURCE,
                              content=_leaf_text(number, words), event_at=event_at)
            conn.commit()
        refs.append({"table": "conversation_messages", "dataset_id": mc.DATASET, "source_id": mc.SOURCE,
                     "record_id": message_id})
    short_id = message_ids[LONG_LEAVES]
    with sqlite3.connect(corpus.path) as conn:
        mc.insert_message(conn, message_id=short_id, source_id=mc.SOURCE, content=SHORT_TEXT, event_at=event_at)
        facts = FactStore(conn)
        fact = facts.assert_fact(subject_entity_id=mc.OWNER_ENTITY, predicate="works_on",
                                 object_value="the long thread", disclosure="scoped", asserted_by="owner",
                                 source_refs=refs + [{"table": "conversation_messages", "dataset_id": mc.DATASET,
                                                      "source_id": mc.SOURCE, "record_id": short_id}])
        conn.commit()
    mc._review(corpus.resolver, corpus.reviews, fact["object_id"], domains=("work",), sensitivity="none",
               review_id="review-long-fact")
    return fact["object_id"], short_id


def build(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=4242, counts={"clean_positive_C": 2})
    fact_id, short_id = add_long_fact(corpus)
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.p2a_v3_raw = p2a_v3_policy()
    node.activate(node.p2a_v3_raw)
    node.rebuild()
    return node, fact_id, short_id


def locator_read_v3(node, fact_id):
    """The real locator door under the one source capability the merged node still releases."""
    request_id = node.next_id("v3read")
    payload = {"query": "fact:" + fact_id}
    envelope = node._envelope(node.p2a_v3_raw["binding"]["grant_id"], "permissions.v2.read", payload, request_id)
    sent = []
    try:
        with recipient("actor-1", "client-1"):
            node.locator.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id,
                                  send=lambda result, output: sent.append(output))
    except PolicyError as exc:
        return None, exc.code
    return sent[0], None


def test_the_two_locator_views_do_not_measure_the_same_number_of_bytes():
    """The premise, stated as arithmetic so the band is not taken on trust.

    A real opaque id, not a placeholder, so the 66-character length is the one the
    node derives rather than one this test asserts about itself.
    """
    ids = [f"imessage:{5_000_000 + n}" for n in range(LONG_LEAVES + 1)]
    words = calibrate_leaf_words(ids)
    contents = [_leaf_text(n, words) for n in range(LONG_LEAVES)] + [SHORT_TEXT]
    key = bytes(range(100, 132))
    opaque = [opaque_record_id(key, grant_id="g", table="conversation_messages", source_id=mc.SOURCE,
                               dataset_id=mc.DATASET, record_id=record_id) for record_id in ids]
    assert {len(value) for value in opaque} == {66}
    v1 = _disclosure_bytes(contents, ids, VIEW, MessageDisclosure)
    v2 = _disclosure_bytes(contents, opaque, "canonical.message_disclosure.v2", OpaqueMessageDisclosure)
    assert v1 <= MAX_DISCLOSURE_BYTES < v2, (v1, v2, MAX_DISCLOSURE_BYTES)


@pytest.mark.ordinal_ids_retired
def test_the_locator_door_that_ships_refuses_the_long_fact(tmp_path):
    """Not vacuous: p2a-v3 really does refuse this fact, and for the budget."""
    node, fact_id, _ = build(tmp_path)
    output, reason = locator_read_v3(node, fact_id)
    assert output is None and reason == "disclosure_budget", (reason, output)


@pytest.mark.ordinal_ids_retired
def test_search_never_returns_a_record_the_shipping_locator_door_refuses(tmp_path):
    """The invariant, against the door that ships. Fails before the fix.

    The recipient asks for a token that occurs only in the short leaf of a fact whose
    whole disclosure p2a-v3 refuses. Under discovery-subset-access it must come back
    with nothing.
    """
    node, fact_id, short_id = build(tmp_path)
    assert locator_read_v3(node, fact_id) == (None, "disclosure_budget")
    output, refused = node.search_request(NEEDLE, k=5)
    assert refused is None, refused
    returned = [record["content"] for record in output["records"]]
    assert not any(NEEDLE in content for content in returned), (
        "search released a record from a fact whose p2a-v3 locator read refuses: " + repr(returned))


@pytest.mark.ordinal_ids_retired
def test_the_property_over_every_fact_in_a_mixed_corpus(tmp_path):
    """The general property: for every record any query returns, the shipping locator
    door releases that record's fact. Runs over the full kind set plus the long fact."""
    corpus = mc.build(tmp_path / "corpus", seed=99, counts={name: 1 for name in mc.KINDS})
    long_fact, _ = add_long_fact(corpus)
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.p2a_v3_raw = p2a_v3_policy()
    node.activate(node.p2a_v3_raw)
    node.rebuild()

    released: dict[str, str] = {}
    for fact_id in [unit.fact_id for unit in corpus.units] + [long_fact]:
        output, _ = locator_read_v3(node, fact_id)
        for record in (output or {}).get("records", []):
            released[record["content"]] = fact_id

    seen = 0
    for query in list(corpus.queries) + [NEEDLE]:
        output, refused = node.search_request(query, k=25)
        assert refused is None, refused
        for record in output["records"]:
            assert record["content"] in released, (
                "search released content no p2a-v3 locator read releases: " + repr(record["content"][:80]))
            seen += 1
    assert seen, "vacuous: search returned nothing at all"
