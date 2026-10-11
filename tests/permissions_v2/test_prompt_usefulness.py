"""Evidence preservation and abstention with synthetic, unrelated subject matter."""
import pytest

from topos.permissions_v2.answer_generation import build_prompt, post_check_answer, question_lacks_permitted_anchor
from tests.permissions_v2.test_answer_generation import _record, Boundary
from topos.permissions_v2.knowledge_contract import FactResult, GoalResult


@pytest.mark.parametrize("topic,detail,answer", [
    ("kiln", "I switched off the kiln because the temperature sensor failed.",
     "A sensor failure led to shutting down the kiln [1]."),
    ("orchard", "I postponed the orchard planting because a frost was forecast.",
     "The orchard planting was delayed due to forecast frost [1]."),
    ("telescope", "I cancelled the telescope session because clouds obscured the sky.",
     "Clouds caused the telescope session to be cancelled [1]."),
])
def test_late_evidence_in_a_retrieved_record_can_support_an_answer(topic, detail, answer):
    text = "I organized the supplies and checked the room. " * 20 + detail
    records = [_record("a", text)]
    prompt = build_prompt(f"What happened with the {topic}?", records, precision="none")
    checked = post_check_answer(answer, records, prompt, mode="with_sources", boundary=Boundary())
    assert detail in prompt.user
    assert checked.body.outcome == "answered"
    assert checked.body.records[0].record_id == records[0].record_id


def test_short_records_donate_unused_budget_without_changing_citation_order():
    detail = "I moved the rehearsal because the theatre needed repairs."
    records = [_record("a", "I packed the supplies. " * 40 + detail),
               *[_record(letter, "I prepared a short note.") for letter in "bcdef01"]]
    prompt = build_prompt("What happened with the rehearsal?", records, precision="none")
    assert detail in prompt.user
    assert len(prompt.record_texts) == 8
    assert prompt.record_texts[0].startswith("I packed")


def test_absent_topic_still_abstains_with_larger_context():
    records = [_record("a", "I checked the supplies. " * 50)]
    prompt = build_prompt("What happened with electroplating?", records, precision="none")
    assert question_lacks_permitted_anchor(prompt)
    checked = post_check_answer("Electroplating was successful [1].", records, prompt,
                                mode="only", boundary=Boundary())
    assert checked.body.outcome == "no_answer"


def test_total_body_budget_is_bounded_even_with_eight_long_records():
    records = [_record(letter, "I sorted the materials. " * 200) for letter in "abcdef01"]
    prompt = build_prompt("What happened?", records, precision="none")
    assert sum(len(prompt.raw_texts[n]) for n in range(0, 16, 2)) <= 8 * 420


def _derived(cls, **fields):
    raw = _record("a", "Owner wants to restore the telescope.").model_dump()
    raw.update(fields)
    return cls.parse(raw)


@pytest.mark.parametrize("assertion", ["inferred", "owner_stated"])
def test_fact_assertion_type_reaches_the_writer(assertion):
    record = _derived(FactResult, kind="fact", assertion=assertion)
    prompt = build_prompt("What about the telescope?", [record], precision="none")
    assert f"assertion={assertion}" in prompt.user


@pytest.mark.parametrize("status", ["stated_intention", "active", "completed", "abandoned", "unknown"])
def test_goal_lifecycle_reaches_the_writer(status):
    record = _derived(GoalResult, kind="goal", status=status)
    prompt = build_prompt("What about the telescope?", [record], precision="none")
    assert f"status={status}" in prompt.user


def test_all_released_supports_can_inform_a_derived_record():
    record = _derived(FactResult, kind="fact", assertion="inferred")
    first = "I organized supplies. " * 20 + "I found that the lens needed repair."
    second = "I postponed the telescope restoration until the lens was repaired."
    supports = [record.citations[0].model_copy(update={"content": first}),
                record.citations[0].model_copy(update={"record_id": "r." + "b" * 64, "content": second})]
    record = record.model_copy(update={"citations": supports})
    prompt = build_prompt("What about the telescope?", [record], precision="none")
    assert first in prompt.user and second in prompt.user
    assert len(prompt.raw_texts) == 4  # content, kind, two supports


def test_clipping_does_not_turn_a_sentence_fragment_into_completed_work():
    text = "I completed the telescope " + "alignment procedure " * 180 + "only in a simulation, never in reality."
    prompt = build_prompt("What about the telescope?", [_record("a", text)], precision="none")
    assert not any(raw.startswith("I completed") for raw in prompt.raw_texts)
    assert "truncated" in prompt.user.lower()


def test_a_complete_prefix_is_retained_but_an_over_budget_sentence_is_omitted():
    complete = "I postponed the telescope session."
    records = [_record(letter, complete + " I finished " + "alignment " * 100) for letter in "abcdef01"]
    prompt = build_prompt("What about the telescope?", records, precision="none")
    assert prompt.raw_texts[0] == complete
    assert "I finished" not in prompt.raw_texts[0]


@pytest.mark.parametrize("topic", ["kiln", "kite", "loom", "dyes"])
def test_a_short_absent_topic_cannot_be_answered_by_an_unrelated_citation(topic):
    records = [_record("a", "I prepared for an orchard planting.")]
    prompt = build_prompt(f"What happened with the {topic}?", records, precision="none")
    for text in (f"No information is available regarding the {topic} [1].",
                 f"The {topic} was prepared [1]."):
        checked = post_check_answer(text, records, prompt, mode="with_sources", boundary=Boundary())
        assert checked.body.outcome == "no_answer"


def test_short_topic_can_be_supported_across_citations_without_per_item_carriage():
    records = [_record("a", "I prepared the kiln."), _record("b", "The sensor needed replacement.")]
    prompt = build_prompt("What happened with the kiln?", records, precision="none")
    checked = post_check_answer("The kiln needed a new sensor [1, 2].", records, prompt,
                                mode="with_sources", boundary=Boundary())
    assert checked.body.outcome == "answered"


def test_mode_guidance_does_not_change_evidence_or_relax_the_copy_check():
    records = [_record("a", "I cancelled the demonstration because the workshop roof was damaged.")]
    only = build_prompt("What happened with the demonstration?", records, precision="none", mode="only")
    sources = build_prompt("What happened with the demonstration?", records, precision="none", mode="with_sources")
    assert only.user == sources.user and only.raw_texts == sources.raw_texts
    assert only.record_texts == sources.record_texts
    assert only.system != sources.system
    copied = "The owner cancelled the demonstration because the workshop roof was damaged [1]."
    checked = post_check_answer(copied, records, only, mode="only", boundary=Boundary())
    assert checked.body.outcome == "no_answer" and checked.dropped_copy == 1
    assert post_check_answer(copied, records, sources, mode="with_sources", boundary=Boundary()).body.outcome == "answered"


def test_invalid_answer_mode_cannot_build_a_prompt():
    from topos.permissions_v2.canonical import PolicyError
    with pytest.raises(PolicyError, match="answer_prompt_invalid"):
        build_prompt("What happened?", [_record("a", "I delayed a demonstration.")], precision="none", mode="all")
