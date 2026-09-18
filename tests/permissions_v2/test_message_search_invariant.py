"""p2c-v1's invariant: discovery is a subset of access, decided by the locator door's own function.

For every record a search returns: the unit is gold-release for search, its fact
is released by the real p2a-v2 door under a grant with the same rules and the
returned content is byte-identical to that release, no canary of a withheld
unit appears anywhere in any response, and the record lies inside the window.
The property runs over seeded corpora carrying every campaign kind and every
floor/state kind. P2C_SEEDS widens it (the recorded run used 500).
"""
from __future__ import annotations

import json
import os

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner

SEEDS = int(os.environ.get("P2C_SEEDS", "40"))


def build_node(tmp_path, seed, **kwargs):
    corpus = mc.build(tmp_path / "corpus", seed=seed, **kwargs)
    embed_corpus(corpus, skip_every=5)
    node = Node(corpus, tmp_path)
    node.rebuild()
    return node


def released_by_locator(node):
    """message_id -> content, as the real locator door releases it, for every unit."""
    released = {}
    for unit in node.corpus.units:
        output = node.locator_read(unit.fact_id)
        if output is not None:
            for record in output["records"]:
                released[(record["source_id"], record["record_id"])] = record["content"]
    return released


def test_the_generator_gold_matches_the_real_locator_door(tmp_path):
    node = build_node(tmp_path, seed=7, counts={name: 2 for name in mc.KINDS})
    released = released_by_locator(node)
    for unit in node.corpus.units:
        assert ((unit.source_id, unit.message_id) in released) == unit.p2a_release, unit.kind


@pytest.mark.parametrize("seed", range(SEEDS))
def test_every_searched_record_is_released_by_the_locator_door_for_its_fact(tmp_path, seed):
    node = build_node(tmp_path, seed=1000 + seed)
    released = released_by_locator(node)
    by_content = {}
    for (source, message), content in released.items():
        by_content.setdefault(content, []).append((source, message))
    seen_positive = set()
    for query in node.corpus.queries:
        for k in (1, 5, 25):
            output, refused = node.search_request(query, k=k)
            assert refused is None
            body = json.dumps(output)
            for canary in node.corpus.canaries:
                assert canary not in body
            assert len(output["records"]) <= k
            for record in output["records"]:
                [(source, message)] = by_content[record["content"]]
                unit = node.corpus.unit(message)
                assert unit.search_release, unit.kind
                assert released[(source, message)] == record["content"]
                assert record["source_id"] == source and record["canonical_table"] == "conversation_messages"
                assert mc.NOW - mc.WINDOW_SECONDS <= record["event_at"] <= mc.NOW
                seen_positive.add(message)
    # Not vacuous: search finds positives.
    positives = {unit.message_id for unit in node.corpus.units if unit.search_release}
    assert positives and seen_positive and seen_positive <= positives
