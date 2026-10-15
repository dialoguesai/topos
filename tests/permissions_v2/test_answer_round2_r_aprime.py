"""Lane GD round 2 (15 Oct 2026, grantee density): R, answers mode's own vector floor.

R. The vector lane drops a member whose best cosine is under `search_lanes.VECTOR_FLOOR` (0.25). With the node's
default embedding model a broad question ("What work and projects is the owner working on?") scores low against short
first-person notes: on the grantee density fixture (synthetic owner) 1 of 22 supporting records cleared 0.25, 6
cleared 0.20, 11 cleared 0.15 and 16 cleared 0.10, so the writer saw one record. `ANSWER_VECTOR_FLOOR` (hundredths,
25 by default) sets the floor for `retrieve_for_answer` alone; named search, batch and dispatch keep 0.25. It moves
which already-permitted members are ranked into the answer's candidates, never what a grant permits: each candidate
still passes the same walk. The default is 1.5.9 byte for byte. Invented names and text only.
"""
from __future__ import annotations

import math
import random

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import search_lanes, search_release, switches
from topos.permissions_v2.search_index import LoadedIndex, Member
from topos.permissions_v2.search_lanes import VECTOR_FLOOR
from topos.permissions_v2.search_release import answer_vector_floor

P = "TOPOS_PERMISSIONS_V2_"
FLOOR = P + "ANSWER_VECTOR_FLOOR"
DAY_US = 86_400 * 1_000_000
WIDE = dict(lower_us=0, upper_us=400 * DAY_US)


@pytest.fixture(autouse=True)
def clean_switches(monkeypatch):
    for name in ("ANSWER_VECTOR_FLOOR",):
        monkeypatch.delenv(P + name, raising=False)


def _member(opaque, text, day):
    terms = {}
    for word in text.lower().split():
        terms[word] = terms.get(word, 0) + 1
    return Member(opaque_id=opaque, event_at_us=day * DAY_US, doc_len=len(text.split()), terms=terms, sealed=b"")


def _at(cosine):
    """A unit chunk vector at exactly this cosine to the query [1, 0]."""
    return [cosine, math.sqrt(1 - cosine * cosine)]


QUERY = [1.0, 0.0]
#: The fixture's shape: one note clears 0.25, the next sit between 0.15 and 0.25, one under 0.10. No word is shared
#: with the question, so only the vector lane can rank them.
MEMBERS = {"clear": 0.31, "mid-a": 0.22, "mid-b": 0.18, "mid-c": 0.16, "low": 0.08}


def _index():
    members = [_member(opaque, f"note {number}", 100 + number) for number, opaque in enumerate(MEMBERS)]
    return LoadedIndex(basis={}, model="invented-model", dims=2, members=tuple(members),
                       vectors={opaque: [_at(cosine)] for opaque, cosine in MEMBERS.items()})


def _vector_1_5_9(index, query_vector):
    """`search_lanes.vector` as 1.5.9 shipped it, line for line: the reference the default must equal."""
    if not query_vector or not index.vectors:
        return []
    norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
    unit = [value / norm for value in query_vector]
    scored = []
    for member in index.members:
        best = None
        for chunk in index.vectors.get(member.opaque_id, ()):
            if len(chunk) != len(unit):
                continue
            chunk_norm = math.sqrt(sum(value * value for value in chunk)) or 1.0
            similarity = sum(a * b for a, b in zip(unit, chunk)) / chunk_norm
            best = similarity if best is None else max(best, similarity)
        if best is not None and best >= 0.25:
            scored.append((member.opaque_id, best))
    return scored


# --- the floor's value -----------------------------------------------------------------------------------------------

def test_the_default_is_1_5_9s_floor_exactly():
    assert switches.number(switches.ANSWER_VECTOR_FLOOR) == 25
    assert answer_vector_floor() == VECTOR_FLOOR == 0.25


@pytest.mark.parametrize("raw, floor", [("15", 0.15), ("0", 0.0), ("100", 1.0), ("150", 1.0), ("100000", 1.0),
                                        ("25", 0.25), ("-5", 0.25), ("0.15", 0.25), ("", 0.25), ("fifteen", 0.25)])
def test_the_floor_is_clamped_to_0_to_100_hundredths(raw, floor, monkeypatch):
    # Digits only; anything else is no override (the parser's rule for numbers), so it reads as the default.
    monkeypatch.setenv(FLOOR, raw)
    assert answer_vector_floor() == floor
    assert answer_vector_floor({FLOOR: raw}) == floor


# --- the lane --------------------------------------------------------------------------------------------------------

def test_the_default_floor_ranks_exactly_as_1_5_9():
    rng = random.Random(178)
    for _ in range(40):
        members, vectors = [], {}
        for number in range(rng.randint(1, 30)):
            opaque = f"m{number:02d}"
            members.append(_member(opaque, " ".join(rng.choice(("seed", "fair", "rota", "kiln", "jug", "hike"))
                                                    for _ in range(rng.randint(1, 6))), rng.randint(1, 300)))
            vectors[opaque] = [[rng.uniform(-1, 1) for _ in range(8)] for _ in range(rng.randint(1, 3))]
        index = LoadedIndex(basis={}, model="invented-model", dims=8, members=tuple(members), vectors=vectors)
        query_vector = [rng.uniform(-1, 1) for _ in range(8)]
        query = rng.choice(("seed fair", "kiln", "nothing shared", "rota hike jug"))
        assert search_lanes.vector(index, query_vector) == _vector_1_5_9(index, query_vector)
        assert (search_lanes.vector(index, query_vector, vector_floor=answer_vector_floor())
                == _vector_1_5_9(index, query_vector))
        for spare, fill in ((0, False), (5, False), (0, True)):
            arguments = dict(limit=rng.randint(1, 12), spare=spare, fill=fill, precision="day", **WIDE)
            assert (search_lanes.rank(index, query, query_vector, **arguments)
                    == search_lanes.rank(index, query, query_vector, vector_floor=answer_vector_floor(), **arguments))


def test_a_floor_of_15_admits_a_member_between_0_15_and_0_25_and_25_does_not():
    index = _index()
    assert [opaque for opaque, _ in search_lanes.vector(index, QUERY)] == ["clear"]
    assert [opaque for opaque, _ in search_lanes.vector(index, QUERY, vector_floor=0.25)] == ["clear"]
    assert ([opaque for opaque, _ in search_lanes.vector(index, QUERY, vector_floor=0.15)]
            == ["clear", "mid-a", "mid-b", "mid-c"])
    assert search_lanes.rank(index, "work projects", QUERY, limit=10, **WIDE) == ["clear"]
    assert (search_lanes.rank(index, "work projects", QUERY, limit=10, vector_floor=0.15, **WIDE)
            == ["clear", "mid-a", "mid-b", "mid-c"])     # by cosine; the one under 0.10 stays out
    # A floor ranks only members already in the index and window: none outside the window enters at any floor.
    narrow = dict(lower_us=101 * DAY_US, upper_us=102 * DAY_US)
    assert search_lanes.rank(index, "work projects", QUERY, limit=10, vector_floor=0.0, **narrow) == ["mid-a", "mid-b"]


def test_the_switch_reaches_the_lane_through_answer_vector_floor(monkeypatch):
    index = _index()
    monkeypatch.setenv(FLOOR, "15")
    assert (search_lanes.rank(index, "work projects", QUERY, limit=10, vector_floor=answer_vector_floor(), **WIDE)
            == ["clear", "mid-a", "mid-b", "mid-c"])
    # The lane itself never reads the switch: without the keyword it is 0.25 whatever the environment says.
    assert search_lanes.rank(index, "work projects", QUERY, limit=10, **WIDE) == ["clear"]


# --- only the answer retrieval reads it ------------------------------------------------------------------------------

def _spy(monkeypatch):
    """Record the `vector_floor` each `rank` call of `search_release` passes, and each read of the switch."""
    floors, reads = [], []
    real_rank, real_floor = search_release.rank, search_release.answer_vector_floor

    def rank(*args, **kwargs):
        floors.append(kwargs.get("vector_floor", "not passed"))
        return real_rank(*args, **kwargs)

    def floor(*args, **kwargs):
        reads.append(1)
        return real_floor(*args, **kwargs)

    monkeypatch.setattr(search_release, "rank", rank)
    monkeypatch.setattr(search_release, "answer_vector_floor", floor)
    return floors, reads


def test_named_search_and_batch_never_read_the_answer_floor(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(FLOOR, "15")
    node, _identity = node_for(legacy, tmp_path, monkeypatch)
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "ready"
    floors, reads = _spy(monkeypatch)
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is None and len(output["records"]) == 1
    outputs, refused = node.search_batch_request(["Synthetic message", "work"], k=10)
    assert refused is None and len(outputs) == 2
    assert len(floors) == 3 and set(floors) == {"not passed"} and reads == []


def test_the_answer_retrieval_passes_the_switchs_floor_to_every_rank(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(FLOOR, "15")
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    floors, reads = _spy(monkeypatch)
    service = _service(node, "The owner is working on a synthetic task [1].")
    try:
        body = _finished(node, service, _ask(node, service, "Synthetic message"))
    finally:
        service.close()
    assert body["outcome"] == "answered"
    assert floors and set(floors) == {0.15}
    # Read once per retrieval (the answer step retrieves, then re-checks the written items without ranking).
    assert len(reads) == 1


def test_with_nothing_set_the_answer_retrieval_passes_1_5_9s_floor(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    floors, _reads = _spy(monkeypatch)
    service = _service(node, "The owner is working on a synthetic task [1].")
    try:
        body = _finished(node, service, _ask(node, service, "Synthetic message"))
    finally:
        service.close()
    assert body["outcome"] == "answered" and floors and set(floors) == {VECTOR_FLOOR}
