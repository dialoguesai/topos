"""BL-146 round 2, item 2: a category question is answered from items the release reviewed into that category.

"What has the owner said about their hobbies?" names a category of the share ("Hobbies"), never a word the items
carry: an item says "bouldering at the quarry", not "hobby". Each released message or journal entry carries the
domains its review gave it, and `_accept` released it on exactly those domains. The answer step reads them, from the
same walk's decisions, as evidence for the matching category word (the domain's own name, in any of its forms) and
for nothing else.

Nothing is widened. The walk's output and decision are the same with or without the domains. The domains never
enter the prompt or the body. A domain counts only for the item that carries it, only for its own name: a hobbies
item never answers a health question, and an item without the domain still drops. Invented data only.
"""
from __future__ import annotations

import json

import pytest

import tests.permissions_v2.test_knowledge_search as tks
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2.answer_generation import build_prompt, post_check_answer, question_lacks_permitted_anchor
from topos.permissions_v2.answer_protocol import same_answer_authority  # noqa: F401 -- the door's own import
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.knowledge_contract import MessageResult

MODES = ("only", "with_sources")


class Boundary:
    def mentions_protected(self, text):
        return False


def _message(letter, text):
    rid = "r." + letter * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


ITEM = _message("a", "Bouldering at the quarry wall felt easy today.")
SENTENCE = "Bouldering went smoothly for them [1]."


def _checked(question, domains, mode, records=(ITEM,), sentence=SENTENCE):
    prompt = build_prompt(question, list(records), precision="none", item_domains=domains)
    return prompt, post_check_answer(sentence, list(records), prompt, mode=mode, boundary=Boundary())


@pytest.mark.parametrize("question,domain", [
    ("What has the owner said about their hobbies?", "hobbies"),
    ("Has the owner mentioned a hobby lately?", "hobbies"),
    ("What has the owner shared about family?", "family"),
    ("What has the owner said about relationships?", "relationships"),
])
def test_a_category_question_is_answered_from_an_item_reviewed_into_that_category(question, domain):
    for mode in MODES:
        prompt, checked = _checked(question, [(domain,)], mode)
        assert not question_lacks_permitted_anchor(prompt)
        assert (checked.body.outcome, checked.reason, checked.dropped_relevance) == ("answered", "answered", 0), mode
        # The same item without its domain still drops: the domain is the only evidence for the category word.
        _prompt, without = _checked(question, None, mode)
        assert without.body.outcome == "no_answer", mode


def test_a_domain_is_evidence_only_for_its_own_name_and_only_for_its_own_item():
    for mode in MODES:
        # A hobbies item never answers a health question.
        _prompt, checked = _checked("What has the owner said about their health?", [("hobbies",)], mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
        # Another item's domain does not carry the subject for the item the sentence cites.
        other = _message("b", "Dinner at the harbour cafe on Friday.")
        _prompt, checked = _checked("What has the owner said about their hobbies?", [(), ("hobbies",)], mode,
                                    records=(ITEM, other))
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
        # A category word is still a subject beside another one the items lack.
        _prompt, checked = _checked("What has the owner said about hobbies and trips?", [("hobbies",)], mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1


def test_the_domains_never_enter_the_prompt_or_the_body():
    prompt = build_prompt("What has the owner said about their health?", [ITEM], precision="none",
                          item_domains=[("health", "hobbies")])
    assert "health" not in prompt.user.split("Permitted items:")[1] and "hobbies" not in prompt.user
    assert all("hobbies" not in text for text in prompt.raw_texts)
    _prompt, checked = _checked("What has the owner said about their hobbies?", [("hobbies",)], "with_sources")
    assert "hobbies" not in json.dumps(checked.body.model_dump())
    with pytest.raises(Exception):
        build_prompt("What has the owner said?", [ITEM], precision="none", item_domains=[(), ()])


def test_the_walk_hands_the_answer_step_the_domains_it_released_on_and_releases_the_same(legacy, tmp_path,
                                                                                          monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "x [1].")
    try:
        answer_id = _ask(node, service, "What has the owner said about the Synthetic message?")
        _finished(node, service, answer_id)
        authority = None
        with node.ledger._transaction() as db:
            authority, _policy = node.ledger._authority(db, "grant-search", node.now[0])
        domains: dict = {}
        _c, _p, with_domains, decision_with = node.search.retrieve_for_answer(
            grant_id="grant-search", question="Synthetic message", admitted_authority=authority, domains=domains)
        _c, _p, without, decision_without = node.search.retrieve_for_answer(
            grant_id="grant-search", question="Synthetic message", admitted_authority=authority)
        assert with_domains.records, "the fixture releases a message"
        assert digest(with_domains.model_dump()) == digest(without.model_dump())
        assert digest(decision_with.model_dump()) == digest(decision_without.model_dump())
        messages = [record.record_id for record in with_domains.records if record.kind == "message"]
        assert messages and all(domains.get(record_id) == ("work",) for record_id in messages)
        assert set(domains) <= {record.record_id for record in with_domains.records}
    finally:
        service.close()


def test_the_answer_pass_answers_a_category_question_from_the_released_domain(legacy, tmp_path, monkeypatch):
    # The fixture's message is reviewed into "work"; a category of five letters or more is needed for the word to
    # be a subject at all, so this share also permits "hobbies" and the review says hobbies.
    original = tks.knowledge_policy

    def with_hobbies(max_k=10, answers=None):
        raw = original(max_k, answers)
        for rule in raw["rules"]:
            if rule["effect"] == "permit":
                for side in ("evidence_use", "release"):
                    rule[side]["predicate"]["terms"][0]["values"] = ["work", "plans", "hobbies"]
        return raw

    monkeypatch.setattr(tks, "knowledge_policy", with_hobbies)
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only", labels={"domains": ["hobbies"]})
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "Their pastime takes up the evenings [1].")
    try:
        answer_id = _ask(node, service, "What has the owner said about hobbies and the Synthetic message?")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        assert receipt["records_used"] >= 1, receipt
        assert body["outcome"] == "answered", receipt
        assert receipt["sentences"]["dropped_relevance"] == 0
    finally:
        service.close()
