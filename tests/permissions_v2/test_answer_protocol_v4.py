"""Answer protocol 4 (15 Oct 2026, the owner's decision): thirty cited records and 32,000 characters for an asker
that accepts 4.

The node advertises 4. An asker whose control plane says on the ask frame that it accepts 4 is sent up to
`K_ANSWER_V4` (30) cited records and an answer of up to `MAX_ANSWER_CHARS_V4` (32,000) characters, inside byte caps of
their own (`MAX_ANSWER_BYTES_V4` 200,000 answers-only, `MAX_ANSWER_WITH_SOURCES_BYTES_V4` 512,000 with sources). An
asker that accepts 3 keeps 16 and 8,000 and one that says nothing 8 and 8,000, with the byte caps they had.

What moves with it: the dense templates name a v4 asker's cap (v11, v12; a v3 or older job's prompt is byte for
byte as before); a v4 job over more than sixteen items gets a 12,288-token context; retrieval for a with-sources
answer keeps room in the body's byte cap for the answer text (the walk used to fill the cap with records, so a body
over long items failed whole as `body_invalid`); and the body is assembled to fit its asker's caps by dropping
trailing sentences (`dropped_to_fit`) instead of failing. Invented data only; no model is called.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_answer_longer_la164 import _dispatch, _frame
from tests.permissions_v2.test_answer_quality_aq158 import Open
from tests.permissions_v2.test_answer_release import _service
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_knowledge_search import knowledge_policy, node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_generation as ag, answer_release as ar, answer_transport
from topos.permissions_v2.answer_checks import (DENSE_TEMPLATES, TEMPLATE_VERSION, TEMPLATE_VERSION_DENSE,
    TEMPLATE_VERSION_DENSE_CITE_CAP, TEMPLATE_VERSION_DENSE_ON_QUESTION, TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP)
from topos.permissions_v2.answer_generation import (SYSTEM_PROMPT, SYSTEM_PROMPT_DENSE, SYSTEM_PROMPT_DENSE_CITE_CAP,
    SYSTEM_PROMPT_DENSE_ON_QUESTION, SYSTEM_PROMPT_DENSE_ON_QUESTION_CITE_CAP, SYSTEM_PROMPTS, build_prompt,
    close_answer, sieve_answer)
from topos.permissions_v2.answer_protocol import (ACCEPTS_FIELD, ANSWER_PROTOCOL_VERSION, ASK, FETCH, K_ANSWER,
    K_ANSWER_LEGACY, K_ANSWER_V4, LEGACY_ACCEPTED_VERSION, MAX_ANSWER_BYTES, MAX_ANSWER_BYTES_V4, MAX_ANSWER_CHARS,
    MAX_ANSWER_CHARS_V4, MAX_ANSWER_WITH_SOURCES_BYTES_V4, VERSION, AnswerOnly, AnswerWithSources, answer_bytes_cap,
    answer_chars_cap, answer_within_caps, parse_answer_output, records_cap, verify_node_answer)
from topos.permissions_v2.canonical import MAX_BYTES, PolicyError, canonical_bytes
from topos.permissions_v2.knowledge_contract import MessageResult
from topos.permissions_v2.search_contract import MAX_SEARCH_BYTES

P = "TOPOS_PERMISSIONS_V2_"
QUESTION = "What has the owner been doing?"
SIXTEEN = "Cite at most sixteen different items."
#: v11's and v12's cite sentence, written out by hand so the module cannot grade itself.
THIRTY = "Cite at most thirty different items."
# Plain words for long sentences: no digit, no negative, no meta phrase, nothing the scrub reads.
WORDS = ("lantern", "harbour", "market", "workshop", "garden", "archive", "kitchen", "orchard", "studio", "library",
         "bakery", "ferry", "festival", "choir", "gallery", "museum", "bridge", "meadow", "quarry", "lighthouse",
         "greenhouse", "boatyard", "pottery", "mill", "forge", "theatre", "printing", "weaving", "chapel", "stable")
FILLER = ("the boards were wired again, the moulds were poured a second time, the stall batch was counted and packed, "
          "the frames were sanded and oiled, the notes were copied into the shared book, the spare parts were sorted "
          "into labelled tins, the volunteers were thanked by name in the evening round-up, the van was booked for "
          "the next morning, the invoices were filed in the green folder, and the test piece held up through the "
          "whole night")


@pytest.fixture(autouse=True)
def answer_switches_unset(monkeypatch):
    for name in ("ANSWER_ITEMS", "ANSWER_DENSE", "ANSWER_DENSE_ON_QUESTION"):
        monkeypatch.delenv(P + name, raising=False)


def _records(count, content=None):
    """`count` invented items with distinct ids (`f"r.{n:064x}"`)."""
    out = []
    for number in range(1, count + 1):
        rid = f"r.{number:064x}"
        text = content if content is not None else f"An invented note about the {WORDS[(number - 1) % 30]} plans."
        out.append(MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
            "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport",
                                                           "content": text}]}))
    return out


def _cited(count, chars, filler="x"):
    """An answer of exactly `chars` characters that cites records 1 to `count`."""
    tail = " [" + ", ".join(str(number) for number in range(1, count + 1)) + "]."
    return filler * (chars - len(tail)) + tail


def _with_sources(count, chars, filler="x"):
    return {"version": VERSION, "outcome": "answered", "answer": _cited(count, chars, filler),
            "records": [record.model_dump() for record in _records(count)]}


def _only(chars, filler="x"):
    return {"version": VERSION, "outcome": "answered", "answer": filler * chars}


def _parses(body, mode, accepted_version=None):
    try:
        parse_answer_output(body, request_type=FETCH, mode=mode,
                            **({"accepted_version": accepted_version} if accepted_version is not None else {}))
    except PolicyError as exc:
        assert exc.code == "answer_output_invalid"
        return False
    return True


# --- the caps -------------------------------------------------------------------------------------------------------

def test_v4_is_thirty_records_and_32000_characters_and_v3_and_legacy_keep_theirs():
    assert (ANSWER_PROTOCOL_VERSION, K_ANSWER_V4, MAX_ANSWER_CHARS_V4) == (4, 30, 32_000)
    assert (K_ANSWER, K_ANSWER_LEGACY, MAX_ANSWER_CHARS, LEGACY_ACCEPTED_VERSION) == (16, 8, 8_000, 2)
    for version, records, chars in ((1, 8, 8_000), (2, 8, 8_000), (3, 16, 8_000), (4, 30, 32_000), (5, 30, 32_000),
                                    (99, 30, 32_000)):
        assert (records_cap(version), answer_chars_cap(version)) == (records, chars), version


def test_v4s_byte_caps_are_numbers_of_their_own_under_the_canonical_ceiling():
    assert (MAX_ANSWER_BYTES_V4, MAX_ANSWER_WITH_SOURCES_BYTES_V4) == (200_000, 512_000)
    assert MAX_ANSWER_BYTES_V4 < MAX_ANSWER_WITH_SOURCES_BYTES_V4 < MAX_BYTES == 1_048_576
    # Its own constant: the search's cap, which v3 and older keep for a with-sources body, did not move.
    assert MAX_SEARCH_BYTES == 256_000 and MAX_ANSWER_BYTES == 40_000
    for version in (1, 2, 3):
        assert answer_bytes_cap(version, with_sources=False) == MAX_ANSWER_BYTES
        assert answer_bytes_cap(version, with_sources=True) == MAX_SEARCH_BYTES
    for version in (4, 99):
        assert answer_bytes_cap(version, with_sources=False) == MAX_ANSWER_BYTES_V4
        assert answer_bytes_cap(version, with_sources=True) == MAX_ANSWER_WITH_SOURCES_BYTES_V4
    # 32,000 characters of the Basic Multilingual Plane, six canonical bytes each, fit the answers-only cap.
    assert len(canonical_bytes(_only(MAX_ANSWER_CHARS_V4, "é"))) <= MAX_ANSWER_BYTES_V4


def test_a_v4_asker_parses_thirty_records_and_32000_characters_and_a_v3_asker_is_refused_them():
    widest = _with_sources(K_ANSWER_V4, MAX_ANSWER_CHARS_V4)
    assert AnswerWithSources.parse(widest)                                   # the grammar admits the widest body
    assert _parses(widest, "with_sources", 4) and _parses(widest, "with_sources", 99)
    for older in (3, LEGACY_ACCEPTED_VERSION, 1, None):                      # None: a caller that names no version
        assert not _parses(widest, "with_sources", older), older
    # Each cap alone: seventeen records, or 8,001 characters, is past an asker that accepts 3; not past one at 4.
    for body in (_with_sources(K_ANSWER + 1, MAX_ANSWER_CHARS), _with_sources(K_ANSWER, MAX_ANSWER_CHARS + 1)):
        assert not _parses(body, "with_sources", 3) and not _parses(body, "with_sources")
        assert _parses(body, "with_sources", 4)
    assert _parses(_with_sources(K_ANSWER, MAX_ANSWER_CHARS), "with_sources", 3)
    assert _parses(_with_sources(K_ANSWER, MAX_ANSWER_CHARS), "with_sources")
    # An asker that says nothing keeps eight.
    assert _parses(_with_sources(K_ANSWER_LEGACY, 200), "with_sources", 2)
    assert not _parses(_with_sources(K_ANSWER_LEGACY + 1, 200), "with_sources", 2)
    # Answers only: the characters alone.
    assert _parses(_only(MAX_ANSWER_CHARS_V4), "only", 4) and not _parses(_only(MAX_ANSWER_CHARS + 1), "only", 3)
    assert _parses(_only(MAX_ANSWER_CHARS), "only", 3)


def test_the_grammar_refuses_a_thirty_first_record_and_a_32001st_character_at_any_version():
    with pytest.raises(PolicyError):
        AnswerWithSources.parse(_with_sources(K_ANSWER_V4 + 1, 2_000))
    with pytest.raises(PolicyError):
        AnswerOnly.parse(_only(MAX_ANSWER_CHARS_V4 + 1))
    for version in (4, 99):
        assert not _parses(_with_sources(K_ANSWER_V4 + 1, 2_000), "with_sources", version)
        assert not _parses(_with_sources(K_ANSWER_V4, MAX_ANSWER_CHARS_V4 + 1), "with_sources", version)
        assert not _parses(_only(MAX_ANSWER_CHARS_V4 + 1), "only", version)


def test_the_byte_caps_bind_by_version_whatever_the_characters():
    # Answers only at 4: 32,000 six-byte characters fit 200,000; as many emoji (twelve bytes each) do not.
    assert _parses(_only(MAX_ANSWER_CHARS_V4, "é"), "only", 4)
    assert not _parses(_only(MAX_ANSWER_CHARS_V4, "\U0001f600"), "only", 4)
    # At 3 the 40,000-byte cap binds before 8,000 such characters (as before 4: about 6,600 of them).
    assert not _parses(_only(MAX_ANSWER_CHARS, "é"), "only", 3)
    assert _parses(_only(6_000, "é"), "only", 3)
    # With sources at 4: thirty long records and the longest six-byte text overflow 512,000 together.
    long_records = [record.model_dump() for record in _records(K_ANSWER_V4, "y" * 8_000)]
    body = {**_with_sources(K_ANSWER_V4, MAX_ANSWER_CHARS_V4, "é"), "records": long_records}
    assert len(canonical_bytes(body)) > MAX_ANSWER_WITH_SOURCES_BYTES_V4 and not _parses(body, "with_sources", 4)


def test_within_the_caps_counts_the_writer_too():
    """The node's assembly asks with the writer the body will carry (`answer_release.body_fits`)."""
    empty = len(canonical_bytes(_only(0)))
    snug = _only((MAX_ANSWER_BYTES - empty) // 6, "é")
    assert len(canonical_bytes(snug)) <= MAX_ANSWER_BYTES and answer_within_caps(snug, 3)
    writer = ar.writer_for("build", TEMPLATE_VERSION)
    assert not answer_within_caps({**snug, "writer": writer}, 3)
    assert answer_within_caps({**snug, "writer": writer}, 4)
    assert not answer_within_caps(snug, "4") and not answer_within_caps("not a body", 4)


# --- negotiation ----------------------------------------------------------------------------------------------------

def test_an_ask_that_says_it_accepts_four_reaches_the_service_with_four(monkeypatch):
    for value in (4, 5):
        frame, seen = _dispatch(monkeypatch, _frame(**{ACCEPTS_FIELD: value}))
        assert frame["status"] == "ok" and seen[0][1]["accepted_version"] == value
    frame, seen = _dispatch(monkeypatch, _frame(answer_transport.FETCH_TYPE, **{ACCEPTS_FIELD: 4}))
    assert frame["status"] == "error" and seen == []                           # never on a fetch


def _submit(node, service, accepted_version=None, question="Synthetic message"):
    payload = {"question": question}
    signed = node._envelope("grant-search", ASK, payload, node.next_id("answer"))
    with recipient():
        _result, output = service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id,
                                         **({"accepted_version": accepted_version} if accepted_version else {}))
    return output["answer_id"], signed.request_id


def _fetched(node, service, answer_id, mode, accepted_version, disclosure="none"):
    for _ in range(500):
        payload = {"answer_id": answer_id}
        signed = node._envelope("grant-search", FETCH, payload, node.next_id("fetch"))
        with recipient():
            result, output = service.fetch(envelope=signed.model_dump(), payload=payload,
                                           request_id=signed.request_id)
        verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                           envelope=signed, output=output, mode=mode, now=node.now[0], disclosure=disclosure,
                           accepted_version=accepted_version)
        if output.get("outcome"):
            return output
        time.sleep(.01)
    raise AssertionError("answer worker did not finish")


def _receipt(node, request_id):
    with node.ledger._transaction() as db:
        row = db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?", (request_id,)).fetchone()
    return json.loads(row[0])


def _answering(node, reply, seen=None):
    service = _service(node, reply)

    async def generate(prompt, *, deadline):
        if seen is not None:
            seen.append(prompt)
        return reply

    async def judge(sentence, items, *, deadline, question=None, context=None):
        return True

    service.generate, service.judge = generate, judge
    return service


def test_the_job_carries_the_caps_of_the_version_its_asker_accepts(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, max_k=30, answers="with_sources")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _answering(node, "The owner is working on a synthetic task [1].")
    try:
        for accepted, records, chars in ((None, 8, 8_000), (3, 16, 8_000), (4, 30, 32_000), (7, 30, 32_000)):
            answer_id, _request = _submit(node, service, accepted)
            job = service._jobs[answer_id]
            assert (job.records_cap, job.chars_cap, job.accepted_version) == (
                records, chars, accepted or LEGACY_ACCEPTED_VERSION), accepted
            body = _fetched(node, service, answer_id, "with_sources", accepted or LEGACY_ACCEPTED_VERSION)
            assert body["outcome"] == "answered"
    finally:
        service.close()


def test_retrieval_for_a_v4_asker_reaches_thirty_items_and_a_v3_asker_stays_at_sixteen(legacy, tmp_path, monkeypatch):
    from topos.permissions_v2 import search_release
    seen = []
    original = search_release.SearchIntent.parse

    def parse(value):
        seen.append(value.get("k"))
        return original(value)

    monkeypatch.setattr(search_release.SearchIntent, "parse", parse)
    node, _identity = node_for(legacy, tmp_path, monkeypatch, max_k=30, answers="with_sources")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    with node.ledger._transaction() as db:
        authority, _policy = node.ledger._authority(db, "grant-search", node.now[0])
    monkeypatch.setenv(P + "ANSWER_ITEMS", "30")
    for accepted, expected in ((4, 30), (3, 16), (LEGACY_ACCEPTED_VERSION, 8)):
        seen.clear()
        with recipient():
            node.search.retrieve_for_answer(grant_id="grant-search", question="Synthetic message",
                                            admitted_authority=authority, records_cap=records_cap(accepted))
        assert seen[0] == expected, (accepted, seen)


# --- the prompt -----------------------------------------------------------------------------------------------------

def test_v11_and_v12_are_v9_and_v10_with_the_cite_sentence_naming_thirty():
    assert SYSTEM_PROMPT_DENSE.count(SIXTEEN) == SYSTEM_PROMPT_DENSE_ON_QUESTION.count(SIXTEEN) == 1
    assert SYSTEM_PROMPT_DENSE_CITE_CAP == SYSTEM_PROMPT_DENSE.replace(SIXTEEN, THIRTY)
    assert SYSTEM_PROMPT_DENSE_ON_QUESTION_CITE_CAP == SYSTEM_PROMPT_DENSE_ON_QUESTION.replace(SIXTEEN, THIRTY)
    for text in (SYSTEM_PROMPT_DENSE_CITE_CAP, SYSTEM_PROMPT_DENSE_ON_QUESTION_CITE_CAP):
        assert THIRTY in text and "sixteen" not in text
    assert SIXTEEN not in SYSTEM_PROMPT and "thirty" not in SYSTEM_PROMPT        # v8 has no cite sentence
    assert (TEMPLATE_VERSION_DENSE_CITE_CAP, TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP) == (
        "topos-answer-template/v11", "topos-answer-template/v12")
    assert {TEMPLATE_VERSION_DENSE_CITE_CAP, TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP} <= DENSE_TEMPLATES
    assert SYSTEM_PROMPTS[TEMPLATE_VERSION_DENSE_CITE_CAP] == SYSTEM_PROMPT_DENSE_CITE_CAP
    assert SYSTEM_PROMPTS[TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP] == SYSTEM_PROMPT_DENSE_ON_QUESTION_CITE_CAP
    # The sentence names the job's cap: at sixteen it is v9's and v10's word for word.
    assert ag.cite_sentence(K_ANSWER) == SIXTEEN and ag.cite_sentence(K_ANSWER_V4) == THIRTY
    assert ag.system_prompt(TEMPLATE_VERSION_DENSE_CITE_CAP, K_ANSWER) == SYSTEM_PROMPT_DENSE
    assert ag.system_prompt(TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP, K_ANSWER) == SYSTEM_PROMPT_DENSE_ON_QUESTION
    assert ag.system_prompt(TEMPLATE_VERSION_DENSE_CITE_CAP, K_ANSWER_V4) == SYSTEM_PROMPT_DENSE_CITE_CAP
    assert ag.system_prompt(TEMPLATE_VERSION, K_ANSWER_V4) == SYSTEM_PROMPT


@pytest.mark.parametrize("dense, on_question, before, wide", [
    (False, False, TEMPLATE_VERSION, TEMPLATE_VERSION),
    (False, True, TEMPLATE_VERSION, TEMPLATE_VERSION),
    (True, False, TEMPLATE_VERSION_DENSE, TEMPLATE_VERSION_DENSE_CITE_CAP),
    (True, True, TEMPLATE_VERSION_DENSE_ON_QUESTION, TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP),
], ids=["off", "on_question_alone", "dense", "dense_on_question"])
def test_a_v3_or_older_job_is_written_with_the_prompt_of_before_byte_for_byte(monkeypatch, dense, on_question, before,
                                                                              wide):
    monkeypatch.setenv(P + "ANSWER_DENSE", "true" if dense else "false")
    monkeypatch.setenv(P + "ANSWER_DENSE_ON_QUESTION", "true" if on_question else "false")
    records = _records(8)
    plain = build_prompt(QUESTION, records, precision="none")                   # the call of before 4
    assert (plain.template, plain.system) == (before, SYSTEM_PROMPTS[before])
    for cap in (K_ANSWER_LEGACY, K_ANSWER):
        prompt = build_prompt(QUESTION, records, precision="none", records_cap=cap)
        assert (prompt.system, prompt.user, prompt.template) == (plain.system, plain.user, plain.template), cap
        assert ag.writer_template(cap) == before
    v4 = build_prompt(QUESTION, records, precision="none", records_cap=K_ANSWER_V4)
    assert (v4.template, v4.user) == (wide, plain.user) and ag.writer_template(K_ANSWER_V4) == wide
    assert v4.system == (SYSTEM_PROMPTS[wide] if dense else SYSTEM_PROMPT)
    assert ar.answer_num_predict(v4) == ar.answer_num_predict(plain) == (2048 if dense else 1024)


@pytest.mark.parametrize("accepted, template", [(None, TEMPLATE_VERSION_DENSE), (3, TEMPLATE_VERSION_DENSE),
                                                (4, TEMPLATE_VERSION_DENSE_CITE_CAP)], ids=["legacy", "v3", "v4"])
def test_the_service_writes_a_v4_job_with_v11_and_its_receipt_and_writer_say_so(legacy, tmp_path, monkeypatch, accepted,
                                                                                template):
    monkeypatch.setenv(P + "ANSWER_DENSE", "true")
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only", writer_disclosure="build")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    seen = []
    service = _answering(node, "The owner is working on a synthetic task [1].", seen)
    try:
        answer_id, request_id = _submit(node, service, accepted)
        body = _fetched(node, service, answer_id, "only", accepted or LEGACY_ACCEPTED_VERSION, disclosure="build")
    finally:
        service.close()
    assert seen[0].template == template and seen[0].system == SYSTEM_PROMPTS[template]
    assert body["outcome"] == "answered" and body["writer"]["template"] == template
    assert _receipt(node, request_id)["template_version"] == template


# --- the writer's context -------------------------------------------------------------------------------------------

def test_a_v4_job_over_more_than_sixteen_items_gets_a_12288_token_context(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_ITEMS", "30")
    assert (ar.ANSWER_NUM_CTX, ar.ANSWER_NUM_CTX_WIDE) == (8192, 12_288)
    for cap, items, expected in ((K_ANSWER_V4, 17, 12_288), (K_ANSWER_V4, 30, 12_288), (K_ANSWER_V4, 16, 8192),
                                 (K_ANSWER_V4, 1, 8192), (K_ANSWER, 16, 8192), (K_ANSWER_LEGACY, 8, 8192),
                                 (None, 17, 8192), (None, 30, 8192)):
        prompt = build_prompt(QUESTION, _records(items), precision="none", records_cap=cap)
        assert ar.answer_num_ctx(prompt) == expected, (cap, items)


@pytest.mark.parametrize("cap, items, expected", [(K_ANSWER_V4, 17, 12_288), (K_ANSWER, 16, 8192)], ids=["v4", "v3"])
def test_the_writer_is_asked_with_that_context(monkeypatch, cap, items, expected):
    from topos.permissions_v2 import shadow_labeler_local as sl
    posted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"model": sl.MODEL, "done": True, "message": {"role": "assistant", "content": "A step [1]."}}

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
    monkeypatch.setenv(P + "ANSWER_ITEMS", "30")
    prompt = build_prompt(QUESTION, _records(items), precision="none", records_cap=cap)
    assert asyncio.run(ar._generate_local(prompt, deadline=2_000_000_000)) == "A step [1]."
    assert posted[0]["options"] == {"temperature": 0.2, "num_predict": 1024, "num_ctx": expected}


# --- the body fits its asker instead of failing whole ---------------------------------------------------------------

def _long_reply(count, cite=lambda index: 1):
    """`count` long cited sentences (about 460 characters each), one per line."""
    return "\n".join(f"The {WORDS[index % 30]} step was taken: {FILLER} [{cite(index)}]." for index in range(count))


@pytest.mark.parametrize("mode", ["only", "with_sources"])
def test_close_answer_drops_trailing_sentences_until_the_body_fits(mode):
    records = _records(4)
    prompt = build_prompt(QUESTION, records, precision="none", records_cap=K_ANSWER)
    # Thirty sentences: the last four alone cite item 4, so it leaves with them.
    reply = _long_reply(30, cite=lambda index: 4 if index >= 26 else 1 + index % 3)
    sieved = sieve_answer(reply, records, prompt, mode=mode, boundary=Open())
    assert len(sieved.sentences) == 30
    whole = close_answer(sieved, records, prompt, mode=mode, boundary=Open())         # no `fits`: as before
    assert len(whole.body.answer) > MAX_ANSWER_CHARS and whole.dropped_to_fit == 0
    assert not _parses(whole.body, mode, 3)                                           # the old `body_invalid`
    fitted = close_answer(sieved, records, prompt, mode=mode, boundary=Open(),
                          fits=lambda value: answer_within_caps(value, 3))
    assert fitted.body.outcome == "answered" and fitted.reason == "answered"
    assert len(fitted.body.answer) <= MAX_ANSWER_CHARS and _parses(fitted.body, mode, 3)
    assert fitted.dropped_to_fit > 0 and fitted.kept + fitted.dropped_to_fit == len(sieved.sentences)
    # Whole sentences from the end: the delivered text is the first `kept` lines of the untrimmed one.
    assert fitted.body.answer.split("\n") == whole.body.answer.split("\n")[:fitted.kept]
    # One more sentence would not have fitted.
    assert len("\n".join(whole.body.answer.split("\n")[:fitted.kept + 1])) > MAX_ANSWER_CHARS
    if mode == "with_sources":
        assert len(whole.body.records) == 4 and len(fitted.body.records) == 3 == fitted.cited
    # An asker that accepts 4 parses the whole answer: nothing is dropped.
    wide = close_answer(sieved, records, prompt, mode=mode, boundary=Open(),
                        fits=lambda value: answer_within_caps(value, 4))
    assert wide.dropped_to_fit == 0 and wide.body.answer == whole.body.answer and wide.kept == 30


def test_a_single_sentence_that_cannot_fit_is_the_refusal_of_before():
    records = _records(1)
    prompt = build_prompt(QUESTION, records, precision="none")
    sieved = sieve_answer(_long_reply(1), records, prompt, mode="with_sources", boundary=Open())
    with pytest.raises(PolicyError):
        close_answer(sieved, records, prompt, mode="with_sources", boundary=Open(), fits=lambda value: False)


@pytest.mark.parametrize("accepted", [None, 3, 4], ids=["legacy", "v3", "v4"])
def test_the_service_delivers_a_long_answer_shorter_instead_of_body_invalid(legacy, tmp_path, monkeypatch, accepted):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="with_sources")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    reply = _long_reply(30)                                    # about 13,900 characters, each sentence citing item 1
    service = _answering(node, reply)
    try:
        answer_id, request_id = _submit(node, service, accepted)
        body = _fetched(node, service, answer_id, "with_sources", accepted or LEGACY_ACCEPTED_VERSION)
    finally:
        service.close()
    receipt = _receipt(node, request_id)
    sentences = receipt["sentences"]
    assert body["outcome"] == receipt["outcome"] == "answered" and receipt["reason"] == "answered"
    if accepted == 4:
        assert sentences["dropped_to_fit"] == 0 and len(body["answer"]) > MAX_ANSWER_CHARS and sentences["kept"] == 30
    else:
        assert sentences["dropped_to_fit"] > 0 and len(body["answer"]) <= MAX_ANSWER_CHARS
        assert sentences["kept"] + sentences["dropped_to_fit"] == 30


def test_without_the_trim_the_same_answer_was_body_invalid(legacy, tmp_path, monkeypatch):
    """The behaviour of before, for the record: the assembly built the whole body and the closed check refused it."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="with_sources")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(ar, "body_fits", lambda job: (lambda value: True))
    service = _answering(node, _long_reply(30))
    try:
        answer_id, request_id = _submit(node, service, 3)
        body = _fetched(node, service, answer_id, "with_sources", 3)
    finally:
        service.close()
    assert body == {"version": VERSION, "outcome": "no_answer"}
    assert _receipt(node, request_id)["reason"] == "body_invalid"


# --- retrieval keeps room for the answer text -----------------------------------------------------------------------

def test_records_bytes_keeps_room_for_the_longest_text_each_asker_parses():
    assert (ar.ANSWER_TEXT_BYTES_PER_CHAR, ar.ANSWER_BODY_ROOM) == (6, 4_096)
    for version, expected in ((LEGACY_ACCEPTED_VERSION, 203_904), (3, 203_904), (4, 315_904)):
        job = SimpleNamespace(mode="with_sources", accepted_version=version, chars_cap=answer_chars_cap(version))
        assert ar.records_bytes(job) == expected == (answer_bytes_cap(version, with_sources=True)
                                                     - answer_chars_cap(version) * 6 - 4_096), version
    assert ar.records_bytes(SimpleNamespace(mode="only", accepted_version=4, chars_cap=MAX_ANSWER_CHARS_V4)) is None


def _walked(records_bytes=None, count=K_ANSWER_V4):
    """`count` candidates of long items through the real walk, each passing `_accept` (as in test_knowledge_search)."""
    from topos.permissions_v2.canonical import digest
    from topos.permissions_v2.registry import parse_policy
    from topos.permissions_v2.search_release import MessageSearchRelease
    policy = parse_policy(knowledge_policy(K_ANSWER_V4))
    order = ["r." + format(number, "064x") for number in range(count)]
    text = "z" * 8_000

    def accept(conn, floor, review_db, key, grant_id, opaque, *_):
        cited = "r." + format(int(opaque[2:], 16) + 100, "064x")
        record = dict(kind="message", record_id=opaque, content=text, source_ids=["imessage"],
                      citations=[dict(record_id=cited, source_id="imessage", content=text)])
        return record, dict(allow_clause_id="permit-content"), dict(record_key_digest=digest(opaque))

    release = SimpleNamespace(_accept=accept, resolver=SimpleNamespace(path="canonical.db", binding=None,
        provenance_pass=lambda conn, provenance: nullcontext()))
    release._walk_candidates = lambda *arguments: MessageSearchRelease._walk_candidates(release, *arguments)
    extra = {} if records_bytes is None else {"records_bytes": records_bytes}
    output, _decision, _, _bindings = MessageSearchRelease._walk(release, None, None, None, None, "grant-search", order,
        dict.fromkeys(order), policy, None, None, {}, 0, 1, K_ANSWER_V4, SimpleNamespace(policy_hash="a" * 64), **extra)
    return [record.model_dump() for record in output.records]


def test_the_walk_keeps_room_so_the_records_and_the_longest_text_fit_together():
    searched = _walked()                                       # a search: the search's own cap, as before
    assert searched == _walked(MAX_SEARCH_BYTES)
    for version in (3, 4):
        job = SimpleNamespace(mode="with_sources", accepted_version=version, chars_cap=answer_chars_cap(version))
        records = _walked(ar.records_bytes(job))
        assert 0 < len(records) < K_ANSWER_V4
        # Every walked record cited, the longest text the asker parses in six-byte characters, the largest writer.
        body = {"version": VERSION, "outcome": "answered", "answer": _cited(len(records), job.chars_cap, "é"),
                "records": records, "writer": ar.writer_for("build", TEMPLATE_VERSION_DENSE_ON_QUESTION_CITE_CAP)}
        assert answer_within_caps(body, version), version
        assert AnswerWithSources.parse(body)
    # A v4 asker gets more of such items than a v3 one, and more than the search's cap alone held.
    v3 = _walked(ar.records_bytes(SimpleNamespace(mode="with_sources", accepted_version=3, chars_cap=MAX_ANSWER_CHARS)))
    v4 = _walked(ar.records_bytes(SimpleNamespace(mode="with_sources", accepted_version=4,
                                                  chars_cap=MAX_ANSWER_CHARS_V4)))
    assert len(v3) < len(searched) < len(v4)
    # The walk of before (the search's cap, no room kept): the same records and the longest v3 text overflow a v3 body.
    old = {"version": VERSION, "outcome": "answered", "answer": _cited(len(searched), MAX_ANSWER_CHARS, "é"),
           "records": searched}
    assert not answer_within_caps(old, 3)


# Only a v4 asker's with-sources job walks under the reserve; a v3 or older asker's retrieval runs exactly as before 4
# (the search's own cap), so FT168's byte-for-byte guard still holds for it (`answer_release.records_bytes_kwargs`).
@pytest.mark.parametrize("mode, accepted, expected", [("with_sources", 4, 315_904), ("with_sources", 3, MAX_SEARCH_BYTES),
                                                     ("with_sources", None, MAX_SEARCH_BYTES), ("only", 4, MAX_SEARCH_BYTES)],
                         ids=["sources-v4", "sources-v3", "sources-legacy", "only-v4"])
def test_an_answer_job_walks_under_its_reserve_throughout(legacy, tmp_path, monkeypatch, mode, accepted, expected):
    from topos.permissions_v2.search_release import MessageSearchRelease
    seen = []
    original = MessageSearchRelease._walk_candidates

    def walk(self, *args, **kwargs):
        seen.append(args[16] if len(args) > 16 else kwargs.get("records_bytes", MAX_SEARCH_BYTES))
        return original(self, *args, **kwargs)

    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers=mode)
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(MessageSearchRelease, "_walk_candidates", walk)
    service = _answering(node, "The owner is working on a synthetic task [1].")
    try:
        answer_id, _request = _submit(node, service, accepted)
        body = _fetched(node, service, answer_id, mode, accepted or LEGACY_ACCEPTED_VERSION)
    finally:
        service.close()
    assert body["outcome"] == "answered"
    # The first retrieval, the re-check before the body is kept, and each fetch's re-check: one bound throughout.
    assert len(seen) >= 3 and set(seen) == {expected}, seen
