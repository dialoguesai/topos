"""A2A-4 amendment 9: what the sharer's policy discloses about the model that wrote an answer.

The level is signed in the policy (declared or absent); a final body carries exactly that level's keys, or no writer
at all; a writer under a policy that discloses nothing is refused; an older policy keeps its bytes. Invented data only.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_answer_protocol import DECLARATION
from tests.permissions_v2.test_answer_release import _ask, _service
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401
from tests.permissions_v2.test_knowledge_search import knowledge_policy, node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401
from topos.permissions_v2.answer_checks import TEMPLATE_VERSION
from topos.permissions_v2.answer_protocol import (FETCH, WRITER_KEYS, AnswerOnly, AnswerWithSources, AnswerWriter,
    effective_disclosure, parse_answer_output, verify_node_answer, writer_level)
from topos.permissions_v2.answer_release import writer_for
from topos.permissions_v2.canonical import PolicyError, canonical_bytes, digest
from topos.permissions_v2.knowledge_contract import KnowledgeDeclaration, KnowledgePolicy
from topos.permissions_v2.shadow_labeler_local import MODEL, MODEL_REVISION

ONLY = {"version": "topos-answer/v1", "outcome": "answered", "answer": "The invented trip is on Friday."}
RID = "r." + "c" * 64
RECORD = {"kind": "journal_entry", "record_id": RID, "content": "An invented trip was planned for Friday.",
          "source_ids": ["invented-journal"],
          "citations": [{"record_id": RID, "source_id": "invented-journal", "content": "An invented trip was planned for Friday."}]}
SOURCES = {"version": "topos-answer/v1", "outcome": "answered", "answer": "The invented trip is on Friday [1].", "records": [RECORD]}
FULL = {"origin": "local", "provider": "ollama", "model": "invented:1b", "build": "sha256:" + "d" * 8, "template": "topos-answer-template/v7"}


def writer_at(level: str) -> dict:
    return {key: value for key, value in FULL.items() if key in WRITER_KEYS[level]}


def test_the_policy_level_is_declared_or_absent_and_an_old_policy_keeps_its_bytes():
    old = KnowledgeDeclaration.parse(DECLARATION)
    assert "writer_disclosure" not in old.model_dump()
    for level in WRITER_KEYS:
        assert KnowledgeDeclaration.parse({**DECLARATION, "writer_disclosure": level}).model_dump()["writer_disclosure"] == level
    assert digest(old.model_dump()) != digest(KnowledgeDeclaration.parse({**DECLARATION, "writer_disclosure": "model"}).model_dump())
    with pytest.raises(PolicyError):
        KnowledgeDeclaration.parse({**DECLARATION, "writer_disclosure": None})
    with pytest.raises(PolicyError):
        KnowledgeDeclaration.parse({**DECLARATION, "writer_disclosure": "none"})
    # The whole policy: the field absent dumps to the bytes a 1.5.3 node signed; present, it is inside the hash.
    plain, told = KnowledgePolicy.parse(knowledge_policy(answers="only")), KnowledgePolicy.parse(knowledge_policy(answers="only", writer_disclosure="build"))
    assert b"writer_disclosure" not in canonical_bytes(plain.model_dump())
    assert told.search.writer_disclosure == "build" and digest(plain.model_dump()) != digest(told.model_dump())
    assert effective_disclosure(plain) == "none" and effective_disclosure(told) == "build"


def test_a_writer_carries_exactly_one_level_and_nothing_undeclared():
    for level, keys in WRITER_KEYS.items():
        writer = AnswerWriter.parse(writer_at(level))
        assert writer_level(writer) == level and set(writer.model_dump()) == set(keys)
    for broken in ({"origin": "local", "model": "invented:1b"},                      # model without provider
                   {"origin": "local", "provider": "ollama", "model": "x", "build": "y"},  # build without template
                   {"origin": "local", "provider": None},                             # an explicit null
                   {"origin": "elsewhere"}, {"origin": "local", "provider": "someone"},
                   {"origin": "local", "provider": "ollama", "model": "in\tvented"}, {}):
        with pytest.raises(PolicyError):
            AnswerWriter.parse(broken)


@pytest.mark.parametrize("mode,body", [("only", ONLY), ("with_sources", SOURCES)])
def test_a_body_may_carry_its_policy_level_or_no_writer_and_never_more(mode, body):
    # No writer: valid under every level (a node from before the field).
    for disclosure in ("none", *WRITER_KEYS):
        parsed = parse_answer_output(body, request_type=FETCH, mode=mode, disclosure=disclosure)
        assert parsed.writer is None and "writer" not in parsed.model_dump()
    for level in WRITER_KEYS:
        told = {**body, "writer": writer_at(level)}
        parsed = parse_answer_output(told, request_type=FETCH, mode=mode, disclosure=level)
        assert parsed.model_dump()["writer"] == writer_at(level)
        # The closed reading (no level passed) and every other level refuse it.
        with pytest.raises(PolicyError, match="answer_output_invalid"):
            parse_answer_output(told, request_type=FETCH, mode=mode)
        for other in ("none", *(item for item in WRITER_KEYS if item != level)):
            with pytest.raises(PolicyError, match="answer_output_invalid"):
                parse_answer_output(told, request_type=FETCH, mode=mode, disclosure=other)
    with pytest.raises(PolicyError, match="answer_output_invalid"):
        parse_answer_output({**body, "writer": None}, request_type=FETCH, mode=mode, disclosure="model")
    # The dump of a body without a writer is the 1.5.3 dump, so every golden vector and signature stands.
    dumped = (AnswerOnly if mode == "only" else AnswerWithSources).parse(body).model_dump()
    assert "writer" not in dumped and dumped["answer"] == body["answer"]


def test_the_node_writes_its_pinned_local_model_cut_to_the_level():
    assert writer_for("none") is None and writer_for("anything") is None
    for level, keys in WRITER_KEYS.items():
        writer = writer_for(level)
        assert set(writer) == set(keys) and writer_level(AnswerWriter.parse(writer)) == level
    assert writer_for("build") == {"origin": "local", "provider": "ollama", "model": MODEL, "build": MODEL_REVISION,
                                   "template": TEMPLATE_VERSION}


def _fetch_with(node, service, answer_id, disclosure):
    import time
    payload = {"answer_id": answer_id}
    for _ in range(100):
        signed = node._envelope("grant-search", FETCH, payload, node.next_id("fetch"))
        with recipient():
            result, output = service.fetch(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
        verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                           envelope=signed, output=output, mode="only", now=node.now[0], disclosure=disclosure)
        if output.get("outcome"):
            return output
        time.sleep(.01)
    raise AssertionError("answer worker did not finish")


@pytest.mark.parametrize("level", ["origin", "model", "build"])
def test_an_answer_under_a_disclosing_share_carries_the_writer_and_verifies_at_that_level(legacy, tmp_path, monkeypatch, level):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only", writer_disclosure=level)
    with owner(): node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The owner is working on a synthetic task [1].")
    try:
        answer_id = _ask(node, service, "Synthetic message")
        body = _fetch_with(node, service, answer_id, level)
        assert body["outcome"] == "answered" and body["writer"] == writer_for(level)
        assert set(body) == {"version", "outcome", "answer", "writer"}
    finally:
        service.close()


def test_an_answer_under_a_share_that_discloses_nothing_carries_no_writer(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner(): node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The owner is working on a synthetic task [1].")
    try:
        answer_id = _ask(node, service, "Synthetic message")
        body = _fetch_with(node, service, answer_id, "none")
        assert body["outcome"] == "answered" and set(body) == {"version", "outcome", "answer"}
    finally:
        service.close()


def test_the_node_advertises_at_least_answer_version_two_when_answers_are_on(monkeypatch):
    """Amendment 9 needs 2; amendment 13 (LA164) moved the number to 3 and answer protocol 4 to 4, each of which still
    honours a disclosure level."""
    from topos.engine import registration
    from topos.permissions_v2 import switches
    monkeypatch.setattr(switches, "is_bound", lambda: True)
    monkeypatch.setattr(switches, "on", lambda _name: True)
    assert registration._answer_version() == registration.ANSWER_VERSION == 4
    monkeypatch.setattr(switches, "on", lambda _name: False)
    assert registration._answer_version() == 0
