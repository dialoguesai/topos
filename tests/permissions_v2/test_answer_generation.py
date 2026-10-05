"""A model's response is data; only checked sentences reach the answer body."""
from __future__ import annotations

from topos.permissions_v2.answer_generation import build_prompt, post_check_answer, question_lacks_permitted_anchor
from topos.permissions_v2.knowledge_contract import JournalEntryResult


class Boundary:
    def mentions_protected(self, text):
        return "secretperson" in text.lower()


def _record(letter, text):
    rid = "r." + letter * 64
    return JournalEntryResult.parse({"kind": "journal_entry", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


def test_only_mode_releases_no_source_schema_or_citation_markers():
    records = [_record("a", "A cabin was reserved near the lake."), _record("b", "The train leaves on Friday.")]
    prompt = build_prompt("What is planned?", records, precision="none")
    assert records[0].record_id not in prompt.user and "ownerimport" not in prompt.user
    checked = post_check_answer("They plan a cabin trip [1]. The train leaves Friday [2].", records, prompt,
                                mode="only", boundary=Boundary())
    assert checked.body.outcome == "answered"
    assert "[1]" not in checked.body.answer and "[2]" not in checked.body.answer
    assert "records" not in checked.body.model_dump()


def test_prompt_bounds_item_text_and_excludes_source_identifiers():
    content = "A" * 420 + "PRIVATE_TAIL" * 20
    record = _record("a", content)
    prompt = build_prompt("What happened?", [record], precision="none")
    assert "A" * 420 in prompt.user
    assert "PRIVATE_TAIL" not in prompt.user
    assert record.record_id not in prompt.user
    assert "ownerimport" not in prompt.user


def test_with_sources_includes_only_cited_records_and_renumbers_in_first_use_order():
    records = [_record("a", "A cabin was reserved."), _record("b", "The train leaves Friday.")]
    prompt = build_prompt("When?", records, precision="none")
    checked = post_check_answer("The train is Friday [2].", records, prompt, mode="with_sources", boundary=Boundary())
    assert checked.body.answer == "The train is Friday [1]."
    assert len(checked.body.records) == 1 and checked.body.records[0].record_id == records[1].record_id


def test_question_or_answer_about_protected_person_never_yields_a_body():
    records = [_record("a", "A meeting was planned.")]
    prompt = build_prompt("What happened?", records, precision="none")
    checked = post_check_answer("Secretperson was there [1].", records, prompt, mode="only", boundary=Boundary())
    assert checked.body.model_dump() == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert checked.reason == "answer_protected"


def test_uncited_and_copied_sentences_are_removed_not_repaired():
    records = [_record("a", "We booked the lakeside cabin for the second weekend of June and paid the deposit on Friday.")]
    prompt = build_prompt("What did they do?", records, precision="none")
    checked = post_check_answer("They booked the lakeside cabin for the second weekend of June [1]. An uncited claim.",
                                records, prompt, mode="only", boundary=Boundary())
    assert checked.body.outcome == "no_answer"
    assert checked.dropped_copy == 1 and checked.dropped_citation == 1


def test_an_unseen_question_subject_cannot_become_answer_evidence():
    records = [_record("a", "A cabin was reserved near the lake.")]
    prompt = build_prompt("What does shadowglass reference mean?", records, precision="none")
    assert question_lacks_permitted_anchor(prompt)
    for mode in ("only", "with_sources"):
        checked = post_check_answer("Shadowglass describes a cabin plan [1].", records, prompt,
                                    mode=mode, boundary=Boundary())
        assert checked.body.outcome == "no_answer"
        assert checked.dropped_question_echo == 1
    ordinary = build_prompt("What happened at the cabin?", records, precision="none")
    assert not question_lacks_permitted_anchor(ordinary)


def test_a_mixed_question_still_drops_its_unseen_word_if_the_model_echoes_it():
    records = [_record("a", "A lakeside cabin was reserved for the trip.")]
    prompt = build_prompt("Did shadowglass affect the lakeside cabin?", records, precision="none")
    assert not question_lacks_permitted_anchor(prompt)
    checked = post_check_answer("Shadowglass affected the cabin plan [1].", records, prompt,
                                mode="only", boundary=Boundary())
    assert checked.body.outcome == "no_answer" and checked.dropped_question_echo == 1


def test_irrelevant_citation_cannot_answer_a_specific_question():
    records = [_record("a", "The eviction loop was incorrect and caused system thrashing."),
               _record("b", "The habanero mash needs another day of fermentation.")]
    prompt = build_prompt("What went wrong with my first pepper mash?", records, precision="none")
    for mode in ("only", "with_sources"):
        checked = post_check_answer("The pepper mash failed because the eviction loop thrashed [1].",
                                    records, prompt, mode=mode, boundary=Boundary())
        assert checked.body.outcome == "no_answer"
        assert checked.dropped_relevance == 1


def test_cited_items_must_support_all_question_subject_terms():
    records = [_record("a", "The glass bead was purple."),
               _record("b", "The striped glass bead cracked during cooling.")]
    prompt = build_prompt("Why did my first glass bead crack?", records, precision="none")
    wrong = post_check_answer("The bead cracked during cooling [1].", records, prompt,
                              mode="with_sources", boundary=Boundary())
    assert wrong.body.outcome == "no_answer" and wrong.dropped_relevance == 1
    right = post_check_answer("The bead cracked during cooling [2].", records, prompt,
                              mode="with_sources", boundary=Boundary())
    assert right.body.outcome == "answered" and right.dropped_relevance == 0


def test_two_cited_items_can_jointly_support_the_question_subject():
    records = [_record("a", "The pepper harvest was ready."),
               _record("b", "The mash jar was cleaned for fermentation.")]
    prompt = build_prompt("What happened with the pepper mash?", records, precision="none")
    checked = post_check_answer("The harvest and jar were prepared [1, 2].", records, prompt,
                                mode="with_sources", boundary=Boundary())
    assert checked.body.outcome == "answered"
    assert len(checked.body.records) == 2
