"""Noninterference: the same permitted set with different hidden data gives identical recipient bytes.

Twin corpora share every permitted unit byte for byte (same ids, texts, times,
reviews) and differ only in hidden data: unreviewed messages full of private and
work words, their embeddings, and reviewed-but-withheld units of every kind. Every
query, including the hidden units' canaries and the private vocabulary, must give
the same response on both, and no hidden byte may appear in either.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import twin

POSITIVES = {"clean_positive_C": 6, "p2b_state_work": 2}
QUERIES = (["roadmap", "deploy invoice", "sprint review budget", "release latency", "oncologist", "mortgage rent",
            "diagnosis clinic medication", "therapist", "team notes", "hidden", "zq"]
           + list(mc.PRIVATE_WORDS) + list(mc.WORK_WORDS))


def responses(node, queries=QUERIES):
    out = []
    for query in queries:
        for k in (1, 3, 25):
            output, refused = node.search_request(query, k=k)
            out.append((query, k, refused, json.dumps(output, sort_keys=True)))
        output, refused = node.search_request(query, k=10, window={"after": mc.NOW - 30 * 86_400, "before": mc.NOW})
        out.append((query, "window", refused, json.dumps(output, sort_keys=True)))
    return out


@pytest.mark.parametrize("seed", [3, 11, 29])
@pytest.mark.parametrize("hidden", [
    {"hidden_messages": 300},
    {"extra_withheld": {kind: 3 for kind in mc.WITHHELD_KINDS}},
    {"hidden_messages": 1_000, "extra_withheld": {kind: 6 for kind in mc.WITHHELD_KINDS}},
], ids=["unreviewed-300", "withheld-every-kind", "both-large"])
def test_twin_corpora_give_byte_identical_answers(tmp_path, seed, hidden):
    base = twin(tmp_path, "a", seed=seed, counts=POSITIVES)
    other = twin(tmp_path, "b", seed=seed, counts=POSITIVES, **hidden)
    queries = QUERIES + [unit.canary for unit in other.corpus.units if unit.canary][:20]
    left, right = responses(base, queries), responses(other, queries)
    assert left == right
    assert any(json.loads(body)["records"] for *_, body in left if body != "null")
    hidden_text = [unit.text for unit in other.corpus.units if not unit.search_release]
    for *_, body in right:
        for canary in other.corpus.canaries:
            assert canary not in body
        for text in hidden_text:
            assert text not in body
