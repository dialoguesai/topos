"""Unit tests for the LongMemEval adapter + judge prompt routing (plan D3.1).

Pure functions only: NO models, NO real DB ingest, NO network. The engine-touching
paths (build_bench_db / query_bench) are exercised by the smoke runner, not here.
Marked ``gap`` like the neighbouring qq test modules; the ``public`` marker is
auto-added by tests/conftest.py, so these run in the default CI lane
(``-m "public and not e2e and not live and not qq_eval"``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from longmemeval_adapter import (
    ADAPTER_VERSION,
    LMEQuestion,
    build_message_id,
    build_session_jsonl,
    build_session_records,
    dataset_id_for,
    expected_sender_type,
    gold_plan_pairs,
    iso_utc,
    load_questions,
    parse_lme_date,
    plan_sessions,
)
from adapter.lme_judge import (
    JUDGE_PROMPT_VERSION,
    get_anscheck_prompt,
    parse_label,
    select_anscheck_prompt,
)

pytestmark = pytest.mark.gap


# ---------------------------------------------------------------------------
# Fixtures: a small synthetic question (out-of-order dates + a duplicate id).
# ---------------------------------------------------------------------------


def _question(**overrides) -> LMEQuestion:
    data = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What instrument does the user play?",
        "question_date": "2023/06/01 (Thu) 10:00",
        "answer": "the cello",
        "answer_session_ids": ["s_gold"],
        "haystack_dates": [
            "2023/05/22 (Mon) 09:30",  # s_gold — later than s_b: order needs sorting
            "2023/05/20 (Sat) 02:21",  # s_b
            "2023/05/25 (Thu) 12:00",  # s_b again (duplicate id, different date)
        ],
        "haystack_session_ids": ["s_gold", "s_b", "s_b"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I started cello lessons!", "has_answer": True},
                {"role": "assistant", "content": "Great choice.", "has_answer": True},
                {"role": "user", "content": "Any practice tips?"},
            ],
            [
                {"role": "user", "content": "Tell me about the weather."},
                {"role": "assistant", "content": "It is sunny."},
            ],
            [
                {"role": "user", "content": "Tell me about the weather."},
                {"role": "assistant", "content": "It is sunny."},
            ],
        ],
    }
    data.update(overrides)
    return LMEQuestion.from_mapping(data)


# ---------------------------------------------------------------------------
# Date parsing.
# ---------------------------------------------------------------------------


def test_parse_lme_date_exact_format_is_utc() -> None:
    dt = parse_lme_date("2023/05/20 (Sat) 02:21")
    assert dt == datetime(2023, 5, 20, 2, 21, tzinfo=timezone.utc)
    assert dt.tzinfo == timezone.utc


def test_parse_lme_date_is_locale_independent() -> None:
    # Even if %a matching fails under a non-English locale, the day-of-week token
    # is decorative — the fallback strips it and must yield the same instant.
    assert parse_lme_date("2023/12/31 (Sun) 23:59") == datetime(
        2023, 12, 31, 23, 59, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("bad", ["", "2023-05-20 02:21", "not a date", "2023/05/20"])
def test_parse_lme_date_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_lme_date(bad)


def test_iso_utc_is_uniform() -> None:
    dt = datetime(2023, 5, 20, 2, 21, tzinfo=timezone.utc)
    assert iso_utc(dt) == "2023-05-20T02:21:00+00:00"
    # Naive datetimes are treated as UTC, not localtime.
    assert iso_utc(datetime(2023, 5, 20, 2, 21)) == "2023-05-20T02:21:00+00:00"
    # Uniform shape ⇒ lexicographic order == chronological order.
    later = iso_utc(dt + timedelta(seconds=59))
    assert later > iso_utc(dt)


# ---------------------------------------------------------------------------
# Session planning: chronological sort + duplicate session ids.
# ---------------------------------------------------------------------------


def test_plan_sessions_sorts_chronologically_and_reports_it() -> None:
    plans, sort_needed = plan_sessions(_question())
    assert sort_needed is True
    assert [p.session_key for p in plans] == ["s_b", "s_gold", "s_b__dup1"]
    dates = [p.date for p in plans]
    assert dates == sorted(dates)


def test_plan_sessions_no_sort_flag_when_already_ordered() -> None:
    q = _question(
        haystack_dates=[
            "2023/05/20 (Sat) 02:21",
            "2023/05/22 (Mon) 09:30",
            "2023/05/25 (Thu) 12:00",
        ],
        haystack_session_ids=["s_b", "s_gold", "s_c"],
        haystack_sessions=[
            [{"role": "user", "content": "a"}],
            [{"role": "user", "content": "b", "has_answer": True}],
            [{"role": "assistant", "content": "c"}],
        ],
    )
    plans, sort_needed = plan_sessions(q)
    assert sort_needed is False
    assert [p.session_key for p in plans] == ["s_b", "s_gold", "s_c"]


def test_duplicate_session_ids_get_distinct_keys_first_occurrence_keeps_base() -> None:
    plans, _ = plan_sessions(_question())
    by_key = {p.session_key: p for p in plans}
    # Occurrence order is haystack order (index 1 before index 2), independent of sort.
    assert by_key["s_b"].haystack_index == 1
    assert by_key["s_b__dup1"].haystack_index == 2
    # Distinct thread_ids ⇒ distinct conversations and non-colliding message ids.
    assert build_message_id("q1", "s_b", 0) != build_message_id("q1", "s_b__dup1", 0)


# ---------------------------------------------------------------------------
# JSONL record construction.
# ---------------------------------------------------------------------------


def test_build_session_records_shape_and_timestamps() -> None:
    plans, _ = plan_sessions(_question())
    gold_plan = next(p for p in plans if p.session_key == "s_gold")
    records = build_session_records("q1", gold_plan)

    assert [r["id"] for r in records] == [
        "lme:q1:s_gold:000",
        "lme:q1:s_gold:001",
        "lme:q1:s_gold:002",
    ]
    assert {r["thread_id"] for r in records} == {"s_gold"}
    assert [r["role"] for r in records] == ["user", "assistant", "user"]
    assert records[0]["content"] == "I started cello lessons!"
    # created_at = session date + turn-index seconds, uniform ISO-8601 UTC.
    assert records[0]["created_at"] == "2023-05-22T09:30:00+00:00"
    assert records[1]["created_at"] == "2023-05-22T09:30:01+00:00"
    assert records[2]["created_at"] == "2023-05-22T09:30:02+00:00"
    # Dataset time, not wall clock: the ingest year comes from the corpus.
    assert all(r["created_at"].startswith("2023-") for r in records)


def test_build_session_jsonl_round_trips() -> None:
    plans, _ = plan_sessions(_question())
    payload = build_session_jsonl("q1", plans[0])
    lines = payload.decode("utf-8").strip().split("\n")
    parsed = [json.loads(line) for line in lines]
    assert len(parsed) == len(plans[0].turns)
    for record in parsed:
        assert set(record) == {"id", "thread_id", "role", "content", "created_at"}


def test_build_session_records_rejects_unknown_roles() -> None:
    q = _question(
        haystack_sessions=[
            [{"role": "system", "content": "nope"}],
            [{"role": "user", "content": "x"}],
            [{"role": "user", "content": "x"}],
        ]
    )
    plans, _ = plan_sessions(q)
    bad_plan = next(p for p in plans if p.session_key == "s_gold")
    with pytest.raises(ValueError, match="unexpected role"):
        build_session_records("q1", bad_plan)


def test_expected_sender_type_mirrors_parser() -> None:
    assert expected_sender_type("user") == "human"
    assert expected_sender_type("assistant") == "assistant"


def test_dataset_id_shape_is_frozen() -> None:
    assert dataset_id_for("q1") == "lme:q1"
    assert ADAPTER_VERSION == "lme-adapter-1"


# ---------------------------------------------------------------------------
# Gold mapping.
# ---------------------------------------------------------------------------


def test_gold_pairs_from_has_answer_turns() -> None:
    q = _question()
    assert q.gold == (("s_gold", 0), ("s_gold", 1))


def test_gold_plan_pairs_use_session_keys_in_ingest_order() -> None:
    q = _question()
    plans, _ = plan_sessions(q)
    assert gold_plan_pairs(q, plans) == [("s_gold", 0), ("s_gold", 1)]
    ids = [build_message_id(q.question_id, sk, ti) for sk, ti in gold_plan_pairs(q, plans)]
    assert ids == ["lme:q1:s_gold:000", "lme:q1:s_gold:001"]


def test_is_abstention_property() -> None:
    assert _question().is_abstention is False
    assert _question(question_id="q9_abs").is_abstention is True


def test_answer_session_ids_must_be_in_haystack() -> None:
    with pytest.raises(ValueError, match="answer_session_ids not in haystack"):
        _question(answer_session_ids=["missing_session"])


def test_misaligned_haystack_lists_raise() -> None:
    with pytest.raises(ValueError, match="misaligned haystack lists"):
        _question(haystack_dates=["2023/05/22 (Mon) 09:30"])


# ---------------------------------------------------------------------------
# load_questions.
# ---------------------------------------------------------------------------


def test_load_questions_parses_and_coerces(tmp_path) -> None:
    instances = [
        {
            "question_id": "qa",
            "question_type": "temporal-reasoning",
            "question": "How many days between X and Y?",
            "question_date": "2023/06/01 (Thu) 10:00",
            "answer": 18,  # a handful of dataset answers are ints
            "answer_session_ids": ["s1"],
            "haystack_dates": ["2023/05/20 (Sat) 02:21"],
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[{"role": "user", "content": "hi", "has_answer": True}]],
        }
    ]
    path = tmp_path / "split.json"
    path.write_text(json.dumps(instances), encoding="utf-8")
    questions = load_questions(path)
    assert len(questions) == 1
    assert questions[0].answer == "18"
    assert isinstance(questions[0].answer, str)
    assert questions[0].gold == (("s1", 0),)


def test_load_questions_rejects_non_list(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON list"):
        load_questions(path)


def test_load_questions_missing_field_raises(tmp_path) -> None:
    path = tmp_path / "missing.json"
    path.write_text(json.dumps([{"question_id": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        load_questions(path)


# ---------------------------------------------------------------------------
# Judge prompt selection (verbatim official templates + '_abs' routing).
# ---------------------------------------------------------------------------


def test_default_template_for_session_and_multi_session_types() -> None:
    for task in ("single-session-user", "single-session-assistant", "multi-session"):
        prompt = get_anscheck_prompt(task, "Q?", "A", "R")
        assert prompt.startswith(
            "I will give you a question, a correct answer, and a response from a model."
        )
        assert "Question: Q?" in prompt
        assert "Correct Answer: A" in prompt
        assert "Model Response: R" in prompt
        assert prompt.endswith("Is the model response correct? Answer yes or no only.")
        assert "off-by-one" not in prompt


def test_temporal_template_allows_off_by_one() -> None:
    prompt = get_anscheck_prompt("temporal-reasoning", "Q?", "18", "19")
    assert "do not penalize off-by-one errors" in prompt
    assert "predicting 19 days when the answer is 18" in prompt


def test_knowledge_update_template_accepts_updated_answer() -> None:
    prompt = get_anscheck_prompt("knowledge-update", "Q?", "A", "R")
    assert "updated answer is the required answer" in prompt


def test_preference_template_uses_rubric_wording() -> None:
    prompt = get_anscheck_prompt("single-session-preference", "Q?", "A", "R")
    assert "a rubric for desired personalized response" in prompt
    assert "Rubric: A" in prompt


def test_unknown_task_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        get_anscheck_prompt("mystery-type", "Q?", "A", "R")


def test_abstention_flag_overrides_task_template() -> None:
    prompt = get_anscheck_prompt("temporal-reasoning", "Q?", "A", "R", abstention=True)
    assert prompt.startswith("I will give you an unanswerable question")
    assert prompt.endswith(
        "Does the model correctly identify the question as unanswerable? "
        "Answer yes or no only."
    )
    assert "off-by-one" not in prompt


def test_select_prompt_routes_abs_question_ids_to_abstention() -> None:
    abs_prompt = select_anscheck_prompt("q9_abs", "temporal-reasoning", "Q?", "A", "R")
    assert abs_prompt.startswith("I will give you an unanswerable question")
    normal_prompt = select_anscheck_prompt("q9", "temporal-reasoning", "Q?", "A", "R")
    assert "off-by-one" in normal_prompt


def test_parse_label_is_the_official_substring_rule() -> None:
    assert parse_label("yes") is True
    assert parse_label("Yes.") is True
    assert parse_label("  YES  ") is True
    assert parse_label("The answer is yes") is True
    assert parse_label("no") is False
    assert parse_label("No.") is False
    assert parse_label("") is False


def test_judge_prompt_version_pinned() -> None:
    assert JUDGE_PROMPT_VERSION == "lme-judge-1"
