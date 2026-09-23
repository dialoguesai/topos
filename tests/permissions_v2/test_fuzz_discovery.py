"""Fuzz lane, part 7: discovery is a subset of access, on generated corpora (design §6.2-6.3).

Each example builds a corpus with a drawn mix of every unit kind, a p2c-v1 search grant and
the p2a-v3 locator grant over the same cell-C rules -- the one locator door a recipient can
reach after the bookkeeping merge -- then asks the search door drawn questions:

D1  Every record any search returns is, byte for byte, content the p2a-v3 locator door
    releases for that record's fact. A fact the locator refuses (any floor, any deny, the
    disclosure budget) contributes nothing to any search answer.
D2  No canary of a withheld unit, and no hidden message, ever appears in a search answer.
D3  A search answer is bounded by k and the grant's window, and its shape is closed.
D4  A record flagged NSFW at ingest never reaches a search answer, even where the locator door
    would release it (the battery's `search_nsfw_ignored` survived D1-D3: the locator releases
    such a record, so D1's oracle admitted it, and the drawn queries never met one).
"""
from __future__ import annotations

import itertools

import pytest

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from tests.permissions_v2 import fuzz_support as fz  # noqa: E402
from tests.permissions_v2 import message_search_corpus as mc  # noqa: E402
from tests.permissions_v2.message_search_harness import Node, embed_corpus  # noqa: E402
from tests.permissions_v2.test_nightA_discovery_subset_access import locator_read_v3, p2a_v3_policy  # noqa: E402

pytestmark = [pytest.mark.fuzz, pytest.mark.ordinal_ids_retired]
DOOR = settings(max_examples=fz.examples("door"), deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large])
_counter = itertools.count()
TOKENS = mc.WORK_WORDS + mc.PRIVATE_WORDS + mc.FILLER


def build(tmp_path, seed, counts, hidden):
    root = tmp_path / f"node-{next(_counter)}"
    corpus = mc.build(root / "corpus", seed=seed, counts=counts, hidden_messages=hidden)
    embed_corpus(corpus)
    node = Node(corpus, root)
    node.p2a_v3_raw = p2a_v3_policy()
    node.activate(node.p2a_v3_raw)
    node.rebuild()
    return corpus, node


def access(corpus, node) -> dict:
    """content -> fact id for everything the shipping locator door releases."""
    released = {}
    for unit in corpus.units:
        output, _reason = locator_read_v3(node, unit.fact_id)
        for record in (output or {}).get("records", []):
            released[record["content"]] = unit.fact_id
    return released


def counts_strategy():
    return st.fixed_dictionaries({name: st.integers(0, 2) for name in mc.KINDS}).filter(lambda c: sum(c.values()) > 0)


def queries(corpus):
    return st.one_of(st.lists(st.sampled_from(TOKENS), min_size=1, max_size=3).map(" ".join),
                     st.sampled_from(corpus.queries), st.sampled_from(corpus.canaries or ["zqnone"]),
                     st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=20))


@DOOR
@given(st.integers(1, 2**31 - 1), counts_strategy(), st.integers(0, 3), st.data())
def test_D1_D2_D3_search_never_returns_what_the_locator_door_refuses(tmp_path, seed, counts, hidden, data):
    corpus, node = build(tmp_path, seed, counts, hidden)
    released = access(corpus, node)
    hidden_texts = set()
    import sqlite3
    with sqlite3.connect(corpus.path) as conn:
        for (content,) in conn.execute("SELECT content FROM conversation_messages WHERE message_id LIKE 'imessage:2%'"):
            hidden_texts.add(content)
    for _ in range(data.draw(st.integers(1, 4))):
        query = data.draw(queries(corpus))
        k = data.draw(st.integers(1, 25))
        output, refused = node.search_request(query, k=k)
        assert refused is None, refused
        records = output["records"]
        assert len(records) <= k and output["view_id"] == "canonical.message_search.v1"
        for record in records:
            assert set(record) == {"record_id", "source_id", "canonical_table", "content"}, record.keys()
            assert record["content"] in released, ("search released content no p2a-v3 locator read releases",
                                                    record["content"][:60])
            assert record["content"] not in hidden_texts
            for canary in corpus.canaries:
                assert canary not in record["content"]


@DOOR
@given(st.integers(1, 2**31 - 1), st.integers(1, 3))
def test_D4_a_record_flagged_nsfw_at_ingest_never_reaches_a_search_answer(tmp_path, seed, flagged):
    counts = {name: 0 for name in mc.KINDS}
    counts["nsfw_flagged"] = flagged
    corpus, node = build(tmp_path, seed, counts, 0)
    flagged_units = [unit for unit in corpus.units if unit.kind == "nsfw_flagged"]
    assert len(flagged_units) == flagged
    for unit in flagged_units:
        for query in ([unit.canary] if unit.canary else []) + [" ".join(unit.text.split()[:3])]:
            output, refused = node.search_request(query, k=25)
            assert refused is None, refused
            for record in output["records"]:
                assert record["content"] != unit.text and (not unit.canary or unit.canary not in record["content"])
