"""Lane GD round 2 (15 Oct 2026, grantee density): R, answers mode's own vector floor, and A′, dense template v10.

R. The vector lane drops a member whose best cosine is under `search_lanes.VECTOR_FLOOR` (0.25). With the node's
default embedding model a broad question ("What work and projects is the owner working on?") scores low against short
first-person notes: on the grantee density fixture (synthetic owner) 1 of 22 supporting records cleared 0.25, 6
cleared 0.20, 11 cleared 0.15 and 16 cleared 0.10, so the writer saw one record. `ANSWER_VECTOR_FLOOR` (hundredths,
25 by default) sets the floor for `retrieve_for_answer` alone; named search, batch and dispatch keep 0.25. It moves
which already-permitted members are ranked into the answer's candidates, never what a grant permits: each candidate
still passes the same walk. The default is 1.5.9 byte for byte.

A′. When the items do not answer the question, v9's "seven to twelve sentences" made the 9B writer pad with unrelated
items presented as the answer (a health question answered with a newsletter and a workshop). With ANSWER_DENSE and
ANSWER_DENSE_ON_QUESTION both on, the prompt is template v10: v9 with only its length sentence replaced (only
sentences that answer the question, as many as the items support, up to twelve, one to three paragraphs). Everything
else v9 does applies to v10: the 2,048-token budget, the paragraphs, the checks, the counters and the receipt's keys.
With ANSWER_DENSE off the switch changes nothing (v8, as 1.5.9). Invented names and text only.
"""
from __future__ import annotations

import math
import random
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_dense_gda import (COUNTERS, MODES, QUESTION, RECEIPT_KEYS, RECORDS, REPLY,
    SENTENCE_KEYS, _as_the_app_reads_it, _checked, _service_answer)
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_release as ar
from topos.permissions_v2 import search_lanes, search_release, switches
from topos.permissions_v2.answer_checks import (DENSE_TEMPLATES, TEMPLATE_VERSION, TEMPLATE_VERSION_DENSE,
    TEMPLATE_VERSION_DENSE_CITE_CAP, TEMPLATE_VERSION_DENSE_ON_QUESTION, TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP)
from topos.permissions_v2.answer_generation import (LENGTH_SENTENCE, LENGTH_SENTENCE_DENSE,
    LENGTH_SENTENCE_DENSE_ON_QUESTION, SYSTEM_PROMPT, SYSTEM_PROMPT_DENSE, SYSTEM_PROMPT_DENSE_ON_QUESTION,
    SYSTEM_PROMPTS, build_prompt, writer_template)
from topos.permissions_v2.answer_protocol import FETCH, parse_answer_output
from topos.permissions_v2.search_index import LoadedIndex, Member
from topos.permissions_v2.search_lanes import VECTOR_FLOOR
from topos.permissions_v2.search_release import answer_vector_floor

P = "TOPOS_PERMISSIONS_V2_"
FLOOR = P + "ANSWER_VECTOR_FLOOR"
DENSE = P + "ANSWER_DENSE"
ON_QUESTION = P + "ANSWER_DENSE_ON_QUESTION"
DAY_US = 86_400 * 1_000_000
WIDE = dict(lower_us=0, upper_us=400 * DAY_US)


@pytest.fixture(autouse=True)
def clean_switches(monkeypatch):
    for name in ("ANSWER_VECTOR_FLOOR", "ANSWER_DENSE", "ANSWER_DENSE_ON_QUESTION", "ANSWER_ITEMS"):
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


# --- A′: template v10 ------------------------------------------------------------------------------------------------

#: The length sentence of v10, written out by hand from the lane's brief so the module cannot grade itself.
V10_SENTENCE = (
    "Write only sentences that answer the question; leave out items that are about something else. If the items "
    "establish an answer, write as many sentences as they support, up to twelve, in one to three paragraphs of prose, "
    "with a blank line between paragraphs and no headings or lists, in your own words; each sentence states one "
    "supported fact, from different items where you can, and ends with the numbers of the items that support it, such "
    "as [1] or [1, 2]. Cite at most sixteen different items. Fewer sentences are correct when few items answer the "
    "question: never repeat a fact, add one the items do not state, or present an item that does not answer the "
    "question as if it did. ")
PAIRS = [(False, False, TEMPLATE_VERSION), (False, True, TEMPLATE_VERSION), (True, False, TEMPLATE_VERSION_DENSE),
         (True, True, TEMPLATE_VERSION_DENSE_ON_QUESTION)]
PAIR_IDS = ["off", "on_question_alone", "dense", "dense_on_question"]


def _switches(monkeypatch, dense, on_question):
    monkeypatch.setenv(DENSE, "true" if dense else "false")
    monkeypatch.setenv(ON_QUESTION, "true" if on_question else "false")


def test_v10_differs_from_v9_only_in_the_length_sentence():
    assert LENGTH_SENTENCE_DENSE_ON_QUESTION == V10_SENTENCE
    assert SYSTEM_PROMPT_DENSE.count(LENGTH_SENTENCE_DENSE) == 1 and V10_SENTENCE not in SYSTEM_PROMPT_DENSE
    before, after = SYSTEM_PROMPT_DENSE.split(LENGTH_SENTENCE_DENSE)
    assert SYSTEM_PROMPT_DENSE_ON_QUESTION == before + V10_SENTENCE + after
    # And so v8's every other word too: v10 is v8 with one sentence replaced.
    assert SYSTEM_PROMPT_DENSE_ON_QUESTION.replace(V10_SENTENCE, LENGTH_SENTENCE) == SYSTEM_PROMPT
    for words in ("Write only sentences that answer the question", "as many sentences as they support, up to twelve",
                  "one to three paragraphs", "present an item that does not answer the question as if it did"):
        assert words in SYSTEM_PROMPT_DENSE_ON_QUESTION and words not in SYSTEM_PROMPT_DENSE, words
    assert "seven to twelve sentences" in SYSTEM_PROMPT_DENSE and "seven to twelve" not in SYSTEM_PROMPT_DENSE_ON_QUESTION
    assert TEMPLATE_VERSION_DENSE_ON_QUESTION == "topos-answer-template/v10"
    # Answer protocol 4 adds v11 and v12, v9 and v10 for an asker that parses thirty (`test_answer_protocol_v4`);
    # the three templates of before are as they were.
    assert DENSE_TEMPLATES == {TEMPLATE_VERSION_DENSE, TEMPLATE_VERSION_DENSE_ON_QUESTION,
                               TEMPLATE_VERSION_DENSE_CITE_CAP, TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP}
    assert set(SYSTEM_PROMPTS) == {TEMPLATE_VERSION, TEMPLATE_VERSION_DENSE, TEMPLATE_VERSION_DENSE_ON_QUESTION,
                                   TEMPLATE_VERSION_DENSE_CITE_CAP, TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP}
    assert {key: SYSTEM_PROMPTS[key] for key in (TEMPLATE_VERSION, TEMPLATE_VERSION_DENSE,
                                                 TEMPLATE_VERSION_DENSE_ON_QUESTION)} == {
        TEMPLATE_VERSION: SYSTEM_PROMPT, TEMPLATE_VERSION_DENSE: SYSTEM_PROMPT_DENSE,
        TEMPLATE_VERSION_DENSE_ON_QUESTION: SYSTEM_PROMPT_DENSE_ON_QUESTION}


@pytest.mark.parametrize("dense, on_question, template", PAIRS, ids=PAIR_IDS)
def test_each_pair_of_switches_writes_its_template_budget_and_writer(dense, on_question, template, monkeypatch):
    _switches(monkeypatch, dense, on_question)
    prompt = build_prompt(QUESTION, RECORDS, precision="none")
    assert (prompt.system, prompt.template) == (SYSTEM_PROMPTS[template], template)
    assert writer_template() == ar.answer_template() == template
    assert ar.answer_num_predict(prompt) == (2048 if dense else 1024)
    assert ar.writer_for("build")["template"] == template
    # The prompt carries its template: the budget follows it even after the switches move mid-job.
    _switches(monkeypatch, not dense, not on_question)
    assert ar.answer_num_predict(prompt) == (2048 if dense else 1024)


@pytest.mark.asyncio
async def test_the_writer_is_asked_with_v10_and_2048_tokens(monkeypatch):
    from topos.permissions_v2 import shadow_labeler_local as sl
    _switches(monkeypatch, True, True)
    posted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"model": sl.MODEL, "done": True, "message": {"role": "assistant", "content": REPLY}}

    class Client:
        async def post(self, url, timeout=None, json=None):
            posted.append(json)
            return Response()

        async def aclose(self):
            return None

    class Transport:
        client = Client()
        base_url = "http://host.invalid"

        async def verify(self):
            return None

    monkeypatch.setattr(sl, "open_transport", lambda base_url=None: Transport())
    monkeypatch.setattr(sl, "assessment_base_url", lambda settings=None: "http://host.invalid")
    prompt = build_prompt(QUESTION, RECORDS, precision="none")
    assert await ar._generate_local(prompt, deadline=2_000_000_000) == REPLY
    assert posted[0]["options"] == {"temperature": 0.2, "num_predict": 2048, "num_ctx": 8192}
    assert posted[0]["messages"][0]["content"] == SYSTEM_PROMPT_DENSE_ON_QUESTION


@pytest.mark.parametrize("mode", MODES)
def test_v10_keeps_the_writers_paragraphs_exactly_as_v9_does(mode, monkeypatch):
    def support(sentence, items, question, **_):
        return "rota" not in sentence

    reply = REPLY + "\n\nThis was a busy season.\n\nThe owner tagged one folder nsfw [4]."
    _switches(monkeypatch, True, False)
    _p9, v9 = _checked(reply, mode, support)
    _switches(monkeypatch, True, True)
    p10, v10 = _checked(reply, mode, support)
    assert p10.template == TEMPLATE_VERSION_DENSE_ON_QUESTION
    assert v10.body.answer == v9.body.answer and v10.body.answer.count("\n\n") == 2
    assert len(_as_the_app_reads_it(v10.body.answer)) == 3
    assert {key: getattr(v10, key) for key in COUNTERS} == {key: getattr(v9, key) for key in COUNTERS}
    assert (v10.generated, v10.kept, v10.dropped_support, v10.dropped_scrub) == (10, 7, 1, 1)
    parse_answer_output(v10.body.model_dump(), request_type=FETCH, mode=mode)


@pytest.mark.parametrize("mode", MODES)
def test_with_dense_off_the_on_question_switch_is_1_5_9s_v8(mode, monkeypatch):
    _switches(monkeypatch, False, False)
    _p, off = _checked(REPLY, mode)
    _switches(monkeypatch, False, True)
    prompt, alone = _checked(REPLY, mode)
    assert (prompt.system, prompt.template) == (SYSTEM_PROMPT, TEMPLATE_VERSION)
    assert alone.body.answer == off.body.answer and "\n\n" not in alone.body.answer
    assert {key: getattr(alone, key) for key in COUNTERS} == {key: getattr(off, key) for key in COUNTERS}


@pytest.mark.parametrize("dense, on_question, template", PAIRS, ids=PAIR_IDS)
def test_the_service_prompt_and_receipt_carry_the_template(legacy, tmp_path, monkeypatch, dense, on_question,
                                                           template):
    _switches(monkeypatch, dense, on_question)
    reply = "The owner is working on a synthetic task [1].\n\nThe synthetic task is still under way [1]."
    body, receipt, seen = _service_answer(legacy, tmp_path, monkeypatch, reply)
    assert seen[0].template == receipt["template_version"] == template
    assert seen[0].system == SYSTEM_PROMPTS[template]
    assert body["answer"] == ("The owner is working on a synthetic task ." + ("\n\n" if dense else "\n")
                              + "The synthetic task is still under way .")
    assert set(receipt) == RECEIPT_KEYS and set(receipt["sentences"]) == SENTENCE_KEYS


def test_the_writer_and_a_job_with_no_prompt_name_v10_while_both_are_on(monkeypatch):
    assert ar.writer_for("build", TEMPLATE_VERSION_DENSE_ON_QUESTION)["template"] == TEMPLATE_VERSION_DENSE_ON_QUESTION
    authority = SimpleNamespace(policy_hash="p", protection_revision=1, node_epoch=1)
    job = ar.Job("ans_" + "0" * 32, "answer-1", "grant", "assign", "actor", "client", authority, "only", None, "h",
                 int(time.time()))
    for dense, on_question, template in PAIRS:
        _switches(monkeypatch, dense, on_question)
        assert ar.AnswerService._receipt(job, None, "nothing_matched", 1, 2)["template_version"] == template
        assert ar.writer_for("build")["template"] == template


# --- the switch table ------------------------------------------------------------------------------------------------

def test_the_switch_table_entries_say_what_they_change_and_what_they_never_do():
    floor, on_question = switches.lookup(FLOOR), switches.lookup(ON_QUESTION)
    assert (floor.kind, floor.unbound, floor.bound, floor.floor) == ("int", 25, 25, 0)
    assert (on_question.kind, on_question.unbound, on_question.bound) == ("bool", False, False)
    for words in ("1 of 22 supporting records cleared 0.25", "all-MiniLM-L6-v2",
                  "only which already-permitted members are ranked into the answer's candidates",
                  "never what a grant permits", "Named search, batch and dispatch keep 0.25"):
        assert words in floor.purpose, words
    for words in ("v10", "only its length sentence", "With ANSWER_DENSE off it changes nothing"):
        assert words in on_question.purpose, words
    assert FLOOR in switches.BY_NAME and ON_QUESTION in switches.BY_NAME
