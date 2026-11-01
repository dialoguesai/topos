"""NR179 G2: with `ANSWER_JUDGE_RESERVE_SECONDS` set, a job short of time ends with a shorter answer, not `deadline`.

Seen on the owner's node on 15 Oct (1.5.9): one job took 116 s against the 110 s deadline, 82.6 s of it in three
support-judge calls. The closure only refused to START a call after the deadline, so a call started just before it
ran on, and the job ended `deadline` with nothing for the asker. Pinned here, on a stub clock and a stub judge (no
model is called):

- 0, the default, bound and unbound: exactly 1.5.9 (the judge gets the job's deadline, a job past it ends
  `deadline`, the receipt has no new counter);
- set: the value is read within [6, 60] (WS0 review: 110 or more had judged nothing and dropped every answer);
  no judge call starts with less than the reserve left; the sentences not judged are dropped and counted
  (`dropped_unjudged`), and the job ends `answered` with the sentences already kept, or `all_sentences_dropped`;
- set: each call is told to end by the deadline less the margin, and is cut off there whatever the host does; its
  sentence and the rest count as unjudged;
- the judge only drops: an unjudged sentence is never kept.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_release as ar
from topos.permissions_v2.answer_generation import (JudgeOutOfTime, Sieved, build_prompt, close_answer,
                                                    judge_sentences, post_check_answer)
from topos.permissions_v2.canonical import PolicyError

pytestmark = pytest.mark.public

P = "TOPOS_PERMISSIONS_V2_"
RESERVE = P + "ANSWER_JUDGE_RESERVE_SECONDS"
#: Three sentences the deterministic checks keep on the harness's one invented item.
THREE = ("The owner is working on a synthetic task [1].\n"
         "The owner keeps a synthetic task going [1].\n"
         "The owner writes about a synthetic task [1].")


@pytest.fixture(autouse=True)
def judge_on_and_no_reserve(monkeypatch):
    # 1.5.10's default is an 8 s reserve (owner decision D1); the tests below set it, or 0 for 1.5.9's no reserve.
    monkeypatch.setenv(RESERVE, "0")
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")


def _run(legacy, tmp_path, monkeypatch, judge, answer=THREE, *, writer_seconds=0, wait=10.0):
    """One ask through the real service, its clock the harness's stub clock. The writer takes `writer_seconds` of
    that clock; `judge` may move it too. Returns (body, receipt, accepted_at)."""
    from tests.permissions_v2.message_search_harness import owner
    from tests.permissions_v2.test_answer_release import _ask, _fetch, _service
    from tests.permissions_v2.test_knowledge_search import node_for
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, answer)

    async def generate(_prompt, *, deadline):
        node.now[0] += writer_seconds
        return answer

    service.generate = generate
    service.judge = lambda *args, **kwargs: judge(node, *args, **kwargs)
    accepted = node.now[0]
    try:
        answer_id = _ask(node, service, "Synthetic message")
        # The stub clock jumps while the job runs, so the fetch is signed and checked only once the job has ended
        # (a fetch envelope made before a jump would read as expired after it).
        started = time.monotonic()
        while service._jobs[answer_id].state != "ended":
            assert time.monotonic() - started < wait, "the answer job did not finish"
            time.sleep(0.01)
        body = _fetch(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?", ("answer-1",)).fetchone()
    finally:
        service.close()
    return body, json.loads(receipt[0]), accepted


def _judge(calls, *, seconds=0, verdicts=None):
    """A stub judge: each call moves the job's clock by `seconds` and says the next verdict (yes by default)."""
    answers = list(verdicts or [])

    async def judge(node, sentence, items, *, deadline, question=None, context=None):
        calls.append((sentence, deadline))
        node.now[0] += seconds
        return answers.pop(0) if answers else True
    return judge


# -- off (0, set by the fixture): 1.5.9 ---------------------------------------------------------------------------------

def test_off_a_judge_that_runs_past_the_deadline_ends_the_job_as_deadline_as_in_1_5_9(legacy, tmp_path, monkeypatch):
    calls = []
    body, receipt, accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls, seconds=40))
    assert [deadline for _sentence, deadline in calls] == [accepted + ar.MAX_END_SECONDS] * 3   # the job's own end
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"} and receipt["reason"] == "deadline"
    assert "dropped_unjudged" not in receipt["sentences"]


def test_off_the_receipt_of_an_answer_in_time_is_1_5_9s(legacy, tmp_path, monkeypatch):
    calls = []
    body, receipt, accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls))
    assert body["outcome"] == "answered" and receipt["reason"] == "answered" and len(calls) == 3
    sentences = receipt["sentences"]
    assert sentences["kept"] == 3 and sentences["dropped_support"] == 0 and "dropped_unjudged" not in sentences


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_the_reserve_is_eight_seconds_bound_and_unbound_with_nothing_set(bound, monkeypatch):
    """1.5.10's default (owner decision D1, 16 Oct: "8 seconds")."""
    from topos.permissions_v2 import switches
    monkeypatch.delenv(RESERVE, raising=False)
    monkeypatch.setattr(switches, "is_bound", lambda env=None: bound)
    assert switches.number(switches.ANSWER_JUDGE_RESERVE_SECONDS) == 8 and ar.judge_reserve() == 8
    assert ar.JUDGE_RELEASE_MARGIN_SECONDS == 5


# -- set: a shorter answer instead of none ---------------------------------------------------------------------------

def test_set_the_sentence_with_too_little_time_left_is_dropped_unjudged_and_the_rest_is_delivered(legacy, tmp_path,
                                                                                                 monkeypatch):
    monkeypatch.setenv(RESERVE, "35")
    calls = []
    body, receipt, accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls, seconds=40))
    # Before the third call 30 s are left, under the reserve: two calls, each told to end 5 s before the deadline.
    assert [deadline for _sentence, deadline in calls] == [accepted + ar.MAX_END_SECONDS - 5] * 2
    assert body["outcome"] == "answered" and len(body["answer"].split("\n")) == 2
    assert receipt["reason"] == "answered" and receipt["outcome"] == "answered"
    sentences = receipt["sentences"]
    assert sentences["generated"] == 3 and sentences["kept"] == 2
    assert sentences["dropped_unjudged"] == 1 and sentences["dropped_support"] == 0


def test_set_every_sentence_after_the_reserve_is_dropped_not_only_the_next(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(RESERVE, "60")
    calls = []
    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls, seconds=55))   # 55 s left after one
    assert len(calls) == 1 and body["outcome"] == "answered"
    assert receipt["sentences"]["kept"] == 1 and receipt["sentences"]["dropped_unjudged"] == 2


def test_set_with_nothing_kept_the_job_ends_all_sentences_dropped_not_deadline(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(RESERVE, "35")
    calls = []
    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls, seconds=40, verdicts=[False, False]))
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["reason"] == "all_sentences_dropped"
    assert receipt["sentences"]["dropped_support"] == 2 and receipt["sentences"]["dropped_unjudged"] == 1


def test_set_a_reserve_longer_than_sixty_seconds_is_read_as_sixty(legacy, tmp_path, monkeypatch):
    """WS0 review: 120 used to judge nothing and drop every answer. Read as 60, two calls of 30 s start (110 and 80 s
    left) and the third (50 s left) does not."""
    monkeypatch.setenv(RESERVE, "120")
    calls = []
    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls, seconds=30))
    assert len(calls) == 2 and body["outcome"] == "answered" and receipt["reason"] == "answered"
    assert receipt["sentences"]["kept"] == 2 and receipt["sentences"]["dropped_unjudged"] == 1


@pytest.mark.parametrize("raw, read", [(None, 8), ("0", 0), ("1", 6), ("5", 6), ("6", 6), ("8", 8), ("10", 10),
                                       ("60", 60), ("61", 60), ("120", 60), ("twenty", 8)])
def test_the_reserve_is_off_or_read_within_six_and_sixty(raw, read, monkeypatch):
    if raw is None:
        monkeypatch.delenv(RESERVE, raising=False)
    else:
        monkeypatch.setenv(RESERVE, raw)
    assert ar.judge_reserve() == read
    assert ar.JUDGE_RESERVE_MIN_SECONDS == 6 and ar.JUDGE_RESERVE_MAX_SECONDS == 60


def test_set_with_the_judge_off_there_is_no_judge_call_and_no_unjudged_counter(legacy, tmp_path, monkeypatch):
    """WS0 review: the counter rides on the receipt only while the judge itself is on (1.5.6's receipt when it is off)."""
    monkeypatch.setenv(RESERVE, "8")
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "false")
    calls = []
    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls))
    assert calls == [] and body["outcome"] == "answered"
    assert "dropped_unjudged" not in receipt["sentences"] and "dropped_support" not in receipt["sentences"]


def test_set_a_reserve_under_the_margin_acts_as_the_margin_plus_a_second(legacy, tmp_path, monkeypatch):
    """With 5 s left a call would have no time before its own end (deadline - 5): it is never started."""
    monkeypatch.setenv(RESERVE, "1")
    calls = []
    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls), writer_seconds=ar.MAX_END_SECONDS - 5)
    assert calls == [] and receipt["reason"] == "all_sentences_dropped"
    assert receipt["sentences"]["dropped_unjudged"] == 3


def test_set_a_reserve_under_the_margin_still_judges_with_six_seconds_left(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(RESERVE, "1")
    calls = []
    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, _judge(calls), writer_seconds=ar.MAX_END_SECONDS - 6)
    assert len(calls) == 3 and receipt["reason"] == "answered" and receipt["sentences"]["dropped_unjudged"] == 0


def test_set_a_call_still_running_at_its_bound_is_cut_off_and_its_sentence_is_unjudged(legacy, tmp_path, monkeypatch):
    """Six seconds left: the first call may run one second (to the deadline less the margin). The stub host takes
    eight; the call is cut off after one, and it and the two after it are dropped unjudged."""
    monkeypatch.setenv(RESERVE, "1")
    calls, finished = [], []

    async def slow(node, sentence, items, *, deadline, question=None, context=None):
        calls.append(sentence)
        await asyncio.sleep(8)
        finished.append(sentence)
        return True

    started = time.monotonic()
    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, slow, writer_seconds=ar.MAX_END_SECONDS - 6,
                                    wait=20.0)
    assert time.monotonic() - started < 6.0 and len(calls) == 1 and finished == []
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["reason"] == "all_sentences_dropped"
    assert receipt["sentences"]["dropped_unjudged"] == 3 and receipt["sentences"]["dropped_support"] == 0


def test_set_a_judge_provider_error_stops_the_phase(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(RESERVE, "20")

    calls = []

    async def failing(node, sentence, items, *, deadline, question=None, context=None):
        calls.append(sentence)
        raise PolicyError("model_error")

    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, failing)
    assert receipt["reason"] == "judge_unavailable"
    assert len(calls) == 1
    assert receipt["sentences"]["dropped_unjudged"] == 0


def test_set_a_judge_host_that_is_down_is_still_its_own_reason(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(RESERVE, "20")

    async def down(node, sentence, items, *, deadline, question=None, context=None):
        raise PolicyError("model_unavailable")

    body, receipt, _accepted = _run(legacy, tmp_path, monkeypatch, down)
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["reason"] == "judge_unavailable"


# -- the judge phase itself -------------------------------------------------------------------------------------------

class Open:
    def mentions_protected(self, *texts):
        return False


def _three_sieved():
    return Sieved(3, ("One [1].", "Two [1].", "Three [1]."), 0, 0, 0, 0)


def test_judge_sentences_drops_the_rest_unjudged_and_keeps_only_closed_yeses():
    from tests.permissions_v2.test_answer_quality_aq158 import TOMATOES
    prompt = build_prompt("What has the owner been doing?", [TOMATOES], precision="none")
    seen = []

    def support(sentence, items, question, **_context):
        seen.append(sentence)
        if sentence.startswith("Two"):
            raise JudgeOutOfTime()
        return True

    sieved, drops = judge_sentences(_three_sieved(), prompt, support)
    assert seen == ["One [1].", "Two [1]."] and sieved.sentences == ("One [1].",)
    assert drops == 0 and sieved.dropped_unjudged == 2


def test_without_a_reserve_nothing_is_ever_unjudged():
    from tests.permissions_v2.test_answer_quality_aq158 import TOMATOES
    prompt = build_prompt("What has the owner been doing?", [TOMATOES], precision="none")
    sieved, drops = judge_sentences(_three_sieved(), prompt, lambda *a, **k: False)
    assert drops == 3 and sieved.dropped_unjudged == 0 and sieved.sentences == ()
    assert judge_sentences(_three_sieved(), prompt, None)[0].dropped_unjudged == 0


def test_the_body_close_carries_the_unjudged_count():
    from tests.permissions_v2.test_answer_quality_aq158 import TOMATOES
    prompt = build_prompt("What has the owner been doing?", [TOMATOES], precision="none")

    def out_of_time(*_args, **_kwargs):
        raise JudgeOutOfTime()

    checked = post_check_answer("The owner re-potted tomatoes and fixed the bike chain [1].", [TOMATOES], prompt,
                                mode="only", boundary=Open(), support=out_of_time)
    assert checked.body.outcome == "no_answer" and checked.reason == "all_sentences_dropped"
    assert checked.dropped_unjudged == 1 and checked.dropped_support == 0 and checked.kept == 0
    sieved, _drops = judge_sentences(_three_sieved(), prompt, out_of_time)
    assert close_answer(sieved, [TOMATOES], prompt, mode="only", boundary=Open()).dropped_unjudged == 3
