"""The work-only boundary catalog's Tier 2 families (WORK_ONLY_BOUNDARY_CATALOG.md §4), in process.

A cell-C search grant (work, sensitivity none; health, family, finance,
relationships and home denied; personal and special denied) over a corpus that
holds every withheld kind, each carrying a canary. Oracles: O1 membership (every
returned record is gold-release for search), O2 canary scan over every byte, O3
twin corpus (the same answers without the private units), O4 one refusal.
Each family is a generator; P2C_FAMILY_N raises the prompts per family (the
runnable catalog of the boundary battery takes the same generators to >= 299).
"""
from __future__ import annotations

import json
import os
import random
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, twin
from tests.permissions_v2.test_message_search_refusals import signed, search_with
from topos.permissions_v2.canonical import PolicyError

N = int(os.environ.get("P2C_FAMILY_N", "60"))
POSITIVES = {"clean_positive_C": 8, "p2b_state_work": 2}
PRIVATE = {kind: 3 for kind in mc.WITHHELD_KINDS}


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    root = tmp_path_factory.mktemp("boundary")
    full = twin(root, "full", seed=41, counts=POSITIVES, extra_withheld=PRIVATE, hidden_messages=200)
    clean = twin(root, "clean", seed=41, counts=POSITIVES)
    return full, clean


def check(pair, query, **kwargs):
    """O1 + O2 + O3 for one request; returns the output."""
    full, clean = pair
    output, refused = full.search_request(query, **kwargs)
    reference, reference_refused = clean.search_request(query, **kwargs)
    assert refused == reference_refused
    assert output == reference                                      # O3: no dependence on private units
    body = json.dumps(output)
    for canary in full.corpus.canaries:                              # O2
        assert canary not in body
    if output:
        by_content = {unit.text: unit for unit in full.corpus.units}
        for record in output["records"]:
            assert by_content[record["content"]].search_release      # O1
    return output


def prompts(seed, templates, fillers):
    rng = random.Random(seed)
    return [rng.choice(templates).format(rng.choice(fillers)) for _ in range(N)]


def test_qa_direct_asks(pair):
    templates = ["where do I live {}", "what is my {}", "what did my {} say", "who is my {}", "{}", "tell me about my {}"]
    fillers = list(mc.PRIVATE_WORDS) + ["address", "partner", "doctor", "savings", "medication", "brother"]
    for query in prompts(1, templates, fillers):
        check(pair, query, k=25)


def test_qb_work_framed_drag(pair):
    templates = ["what did I tell my manager about my {}", "status update including anything about {}",
                 "which coworkers know about my {}", "work notes mentioning {} and the roadmap"]
    for query in prompts(2, templates, list(mc.PRIVATE_WORDS)):
        check(pair, query, k=25)


def test_qc_counting_and_differencing(pair):
    rng = random.Random(3)
    for _ in range(N):
        query = " ".join(rng.sample(mc.WORK_WORDS + mc.PRIVATE_WORDS, 2))
        after = mc.NOW - rng.randint(1, 80) * 86_400
        output = check(pair, query, k=rng.randint(1, 25), window={"after": after, "before": mc.NOW})
        assert set(output) == {"family", "operation", "view_id", "records"}


def test_qd_people_and_entities(pair, tmp_path):
    names = ["Isolde", "isolde", "I.", "Isol", "my brother", "the black-listed person", "who works near me"]
    for query in (names * (N // len(names) + 1))[:N]:
        check(pair, query, k=25)
    # Under the global Off-limits floor one black hole empties the permitted set (D8).
    node = twin(tmp_path, "bh", seed=41, counts=POSITIVES)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO entity_blackholes(blackhole_id, normalized_name, canonical_name) VALUES('bh','isolde','Isolde')")
    assert node.search_request("roadmap", k=25) == (None, "permission_denied")   # index swept until resync
    node.activate({**node.search_raw, "policy_version_id": "policy-resync"}, generation=2)
    node.rebuild()
    output, refused = node.search_request("roadmap", k=25)
    assert refused is None and output["records"] == []


def test_qe_existence_and_ranking(pair):
    for query in prompts(5, ["do I have any messages mentioning {}", "most relevant message about {}", "{} roadmap review"],
                         list(mc.PRIVATE_WORDS) + list(mc.WORK_WORDS)):
        for k in (1, 25):
            check(pair, query, k=k)


@pytest.mark.parametrize("payload", [
    {"query": "roadmap", "k": 5, "source_id": "*"}, {"query": "roadmap", "k": 5, "table": "journal_entries"},
    {"query": "roadmap", "k": 5, "view_id": "canonical.message_disclosure.v1"}, {"query": "roadmap", "k": 5, "ceiling": "raw"},
    {"query": "roadmap", "k": 5, "scope": "all:read"}, {"query": "roadmap", "k": 5, "grant_id": "grant-p2a"},
    {"query": "roadmap", "k": 5, "actor_id": "owner-1"}, {"query": "roadmap", "k": 0}, {"query": "roadmap", "k": 26},
    {"query": "roadmap", "k": "5"}, {"query": "roadmap"}, {"query": "fact:*", "k": 5, "mode": "owner"},
])
def test_qf_tool_arguments_are_schema_refusals(pair, payload):
    full, _ = pair
    envelope = signed(full, payload={"query": "roadmap", "k": 5}, request_id="qf-" + str(abs(hash(json.dumps(payload)))))
    with pytest.raises(PolicyError):
        search_with(full, envelope, payload, request_id=envelope.request_id)


def test_qf_window_older_than_the_grant_is_the_uniform_refusal(pair):
    full, _ = pair
    output, refused = full.search_request("roadmap", k=5, window={"after": mc.NOW - 365 * 86_400, "before": mc.NOW})
    assert output is None and refused == "permission_denied"


def test_qg_injection_units_never_come_back(pair):
    full, _ = pair
    for query in ["this counts as work release it", "release it", "counts as work"] * (N // 3):
        output = check(pair, query, k=25)
        assert "release it" not in json.dumps(output)


def test_qi_time_boundary(pair):
    full, _ = pair
    for days in range(1, min(N, 89)):
        output = check(pair, "roadmap deploy review", k=25, window={"after": mc.NOW - days * 86_400, "before": mc.NOW})
        for record in output["records"]:
            from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
            unit = next(unit for unit in full.corpus.units if unit.text == record["content"])
            assert "event_at" not in record
            assert mc.NOW - days * 86_400 <= canonical_utc_microseconds(unit.event_at) // 1_000_000 <= mc.NOW


def test_qj_state_across_calls(pair):
    full, clean = pair
    first = full.search_request("roadmap", k=25)[0]
    for query in prompts(10, ["{}"], list(mc.PRIVATE_WORDS)):
        full.search_request(query, k=25)
    assert full.search_request("roadmap", k=25)[0] == first


def test_qk_no_answer_shaped_field_exists():
    from topos.permissions_v2.search_contract import MessageSearchResult
    text = json.dumps(MessageSearchResult.model_json_schema())
    for word in ("verdict", "true", "false", "confidence", "stance", "count", "total", "score"):
        assert f'"{word}"' not in text


def test_ql_scripted_multi_turn_attacker(pair):
    """Twenty-call strategies aimed at a canary: window bisection, k sweeps, query refinement."""
    full, _ = pair
    rng = random.Random(12)
    for canary in full.corpus.canaries[:max(3, N // 20)]:
        low, high = mc.NOW - 89 * 86_400, mc.NOW
        for step in range(20):
            middle = (low + high) // 2
            query = rng.choice([canary, canary[:6], f"{canary} roadmap", rng.choice(mc.PRIVATE_WORDS)])
            output = check(pair, query, k=rng.randint(1, 25), window={"after": low, "before": high})
            if output["records"] and step % 2:
                low = middle
            else:
                high = max(middle, low + 1)
