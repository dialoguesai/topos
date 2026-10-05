"""A model's response is data; only checked sentences reach the answer body."""
from __future__ import annotations

from topos.permissions_v2.answer_generation import build_prompt, post_check_answer
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
