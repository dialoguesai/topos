"""Lane TR162 (13 Oct 2026, A2A-4 amendment 12): answers respect the question's time.

Seen live by the owner in the recipient's view: "What has <owner> been working on this week?", "... up to lately?",
"... coming up?" were answered from items one to two months old, and a late-September follow-up was called upcoming
on 13 October. Retrieval ranked the whole grant window by words alone; the writer and the judge never knew the date.

A  `answer_time.question_scope`: a closed parser of time words into a window relative to the job's clock, whole UTC
   days, off when the grant withholds times (`ANSWER_TIME_SCOPE`).
B  `retrieve_for_answer`: ranking and the walk inside the scope intersected with the grant window, widened once when
   fewer than two items are released there (`ANSWER_TIME_RETRIEVAL`).
C  the prompt says today's date and the scope at the grant's precision (`ANSWER_TIME_PROMPT`); judge v4 reads the
   same lines and the time rule (`ANSWER_SUPPORT_JUDGE_TEMPLATE`, default v4, v3 kept).
D  a sentence citing only out-of-scope items is dropped; widened, it stands only when it places itself in time
   (`ANSWER_TIME_GUARD`, `dropped_out_of_scope`).

Invented data only.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.permissions_v2.test_answer_quality_aq158 import Open, _message
from topos.permissions_v2 import answer_release as ar, answer_time as at, switches
from topos.permissions_v2.answer_generation import build_prompt, judge_sentences, post_check_answer, sieve_answer

P = switches.PREFIX
DAY = 86_400
NOW = int(dt.datetime(2026, 10, 13, 15, 0, tzinfo=dt.timezone.utc).timestamp())     # a Tuesday, 15:00 UTC
TIME_SWITCHES = (P + "ANSWER_TIME_SCOPE", P + "ANSWER_TIME_RETRIEVAL", P + "ANSWER_TIME_PROMPT", P + "ANSWER_TIME_GUARD",
                 P + "ANSWER_SUPPORT_JUDGE_TEMPLATE")


def day(text: str) -> int:
    return int(dt.datetime.fromisoformat(text + "T00:00:00+00:00").timestamp())


END = NOW + 1          # "up to now": the grant window's own upper edge


@pytest.fixture(autouse=True)
def every_time_switch_unset(monkeypatch):
    for name in TIME_SWITCHES:
        monkeypatch.delenv(name, raising=False)


# --- A: the parser table ---------------------------------------------------------------------------------------------

TABLE = [
    ("What is Ivo up to today?", "today", day("2026-10-13"), END, False),
    ("What is Ivo doing tonight?", "today", day("2026-10-13"), END, False),
    ("What did Ivo do yesterday?", "yesterday", day("2026-10-12"), day("2026-10-13"), False),
    ("What has Ivo been working on this week?", "this_week", day("2026-10-07"), END, False),
    ("What happened this past week?", "this_week", day("2026-10-07"), END, False),
    ("Anything new in the last week?", "this_week", day("2026-10-07"), END, False),
    ("What did Ivo do last week?", "last_week", day("2026-09-30"), day("2026-10-07"), False),
    ("What has Ivo done this month?", "this_month", day("2026-10-01"), END, False),
    ("What did Ivo do last month?", "last_month", day("2026-09-01"), day("2026-10-01"), False),
    ("What has Ivo done in the past month?", "past_days", day("2026-09-14"), END, False),
    ("What happened in the past 3 days?", "past_days", day("2026-10-11"), END, False),
    ("What has Ivo done over the last two weeks?", "past_days", day("2026-09-30"), END, False),
    ("Anything in the past few days?", "past_days", day("2026-10-11"), END, False),
    ("What has Ivo done the last couple of weeks?", "past_days", day("2026-09-30"), END, False),
    ("What has Ivo been up to lately?", "recent", day("2026-09-29"), END, False),
    ("What has Ivo mentioned recently?", "recent", day("2026-09-29"), END, False),
    ("What is Ivo into these days?", "recent", day("2026-09-29"), END, False),
    ("What is Ivo currently reading?", "recent", day("2026-09-29"), END, False),
    ("What is Ivo doing now?", "recent", day("2026-09-29"), END, False),
    ("What plans does Ivo have coming up?", "upcoming", day("2026-09-30"), END, True),
    ("Any upcoming trips?", "upcoming", day("2026-09-30"), END, True),
    ("What is Ivo doing next week?", "upcoming", day("2026-09-30"), END, True),
    ("What is Ivo doing tomorrow?", "upcoming", day("2026-09-30"), END, True),
    ("What did Ivo do last week and this week?", "mixed", day("2026-09-30"), END, False),
    ("What did Ivo do last month, and what is coming up?", "mixed", day("2026-09-01"), END, True),
]


@pytest.mark.parametrize("question,kind,lower,upper,future", TABLE, ids=[row[0] for row in TABLE])
@pytest.mark.parametrize("precision", ["day", "second"])
def test_the_parser_table(question, kind, lower, upper, future, precision):
    scope = at.question_scope(question, now=NOW, precision=precision)
    assert (scope.kind, scope.lower, scope.upper, scope.future, scope.now) == (kind, lower, upper, future, NOW)
    assert scope.kind in at.KINDS


@pytest.mark.parametrize("question", ["What is Ivo working on?", "Do you know where Ivo is?", "What weekly habits?",
                                      "What is Ivo's nowhere project?", "Tell me about the week-long trip.",
                                      "What did the owner say about months of training?"])
def test_a_question_with_no_time_expression_has_no_scope(question):
    """"week-long" splits into tokens "week", "long": no listed expression starts with "week" alone."""
    assert at.question_scope(question, now=NOW, precision="day") is None


@pytest.mark.parametrize("question", [row[0] for row in TABLE])
def test_precision_none_turns_the_feature_off(question):
    """The grant withholds event times: no scope at all, so nothing downstream reads one."""
    assert at.question_scope(question, now=NOW, precision="none") is None
    assert at.question_scope(question, now=NOW, precision="minute") is None          # no other precision is read


@pytest.mark.parametrize("question", [row[0] for row in TABLE])
def test_every_edge_is_a_utc_midnight_or_the_grant_windows_own_upper_edge(question):
    """Never finer than a day, under "second" too: the records door's day rule (both edges on a UTC midnight), with
    the one exception the grant window itself has, its upper edge at now."""
    for precision in ("day", "second"):
        scope = at.question_scope(question, now=NOW, precision=precision)
        widened = at.widen(scope)
        for edge in (scope.lower, widened.lower):
            assert edge % DAY == 0
        assert scope.upper % DAY == 0 or scope.upper == END
        assert widened.upper == scope.upper


def test_the_closed_lists_are_pinned():
    assert at.RECENT_DAYS == 14 and at.FUTURE_DAYS == 14 and at.SCOPE_FLOOR == 2 and at.WIDEN_FACTOR == 3
    # The app's own-AI ladder (features/home/recencyLadder.ts: 3, 7, 14; two fill a rung, the last needs one), with
    # one more rung on the node.
    assert at.RECENT_RUNGS_DAYS == (3, 7, 14, 30) and at.RUNG_ENOUGH == 2 and at.LAST_RUNG_ENOUGH == 1
    assert at.KINDS == ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "past_days",
                        "recent", "upcoming", "mixed")
    assert [" ".join(phrase) + "=" + kind for phrase, kind in at.EXPRESSIONS] == [
        "today=today", "tonight=today", "this morning=today", "this afternoon=today", "this evening=today",
        "yesterday=yesterday", "the last week=this_week", "the past week=this_week", "past week=this_week",
        "this week=this_week", "last week=last_week", "the last month=past_month", "the past month=past_month",
        "past month=past_month", "this month=this_month", "last month=last_month", "lately=recent",
        "recently=recent", "recent=recent", "these days=recent", "currently=recent", "now=recent",
        "coming up=upcoming", "upcoming=upcoming", "next week=upcoming", "next month=upcoming",
        "tomorrow=upcoming", "soon=upcoming", "plans=upcoming", "plan=upcoming", "planning=upcoming",
        "planned=upcoming"]
    assert at.EARLIER_PHRASES == ("earlier", "previously", "ago", "before that", "last month", "last week", "back in",
                                  "in the past", "weeks before", "months before", "at the time", "back then")


def test_a_scope_is_never_wider_than_the_grant_window():
    scope = at.question_scope("What did Ivo do last month?", now=NOW, precision="day")
    grant_lower, grant_upper = (NOW - 14 * DAY) * 1_000_000, NOW * 1_000_000          # a 14-day grant
    low, high = scope.bounds_us(grant_lower, grant_upper)
    assert low == grant_lower and high == day("2026-10-01") * 1_000_000 - 1
    inside = at.question_scope("What did Ivo do today?", now=NOW, precision="day").bounds_us(grant_lower, grant_upper)
    assert inside == (day("2026-10-13") * 1_000_000, grant_upper)
    wide = at.widen(at.question_scope("What did Ivo do last month?", now=NOW, precision="day"))
    assert wide.bounds_us(grant_lower, grant_upper)[0] == grant_lower                # widened, still inside the grant


def test_widening_is_three_times_the_span_in_whole_days_ending_where_it_ended():
    today = at.question_scope("today?", now=NOW, precision="day")
    assert at.widen(today).lower == day("2026-10-11") and at.widen(today).upper == END
    last_week = at.question_scope("last week?", now=NOW, precision="day")
    assert at.widen(last_week).lower == day("2026-09-16") and at.widen(last_week).upper == day("2026-10-07")


def test_the_guard_reads():
    scope = at.question_scope("this week?", now=NOW, precision="day")
    assert at.in_scope(day("2026-10-07"), scope) and not at.in_scope(day("2026-10-06"), scope)
    assert at.in_scope(None, scope)                                  # an item with no released time is never outside
    for sentence in ("In September they wired the lamp.", "On 2026-09-01 they wired the lamp.",
                     "Earlier, they wired the lamp.", "Six weeks ago they wired the lamp.",
                     "Back in the summer they wired the lamp."):
        assert at.dated_sentence(sentence), sentence
    for sentence in ("They wired the lamp.", "They are wiring the lamp this week.", "They will wire the lamp soon."):
        assert not at.dated_sentence(sentence), sentence


# --- C: what the writer is told -----------------------------------------------------------------------------------

def test_the_time_lines_at_each_precision():
    scope = at.question_scope("What has Ivo been working on this week?", now=NOW, precision="day")
    lines = at.time_lines(now=NOW, precision="day", scope=scope)
    assert lines.startswith("Today is 2026-10-13 (UTC). Each item's date is the day it was written.")
    assert "The question asks about 2026-10-07 to 2026-10-13." in lines
    assert "never call what it says upcoming" in lines
    assert at.time_lines(now=NOW, precision="second", scope=scope) == lines            # a date, never a time of day
    assert "15:00" not in lines and "T15" not in lines
    assert at.time_lines(now=NOW, precision="none", scope=scope) == ""
    assert at.time_lines(now=None, precision="day", scope=None) == ""
    future = at.question_scope("What plans does Ivo have coming up?", now=NOW, precision="day")
    assert "asks what is coming up; the items shown were written from 2026-09-30 to 2026-10-13" in at.time_lines(
        now=NOW, precision="day", scope=future)
    plain = at.time_lines(now=NOW, precision="day", scope=None)
    assert plain.startswith("Today is 2026-10-13 (UTC).") and "The question asks" not in plain


def _dated(digit, text, when):
    return _message(digit, text).model_copy(update={"event_at": when})


RECENT = _dated("1", "Wired the second lantern board; it lights without flicker.", day("2026-10-12"))
OLD = _dated("2", "Wired the garden shed lights and fixed the door.", day("2026-09-01"))


def test_the_prompt_carries_today_at_the_grants_precision_and_nothing_at_none():
    question = "What has the owner been working on this week?"
    scope = at.question_scope(question, now=NOW, precision="day")
    prompt = build_prompt(question, [RECENT, OLD], precision="day", now=NOW, time_scope=scope)
    assert prompt.time_line == at.time_lines(now=NOW, precision="day", scope=scope)
    user = prompt.user
    assert user.index(question) < user.index("Today is 2026-10-13 (UTC).") < user.index("Permitted items:")
    assert "[1] message · 2026-10-12" in user and "[2] message · 2026-09-01" in user
    assert prompt.item_times == (day("2026-10-12"), day("2026-09-01")) and prompt.time_scope == scope
    none = build_prompt(question, [RECENT.model_copy(update={"event_at": None})], precision="none", now=NOW,
                        time_scope=scope)
    assert "Today" not in none.user and none.time_line == "" and none.time_scope is None


def test_with_the_prompt_switch_off_the_prompt_is_1_5_9s(monkeypatch):
    question = "What has the owner been working on this week?"
    scope = at.question_scope(question, now=NOW, precision="day")
    before = build_prompt(question, [RECENT, OLD], precision="day")
    monkeypatch.setenv(P + "ANSWER_TIME_PROMPT", "false")
    off = build_prompt(question, [RECENT, OLD], precision="day", now=NOW, time_scope=scope)
    assert off.user == before.user and off.system == before.system and off.time_line == ""


# --- D: the deterministic guard -------------------------------------------------------------------------------------

def _guarded(sentences, *, widened=False, records=(RECENT, OLD), question="What has the owner been working on this week?"):
    scope = at.question_scope(question, now=NOW, precision="day")
    prompt = build_prompt(question, list(records), precision="day", now=NOW, time_scope=scope, time_widened=widened)
    return post_check_answer("\n".join(sentences), list(records), prompt, mode="with_sources", boundary=Open())


def test_a_sentence_citing_only_out_of_scope_items_is_dropped():
    checked = _guarded(["The owner wired a lantern board [1].", "The owner fixed a shed door [2]."])
    assert checked.kept == 1 and checked.dropped_out_of_scope == 1
    assert "shed" not in checked.body.answer


def test_a_sentence_citing_an_in_scope_item_beside_an_old_one_stands():
    checked = _guarded(["The owner wired lights for a lantern and a shed [1, 2]."])
    assert checked.kept == 1 and checked.dropped_out_of_scope == 0


def test_widened_an_old_item_stands_only_when_the_sentence_places_it_in_time():
    dropped = _guarded(["The owner fixed a shed door [2]."], widened=True)
    assert dropped.kept == 0 and dropped.dropped_out_of_scope == 1 and dropped.reason == "all_sentences_dropped"
    kept = _guarded(["In September the owner fixed a shed door [2]."], widened=True)
    assert kept.kept == 1 and kept.dropped_out_of_scope == 0
    unwidened = _guarded(["In September the owner fixed a shed door [2]."], widened=False)
    assert unwidened.dropped_out_of_scope == 1                       # not widened: an old item never answers


def test_no_scope_and_the_switch_off_drop_nothing(monkeypatch):
    plain = _guarded(["The owner fixed a shed door [2]."], question="What has the owner been working on?")
    assert plain.kept == 1 and plain.dropped_out_of_scope == 0
    monkeypatch.setenv(P + "ANSWER_TIME_GUARD", "false")
    off = _guarded(["The owner fixed a shed door [2]."])
    assert off.kept == 1 and off.dropped_out_of_scope == 0


def test_the_guard_runs_after_the_other_checks_and_before_the_judge():
    question = "What has the owner been working on this week?"
    scope = at.question_scope(question, now=NOW, precision="day")
    prompt = build_prompt(question, [RECENT, OLD], precision="day", now=NOW, time_scope=scope)
    sieved = sieve_answer("The owner wired a lantern board [1].\nThe owner fixed a shed door [2].", [RECENT, OLD],
                          prompt, mode="with_sources", boundary=Open())
    assert sieved.sentences == ("The owner wired a lantern board [1].",) and sieved.dropped_out_of_scope == 1
    seen = []
    judge_sentences(sieved, prompt, lambda sentence, items, question, **extra: seen.append((sentence, extra)) or True)
    assert seen == [("The owner wired a lantern board [1].", {"context": prompt.time_line})]


# --- C: judge v4 ----------------------------------------------------------------------------------------------------

def test_judge_v4_is_v3_with_the_time_rule_and_reads_the_nodes_time_lines():
    v3, v4 = ar.JUDGE_SYSTEM_PROMPTS["v3"], ar.JUDGE_SYSTEM_PROMPTS["v4"]
    assert v4 != v3 and v4.startswith(v3.split(" Reply with exactly ")[0]) and v4.endswith(v3[v3.index(" Reply with"):].lstrip())
    for words in ("Time rule", "today's date", "dated before those dates as current or recent", "upcoming or planned",
                  "names no later date"):
        assert words in v4
    assert ar.JUDGE_FORMATS["v4"] == ar.JUDGE_FORMAT_V3
    assert ar.judge_template() == "v4" and ar.judge_template_version() == "topos-answer-support-judge/v4"
    items = ("[1] message · 2026-09-01\nWired the garden shed lights.",)
    lines = at.time_lines(now=NOW, precision="day", scope=None)
    with_time = ar.judge_messages("They are wiring shed lights [1].", items, "v4", "What is Ivo doing now?", lines)
    assert with_time[1]["content"] == (lines + "\n\nQuestion (quoted data):\nWhat is Ivo doing now?\n\nItems (quoted data):"
                                       "\n\n" + items[0] + "\n\nSentence (quoted data):\nThey are wiring shed lights.")
    # v3 never reads the lines; v4 without lines is v3's user message.
    assert ar.judge_messages("x [1].", items, "v3", "Q?", lines)[1] == ar.judge_messages("x [1].", items, "v3", "Q?")[1]
    assert ar.judge_messages("x [1].", items, "v4", "Q?")[1] == ar.judge_messages("x [1].", items, "v3", "Q?")[1]
    assert ar.parse_judge_reply('{"supported": "yes", "answers": "yes"}', "v4") is True
    assert ar.parse_judge_reply('{"supported": "yes", "answers": "no"}', "v4") is False
    assert ar.parse_judge_reply('{"supported": "yes"}', "v4") is None


def test_a_callable_written_for_v3_is_called_as_before_when_the_prompt_has_no_time_lines():
    prompt = build_prompt("What has the owner been doing?", [RECENT], precision="none")
    seen = []
    post_check_answer("The owner wired a board [1].", [RECENT], prompt, mode="only", boundary=Open(),
                      support=lambda sentence, items, question: seen.append(question) or True)
    assert seen == ["What has the owner been doing?"]


# --- B through the service: dated items, an injected clock -----------------------------------------------------------

THIS_WEEK = ["Working on the lantern firmware; the second board lights up.",
             "Working on the lantern enclosure moulds at the studio."]
SIX_WEEKS = ["Working on the garden shed roof before the rain.",
             "Working on the garden shed door and the hinges."]


def _policy(precision):
    from tests.permissions_v2.direct_search_twins import knowledge_policy

    def factory():
        raw = knowledge_policy()
        raw["search"]["answers"] = "only"
        raw["search"]["release_event_time"] = precision
        return raw
    return factory


def _node(tmp_path, precision="day", times=None, bodies=None):
    from tests.permissions_v2.direct_search_twins import build
    times = times or [NOW - DAY, NOW - 2 * DAY, NOW - 42 * DAY, NOW - 43 * DAY]
    bodies = bodies or THIS_WEEK + SIX_WEEKS
    return build(tmp_path / "twin", members=len(times), hidden_facts=0, seed=162, policy_factory=_policy(precision),
                 now_override=NOW, event_times=times, bodies=bodies)


def _asked(node, question, answer=None, judge=None):
    """One ask through the real service; the stand-in model records the prompt and answers `answer(prompt)`."""
    from tests.permissions_v2.test_answer_release import _ask, _finished, _service
    prompts = []
    service = _service(node, "unused")

    async def generate(prompt, *, deadline):
        prompts.append(prompt)
        return answer(prompt) if answer else "The owner worked on a project [1]."
    service.generate = generate
    if judge is not None:
        service.judge = judge
    try:
        answer_id = _ask(node, service, question)
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            row = db.execute("SELECT receipt_json FROM p2a_receipts ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        service.close()
    return body, json.loads(row[0]), prompts


def _bodies(prompt):
    return sorted(line.split("\n", 2)[-1].strip() for line in prompt.item_lines)


def _number(prompt, words):
    return next(number for number, block in enumerate(prompt.item_lines, 1) if words in block)


def test_this_week_answers_from_this_weeks_items_only(tmp_path):
    node = _node(tmp_path)
    body, receipt, prompts = _asked(node, "What has the owner been working on this week?")
    assert body["outcome"] == "answered"
    assert _bodies(prompts[0]) == sorted(THIS_WEEK)
    assert "Today is 2026-10-13 (UTC)." in prompts[0].user and "2026-10-07 to 2026-10-13" in prompts[0].user
    assert receipt["time_scope"] == "this_week" and receipt["time_scope_widened"] is False
    assert receipt["sentences"]["dropped_out_of_scope"] == 0 and receipt["time_prompt_version"] == "topos-answer-time/v1"
    assert receipt["records_used"] == 2 and receipt["judge_template_version"] == "topos-answer-support-judge/v4"


def test_lately_excludes_six_week_old_items(tmp_path):
    node = _node(tmp_path)
    _body, receipt, prompts = _asked(node, "What has the owner been working on lately?")
    assert _bodies(prompts[0]) == sorted(THIS_WEEK) and receipt["time_scope"] == "recent"


def test_a_question_with_no_time_word_is_unchanged(tmp_path, monkeypatch):
    node = _node(tmp_path)
    _body, receipt, prompts = _asked(node, "What has the owner been working on?")
    assert _bodies(prompts[0]) == sorted(THIS_WEEK + SIX_WEEKS)
    assert receipt["time_scope"] == "none" and receipt["time_scope_widened"] is False
    for name in TIME_SWITCHES[:4]:
        monkeypatch.setenv(name, "false")
    _body, before, old = _asked(node, "What has the owner been working on?")
    assert old[0].item_lines == prompts[0].item_lines                  # the same items, in the same order
    assert "time_scope" not in before and "dropped_out_of_scope" not in before["sentences"]
    assert "time_prompt_version" not in before and "Today" not in old[0].user


def test_with_retrieval_off_the_whole_window_is_read_and_the_guard_drops_the_old_items_sentence(tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_TIME_RETRIEVAL", "false")
    node = _node(tmp_path)

    def answer(prompt):
        return f"The owner repaired a shed roof [{_number(prompt, 'shed roof')}]."
    body, receipt, prompts = _asked(node, "What has the owner been working on this week?", answer)
    assert _bodies(prompts[0]) == sorted(THIS_WEEK + SIX_WEEKS)        # 1.5.9's retrieval
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["sentences"]["dropped_out_of_scope"] == 1 and receipt["reason"] == "all_sentences_dropped"
    assert receipt["time_scope"] == "this_week" and receipt["time_scope_widened"] is False


def test_too_few_items_in_the_scope_widens_once_and_the_widened_answer_must_place_itself_in_time(tmp_path):
    node = _node(tmp_path)
    question = "What has the owner been working on today?"         # nothing today; the two this-week items are 1-2 d old
    body, receipt, prompts = _asked(node, question, lambda prompt: "The owner built a lantern board [1].")
    assert _bodies(prompts[0]) == sorted(THIS_WEEK)                  # widened to three days: 11 to 13 Oct
    assert receipt["time_scope"] == "today" and receipt["time_scope_widened"] is True
    assert body["outcome"] == "no_answer" and receipt["sentences"]["dropped_out_of_scope"] == 1
    body, receipt, _ = _asked(node, question, lambda prompt: "In October the owner built a lantern board [1].")
    assert body["outcome"] == "answered" and receipt["sentences"]["dropped_out_of_scope"] == 0


def test_a_widened_scope_that_still_finds_nothing_is_the_normal_no_answer(tmp_path):
    node = _node(tmp_path, times=[NOW - 42 * DAY, NOW - 43 * DAY], bodies=SIX_WEEKS)
    calls = []
    body, receipt, prompts = _asked(node, "What has the owner been working on this week?",
                                    lambda prompt: calls.append(1) or "x [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"} and prompts == [] and calls == []
    assert receipt["reason"] == "nothing_matched" and receipt["time_scope"] == "this_week"


def test_precision_none_reads_no_scope_and_tells_the_model_no_date(tmp_path):
    node = _node(tmp_path, precision="none")
    _body, receipt, prompts = _asked(node, "What has the owner been working on this week?")
    assert _bodies(prompts[0]) == sorted(THIS_WEEK + SIX_WEEKS)
    assert receipt["time_scope"] == "none" and "Today" not in prompts[0].user and prompts[0].time_scope is None


def test_second_precision_scopes_by_whole_days_and_says_only_the_date(tmp_path):
    node = _node(tmp_path, precision="second")
    _body, receipt, prompts = _asked(node, "What has the owner been working on this week?")
    assert _bodies(prompts[0]) == sorted(THIS_WEEK) and receipt["time_scope"] == "this_week"
    assert "Today is 2026-10-13 (UTC)." in prompts[0].user and "15:00" not in prompts[0].time_line


def test_the_judge_gets_the_time_lines_through_the_service(tmp_path):
    node = _node(tmp_path)
    seen = []

    async def judge(sentence, items, *, deadline, question=None, context=None):
        seen.append(context)
        return True
    _body, _receipt, prompts = _asked(node, "What has the owner been working on this week?", judge=judge)
    assert seen and seen[0] == prompts[0].time_line and seen[0].startswith("Today is 2026-10-13 (UTC).")


def test_the_recheck_walks_the_written_items_unchanged(tmp_path):
    """BL-159's re-check (`record_ids=`) reads no scope: the fetch re-walks the written items on the grant window."""
    from tests.permissions_v2.message_search_harness import recipient
    node = _node(tmp_path)
    _body, _receipt, prompts = _asked(node, "What has the owner been working on this week?")
    with node.ledger._transaction() as db:
        authority, _policy = node.ledger._authority(db, "grant-search", node.now[0])
    first = {}
    with recipient():
        _c, _p, output, _d = node.search.retrieve_for_answer(grant_id="grant-search", question="Working this week?",
                                                             admitted_authority=authority, time_scope=first)
        ids = tuple(record.record_id for record in output.records)
        old = node.search.retrieve_for_answer(grant_id="grant-search", question="Working", admitted_authority=authority)[2]
        old_ids = tuple(record.record_id for record in old.records if record.record_id not in ids)
        again = {}
        _c, _p, still, _d = node.search.retrieve_for_answer(grant_id="grant-search", question="Working this week?",
                                                            admitted_authority=authority, record_ids=ids + old_ids,
                                                            time_scope=again)
    assert first["scope"].kind == "this_week" and len(ids) == 2 and again == {}
    assert tuple(record.record_id for record in still.records) == ids + old_ids     # the old ones too: no scope


def test_with_the_scope_switch_off_a_time_word_reads_as_1_5_9_did(tmp_path, monkeypatch):
    """A off: no scope, so retrieval reads the whole window and the guard has nothing to read; C still says the date."""
    monkeypatch.setenv(P + "ANSWER_TIME_SCOPE", "false")
    node = _node(tmp_path)

    def answer(prompt):
        return f"The owner repaired a shed roof [{_number(prompt, 'shed roof')}]."
    body, receipt, prompts = _asked(node, "What has the owner been working on this week?", answer)
    assert _bodies(prompts[0]) == sorted(THIS_WEEK + SIX_WEEKS) and body["outcome"] == "answered"
    assert "time_scope" not in receipt and "dropped_out_of_scope" not in receipt["sentences"]
    assert prompts[0].time_scope is None and "The question asks" not in prompts[0].user
    assert "Today is 2026-10-13 (UTC)." in prompts[0].user


# --- the recency ladder (WS0, 13 Oct): the `recent` class climbs 3 -> 7 -> 14 -> 30 days ---------------------------
# The app's own-AI turn has had this since 28 Sep (topos-react-app features/home/recencyLadder.ts: rungs 3, 7, 14;
# two distinct records fill a rung, the last rung needs one; the answer says which rung it used). The node's answer
# door climbs the same rungs, with a fourth of 30 days, for "lately", "recently", "recent", "these days", "currently"
# and "now" only. A rung is today and the N days before it, in whole UTC days, as the app counts them.

LATELY = "What has the owner been working on lately?"
OLDER = ["Working on the canal boat engine with a neighbour.", "Working on the allotment fence posts."]


def test_the_plan_for_a_recent_question_is_the_four_rungs():
    scope = at.question_scope(LATELY, now=NOW, precision="day")
    plan = at.plan(scope)
    today = day("2026-10-13")
    assert [step.window.lower for step in plan] == [today - days * DAY for days in (3, 7, 14, 30)]
    assert [step.window.upper for step in plan] == [END] * 4
    assert [step.enough for step in plan] == [2, 2, 2, 1]                    # the last rung needs only one
    assert [step.widened for step in plan] == [False, False, False, True]
    assert [step.scope.rung_days for step in plan] == [3, 7, 14, 30]
    # What the guard holds a sentence to: the rung itself, and on the last rung the 14 days that still read as recent.
    assert [step.scope.lower for step in plan] == [today - days * DAY for days in (3, 7, 14, 14)]
    assert scope.rung_days == 0 and scope.lower == today - 14 * DAY          # no rung until retrieval fills one


@pytest.mark.parametrize("question,kind", [
    ("What has the owner been working on this week?", "this_week"), ("What did the owner do yesterday?", "yesterday"),
    ("What is the owner doing today?", "today"), ("What did the owner do last week?", "last_week"),
    ("What has the owner done in the past 5 days?", "past_days"), ("What plans are coming up?", "upcoming"),
    ("What did the owner do lately and last month?", "mixed")])
def test_an_explicit_expression_keeps_its_exact_window_and_the_single_widening(question, kind):
    scope = at.question_scope(question, now=NOW, precision="day")
    plan = at.plan(scope)
    assert scope.kind == kind and [step.window for step in plan] == [scope, at.widen(scope)]
    assert [step.enough for step in plan] == [at.SCOPE_FLOOR, at.SCOPE_FLOOR]
    assert [step.widened for step in plan] == [False, True] and all(step.scope == scope for step in plan)
    assert all(step.scope.rung_days == 0 for step in plan)


def test_the_rung_line_at_each_rung_and_nothing_at_none():
    scope = at.question_scope(LATELY, now=NOW, precision="day")
    for step, days in zip(at.plan(scope), (3, 7, 14, 30)):
        lines = at.time_lines(now=NOW, precision="day", scope=step.scope)
        assert f"The question asks about recent days; these items are from the last {days} days." in lines
        assert ("Little is recent: say when each thing happened." in lines) is (days == 30)
        assert "The question asks about 2026" not in lines
        assert at.time_lines(now=NOW, precision="none", scope=step.scope) == ""
    plain = at.time_lines(now=NOW, precision="day", scope=scope)                  # no rung used: the nominal dates
    assert "The question asks about 2026-09-29 to 2026-10-13." in plain and "these items are from" not in plain


def _ladder(tmp_path, ages, bodies=None, precision="day"):
    """A twin node whose items are `ages` days old (same time of day as the clock)."""
    bodies = bodies or (THIS_WEEK + SIX_WEEKS + OLDER)[:len(ages)]
    return _node(tmp_path, precision=precision, times=[NOW - age * DAY for age in ages], bodies=bodies)


@pytest.mark.parametrize("ages,rung,used", [
    ([1, 2, 5, 10, 20, 42], 3, 2),           # two inside three days: the first rung fills
    ([1, 5, 10, 20, 42], 7, 2),              # one inside three days, two inside seven
    ([1, 10, 20, 42], 14, 2),                # one inside seven, two inside fourteen
    ([10, 20, 42], 30, 2),                   # one inside fourteen (not the last rung: two are needed), two inside thirty
    ([20, 25, 42], 30, 2),                   # none inside fourteen
], ids=["3", "7", "14", "30-after-one-in-14", "30"])
def test_lately_stops_at_the_first_rung_two_items_fill(tmp_path, ages, rung, used):
    node = _ladder(tmp_path, ages)
    _body, receipt, prompts = _asked(node, LATELY, lambda prompt: "In October the owner worked on a project [1].")
    assert receipt["time_scope"] == "recent" and receipt["time_scope_rung_days"] == rung
    assert receipt["records_used"] == used and receipt["time_scope_widened"] is (rung == 30)
    assert sorted(prompts[0].item_times) == sorted(
        (NOW - age * DAY) // DAY * DAY for age in ages if age <= rung)
    assert f"these items are from the last {rung} days." in prompts[0].user
    assert prompts[0].time_scope.rung_days == rung


def test_the_last_rung_needs_only_one_item(tmp_path):
    node = _ladder(tmp_path, [20, 42])
    body, receipt, prompts = _asked(node, LATELY, lambda prompt: "In September the owner worked on firmware [1].")
    assert receipt["time_scope_rung_days"] == 30 and receipt["records_used"] == 1 and body["outcome"] == "answered"
    assert receipt["time_scope_widened"] is True
    # The fourteen-day rung is not the last: one item there does not fill it, and the thirty-day rung is named.
    node = _ladder(tmp_path / "b", [10, 42])
    body, receipt, prompts = _asked(node, LATELY, lambda prompt: "The owner worked on firmware [1].")
    assert receipt["time_scope_rung_days"] == 30 and receipt["records_used"] == 1
    assert body["outcome"] == "answered"         # ten days old is inside the fourteen that read as recent: no date needed


def test_on_the_thirty_day_rung_a_sentence_must_say_when(tmp_path):
    node = _ladder(tmp_path, [20, 25, 42])
    body, receipt, prompts = _asked(node, LATELY, lambda prompt: "The owner worked on firmware [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["sentences"]["dropped_out_of_scope"] == 1 and receipt["time_scope_rung_days"] == 30
    assert "Little is recent: say when each thing happened." in prompts[0].user
    body, receipt, _ = _asked(node, LATELY, lambda prompt: "In September the owner worked on firmware [1].")
    assert body["outcome"] == "answered" and receipt["sentences"]["dropped_out_of_scope"] == 0


def test_nothing_inside_thirty_days_is_the_normal_no_answer_and_names_no_rung(tmp_path):
    node = _ladder(tmp_path, [42, 43], bodies=SIX_WEEKS)
    body, receipt, prompts = _asked(node, LATELY)
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"} and prompts == []
    assert receipt["reason"] == "nothing_matched" and receipt["time_scope"] == "recent"
    assert receipt["time_scope_rung_days"] == 0 and receipt["time_scope_widened"] is False


def test_the_ladder_is_off_when_the_grant_withholds_times(tmp_path):
    node = _ladder(tmp_path, [1, 2, 20, 42], precision="none")
    _body, receipt, prompts = _asked(node, LATELY)
    assert len(prompts[0].item_lines) == 4 and "recent days" not in prompts[0].user and "Today" not in prompts[0].user
    assert receipt["time_scope"] == "none" and receipt["time_scope_rung_days"] == 0


def test_the_judge_is_told_the_rung(tmp_path):
    node = _ladder(tmp_path, [1, 5, 42])
    seen = []

    async def judge(sentence, items, *, deadline, question=None, context=None):
        seen.append(context)
        return True
    _asked(node, LATELY, judge=judge)
    assert seen and "The question asks about recent days; these items are from the last 7 days." in seen[0]
    messages = ar.judge_messages("They worked on firmware [1].", ("[1] message · 2026-10-12\nx",), "v4", LATELY, seen[0])
    assert "these items are from the last 7 days." in messages[1]["content"].split("Question (quoted data):")[0]


def test_an_explicit_week_never_climbs_the_ladder(tmp_path):
    """"this week" is the last seven days exactly: one item inside three days does not narrow it, and no rung is named."""
    node = _ladder(tmp_path, [1, 5, 20, 42])
    _body, receipt, prompts = _asked(node, "What has the owner been working on this week?")
    assert receipt["time_scope"] == "this_week" and receipt["time_scope_rung_days"] == 0
    assert len(prompts[0].item_lines) == 2 and "recent days" not in prompts[0].user
    assert "The question asks about 2026-10-07 to 2026-10-13." in prompts[0].user


def test_with_retrieval_off_lately_names_no_rung_and_the_guard_holds_to_fourteen_days(tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_TIME_RETRIEVAL", "false")
    node = _ladder(tmp_path, [1, 20, 42])

    def answer(prompt):
        return (f"The owner worked on lantern firmware [{_number(prompt, 'lantern firmware')}].\n"
                f"The owner repaired a shed roof [{_number(prompt, 'shed roof')}].")
    body, receipt, prompts = _asked(node, LATELY, answer)
    assert len(prompts[0].item_lines) == 3 and "these items are from" not in prompts[0].user
    assert receipt["time_scope_rung_days"] == 0 and receipt["sentences"]["dropped_out_of_scope"] == 1
    assert body["outcome"] == "answered" and "shed" not in body["answer"]


def test_with_the_scope_switch_off_the_receipt_has_no_rung(tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_TIME_SCOPE", "false")
    node = _ladder(tmp_path, [1, 2, 42])
    _body, receipt, prompts = _asked(node, LATELY)
    assert "time_scope_rung_days" not in receipt and len(prompts[0].item_lines) == 3
