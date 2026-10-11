"""Released inferred facts cannot silently become explicit owner statements."""
import asyncio
import time

import pytest

from tests.permissions_v2 import test_answer_copy_rewrite as mocked
from tests.permissions_v2.test_answer_generation import _record, Boundary
from topos.permissions_v2.answer_generation import build_prompt, post_check_answer
from topos.permissions_v2.answer_release import _generate_local
from topos.permissions_v2.knowledge_contract import FactResult


SUPPORT = "I chose a quiet setting for navigation twice."
UNQUALIFIED = "The owner said they prefer navigation in quiet surroundings [1]."
QUALIFIED = "The records suggest a preference for peaceful navigation surroundings [1]."


def inferred():
    raw = _record("a", "Owner prefers a quiet setting for navigation.").model_dump()
    raw.update(kind="fact", assertion="inferred")
    raw["citations"][0]["content"] = SUPPORT
    return FactResult.parse(raw)


@pytest.mark.parametrize("mode", ["only", "with_sources"])
def test_unqualified_inference_is_withheld_even_without_a_model_check(mode):
    records = [inferred()]
    prompt = build_prompt("What have they shared about navigation?", records, precision="none", mode=mode)
    assert prompt.inferred_items == frozenset({1})
    checked = post_check_answer(UNQUALIFIED, records, prompt, mode=mode, boundary=Boundary())
    assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
    assert post_check_answer(QUALIFIED, records, prompt, mode=mode, boundary=Boundary()).body.outcome == "answered"


@pytest.mark.parametrize("mode", ["only", "with_sources"])
def test_unqualified_proposal_can_get_one_faithful_revision(monkeypatch, mode):
    records = [inferred()]
    prompt = build_prompt("What have they shared about navigation?", records, precision="none", mode=mode)
    monkeypatch.setattr(mocked, "SOURCE", SUPPORT)
    calls, closed = mocked.host(monkeypatch, [UNQUALIFIED, QUALIFIED])
    raw = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert raw == QUALIFIED and len(calls) == 3 and closed == [True]
    assert "lacks the required" in calls[1]["json"]["messages"][0]["content"]
    assert post_check_answer(raw, records, prompt, mode=mode, boundary=Boundary()).body.outcome == "answered"


def test_unqualified_revision_is_not_retried_or_verified(monkeypatch):
    prompt = build_prompt("What have they shared about navigation?", [inferred()], precision="none")
    calls, closed = mocked.host(monkeypatch, [UNQUALIFIED, UNQUALIFIED])
    assert asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110)) == ""
    assert len(calls) == 2 and closed == [True]


def test_explicit_owner_text_and_uncited_inferences_need_no_prefix():
    stated = _record("b", "I postponed navigation because the compass was missing.")
    prompt = build_prompt("What happened with navigation?", [inferred(), stated], precision="none")
    text = "A missing compass delayed navigation practice [2]."
    checked = post_check_answer(text, [inferred(), stated], prompt, mode="with_sources", boundary=Boundary())
    assert checked.body.outcome == "answered" and checked.dropped_relevance == 0


def test_qualification_is_checked_per_sentence_and_citation():
    records = [inferred(), _record("b", "I postponed navigation because the compass was missing.")]
    prompt = build_prompt("What happened with navigation?", records, precision="none")
    checked = post_check_answer(UNQUALIFIED + "\nA missing compass delayed navigation practice [2].",
                                records, prompt, mode="with_sources", boundary=Boundary())
    assert checked.body.outcome == "answered" and checked.kept == checked.dropped_relevance == 1
    assert len(checked.body.records) == 1 and checked.body.records[0].record_id == records[1].record_id


def test_inference_directives_are_absent_for_original_text_only():
    from topos.permissions_v2.answer_generation import INFERRED_GUIDANCE
    original = build_prompt("What happened with navigation?", [_record("b", SUPPORT)], precision="none")
    derived = build_prompt("What happened with navigation?", [inferred()], precision="none")
    assert INFERRED_GUIDANCE not in original.system
    assert INFERRED_GUIDANCE in derived.system
    assert not original.inferred_items and derived.inferred_items == frozenset({1})


def test_goal_lifecycle_guidance_reaches_verification_without_claiming_a_future_event(monkeypatch):
    from topos.permissions_v2.answer_generation import GOAL_VERIFY_GUIDANCE
    from topos.permissions_v2.knowledge_contract import GoalResult
    raw = _record("a", "Owner wants to improve navigation.").model_dump()
    raw.update(kind="goal", status="completed")
    records = [GoalResult.parse(raw)]
    prompt = build_prompt("What are the plans for navigation?", records, precision="none")
    assert prompt.goal_items == frozenset({1}) and not prompt.inferred_items
    monkeypatch.setattr(mocked, "SOURCE", "goal · status=completed")
    text = "The navigation goal is marked completed [1]."
    calls, closed = mocked.host(monkeypatch, [text])
    result = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
    assert result == text and len(calls) == 2 and closed == [True]
    assert GOAL_VERIFY_GUIDANCE in calls[1]["json"]["messages"][0]["content"]
    assert post_check_answer(result, records, prompt, mode="with_sources", boundary=Boundary()).body.outcome == "answered"
