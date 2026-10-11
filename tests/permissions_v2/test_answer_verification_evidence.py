"""A verifier cannot substitute question text or unseen material for evidence."""
from types import SimpleNamespace
import json
import pytest

from topos.permissions_v2.answer_release import _verified_answer


def verdict(**changes):
    value = dict(requested_information="The price of the materials", answers_question=True,
                 claims_supported=True, evidence=[dict(item=1, quote="I spent 47 dollars.")])
    value.update(changes)
    return json.dumps(value)


@pytest.mark.parametrize("content", [
    verdict(evidence=[dict(item=1, quote="The cost was 100 dollars.")]),
    verdict(evidence=[dict(item=2, quote="I spent 47 dollars.")]),
    verdict(evidence=[dict(item=True, quote="I spent 47 dollars.")]),
    verdict(evidence=[dict(item=1, quote="I spent 47 dollars.", extra="ignored")]),
    verdict(evidence="I spent 47 dollars."),
    verdict(requested_information=""),
    verdict(requested_information=True),
    verdict(answers_question="true"),
    '{"requested_information":"price","answers_question":true,"claims_supported":true,"evidence":[{"item":1,"quote":"wrong","quote":"I spent 47 dollars."}]}',
])
def test_malformed_or_unseen_evidence_cannot_validate_a_reply(content):
    prompt = SimpleNamespace(item_texts=("[1] message\nI spent 47 dollars.",))
    with pytest.raises(ValueError):
        _verified_answer(content, prompt)


def test_only_actual_item_text_can_support_a_reply_not_question_or_proposal():
    prompt = SimpleNamespace(item_texts=("[1] message\nI spent 47 dollars.",),
                             user="Question: What did the free materials cost?")
    assert _verified_answer(verdict(), prompt)
    assert not _verified_answer(verdict(evidence=[]), prompt)
    assert not _verified_answer(verdict(answers_question=False, evidence=[]), prompt)
    with pytest.raises(ValueError):
        _verified_answer(verdict(evidence=[dict(item=1, quote="free materials")]), prompt)


def test_multiple_items_and_released_metadata_can_be_evidence():
    prompt = SimpleNamespace(item_texts=("[1] goal · status=completed\nOwner wants to fix the radio.",
                                        "[2] journal_entry\nI finished the cabinet."))
    assert _verified_answer(verdict(requested_information="goal status",
        evidence=[dict(item=1, quote="status=completed"), dict(item=2, quote="I finished the cabinet.")]), prompt)


@pytest.mark.parametrize("changes", [dict(evidence=[dict(item=0, quote="I spent 47 dollars.")]),
    dict(evidence=[dict(item=-1, quote="I spent 47 dollars.")]),
    dict(evidence=[dict(item=1, quote="")]), dict(evidence=[dict(item=1, quote=True)]),
    dict(evidence=[dict(item=1, quote="I spent 47 dollars.")]*9),
    dict(requested_information="x"*257), dict(claims_supported=1)])
def test_evidence_shape_and_bounds_are_checked_even_when_the_model_would_abstain(changes):
    prompt = SimpleNamespace(item_texts=("[1] message\nI spent 47 dollars.",))
    with pytest.raises(ValueError):
        _verified_answer(verdict(answers_question=False, **changes), prompt)


def test_whitespace_wrapping_does_not_create_a_different_source_quote():
    prompt = SimpleNamespace(item_texts=("[1] message\nI spent\n47 dollars.",))
    assert _verified_answer(verdict(), prompt)


def test_hidden_time_domain_and_unseen_tail_are_not_verifier_evidence():
    from topos.permissions_v2.answer_generation import build_prompt
    from tests.permissions_v2.test_answer_generation import _record
    record = _record("a", "I started a material check. " + "I completed the demonstration " + "alignment "*400 + "only in a simulation.")
    record = record.model_copy(update={"event_at": 1783641600})
    prompt = build_prompt("What happened?", [record], precision="none", item_domains=[["work"]])
    for quote in ("2026-07-10", "projects", "only in a simulation", "ownerimport",
                  "Original owner text; first person outside quotations is the owner"):
        with pytest.raises(ValueError):
            _verified_answer(verdict(evidence=[dict(item=1, quote=quote)]), prompt)
    assert _verified_answer(verdict(requested_information="what was started",
        evidence=[dict(item=1, quote="I started a material check.")]), prompt)
    day = build_prompt("What happened?", [record], precision="day")
    assert _verified_answer(verdict(requested_information="event date",
        evidence=[dict(item=1, quote="2026-07-10")]), day)


def test_valid_quote_from_an_uncited_item_cannot_validate_another_item():
    prompt = SimpleNamespace(item_texts=("[1] journal_entry\nI spent 47 dollars.", "[2] journal_entry\nI spent 80 dollars."))
    with pytest.raises(ValueError, match="evidence_citation_mismatch"):
        _verified_answer(verdict(), prompt, sentences=["The owner paid 80 dollars [2]."])


def test_each_proposed_sentence_needs_evidence_from_its_own_citations():
    prompt = SimpleNamespace(item_texts=("[1] journal_entry\nI spent 47 dollars.", "[2] journal_entry\nI spent 80 dollars."))
    sentences = ["The first cost 47 dollars [1].", "The second cost 80 dollars [2]."]
    assert not _verified_answer(verdict(), prompt, sentences=sentences)
    assert _verified_answer(verdict(evidence=[dict(item=1, quote="I spent 47 dollars."),
        dict(item=2, quote="I spent 80 dollars.")]), prompt, sentences=sentences)


def test_verifier_context_has_the_same_released_evidence_without_writer_instructions():
    from topos.permissions_v2.answer_generation import build_prompt
    from tests.permissions_v2.test_answer_assertion_qualification import inferred, SUPPORT
    prompt = build_prompt("What have they shared about navigation?", [inferred()], precision="none")
    assert "every sentence citing this item begins" in prompt.user
    assert "every sentence citing this item begins" not in prompt.verification_user
    assert "assertion=inferred" in prompt.verification_user
    assert SUPPORT in prompt.user and SUPPORT in prompt.verification_user
    assert all(item in prompt.verification_user for item in prompt.item_texts)
    for forbidden in ("ownerimport", "Node inference;", "Proposal requiring revision"):
        assert forbidden not in prompt.verification_user
