"""verify-claim effort high: the interpreter alone (guards, examples, the two model calls, capacity). No model runs:
the pinned model is replaced by a scripted chat, and the transport by a scripted host."""
import io
import json
from pathlib import Path
import threading
import time

import pytest

from topos.permissions_v2 import truth_interpret as ti
from topos.permissions_v2.truth_contract import BY_ID


class Chat:
    """Scripted pinned model: answers in order, and records every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, system, user, schema, timeout):
        self.calls.append({"system": system, "user": user, "schema": schema, "timeout": timeout})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


EXAMPLES = ti.Examples([("My go-to drink is tea.", "drink=tea", None),
                        ("I think tea is my drink.", "NONE", "hedge_or_modality"),
                        ("I'm a night owl.", "time=evening", None),
                        ("Rowan prefers tea.", "NONE", "other_subject")])


def interpreter(*answers, **kwargs):
    chat = Chat(*answers)
    return ti.Interpreter(chat, EXAMPLES, **kwargs), chat


@pytest.mark.parametrize("statement, reason", [
    ("", "vague_or_no_topic"),
    ("x" * 281, "too_long"),
    ("Is tea my go-to drink?", "question"),
    ("Is tea my go-to drink", "question"),
    ("Oh sure, I'm a total morning person", "hedge_or_modality"),
    ("Tea is my drink. Coffee too", "compound"),
    ("Tea is the best drink", "vague_or_no_topic"),
    ("Rowan prefers tea, like me", "other_subject"),
    ("My sister is a morning person, like me", "other_subject"),
    ("We plan ahead in my house", "other_subject"),
    ("I never drink coffee", "negation"),
    ("I probably prefer tea", "hedge_or_modality"),
    ("This weekend I want to stay home", "hedge_or_modality"),
    ("My ideal weekend this month is spent outdoors", "hedge_or_modality"),       # one occasion, any unit
    ("At that wedding, I recharged with people", "hedge_or_modality"),
    ("Evening is when I feel most like myself, plus I play the drums", "compound"),
    ("I always plan ahead", "hedge_or_modality"),
    ("I recharge with a crowd around me on most occasions", "hedge_or_modality"),
    ("Coffee is my go-to drink for the most part", "hedge_or_modality"),
    ("I recharge alone or with friends", "compound"),
    ("I love mornings but evenings are fine too", "compound"),
    ("I prefer planning ahead, and I have a big family", "compound"),
    ("I'm an introvert at heart", "related_not_equivalent"),
    ("Tea for me, thanks", "related_not_equivalent"),
    ("I'm really fond of tea", "related_not_equivalent"),                 # liking is weaker than an answer
    ("I enjoy quiet time by myself", "related_not_equivalent"),
    ("I love a long walk outdoors", "related_not_equivalent"),
    ("My ideal weekend is a hike with a big group of mates and their friends", "ambiguous_multiple_topics"),
    ("Coffee in the morning is my thing", "ambiguous_multiple_topics"),
    ("Quiet evenings alone are my favorite", "ambiguous_multiple_topics"),        # a question's answer words count
    ("A weekend outside with friends is my favorite", "ambiguous_multiple_topics"),
])
def test_guards_refuse_before_any_model_call(statement, reason):
    interp, chat = interpreter()
    reading = interp.interpret(statement)
    assert (reading.accepted, reading.stage, reading.reason) == (False, "guard", reason)
    assert chat.calls == []


@pytest.mark.parametrize("statement", [
    "Tea beats coffee for me, no contest", "Neither coffee nor tea is my drink of choice",
    "Whether I'm a morning or an evening person depends on the day", "A cozy weekend indoors is my ideal",
    "Hands down, I'm a planner", "I recharge by myself", "I like to play it by ear", "I love tea more than coffee",
    "I enjoy my weekends best at home", "I love recharging on my own"])
def test_guards_pass_plain_preferences_and_idioms(statement):
    assert ti.guard(statement) is None


def test_two_agreeing_calls_accept_and_the_examples_show_refusals_first():
    interp, chat = interpreter({"option": "drink=tea"}, {"answer": "tea"})
    reading = interp.interpret("A pot of tea is my everyday drink")
    assert (reading.question_id, reading.value, reading.stage, reading.reason) == ("drink", "tea", "model_b", None)
    first, second = chat.calls
    assert first["system"] == ti.SYSTEM_A and first["schema"]["properties"]["option"]["enum"] == ti.CODES
    assert len(ti.CODES) == 16 and ti.CODES[-1] == "NONE"
    shots = first["user"].split("\n\n")[0]
    assert shots.index("NONE (hedged") < shots.index("Answer: drink=tea")       # refusals before accepts
    assert "NONE (about someone else)" in shots
    assert first["user"].endswith("Sentence: A pot of tea is my everyday drink\nAnswer:")
    assert second["system"] == ti.SYSTEM_B and second["schema"]["properties"]["answer"]["enum"] == [
        "coffee", "tea", "neither", "none"]
    assert "Quiz question: What is your go-to drink?" in second["user"]
    assert 0 < second["timeout"] <= first["timeout"] <= ti.DEADLINE_SECONDS


@pytest.mark.parametrize("answers, stage, reason, calls", [
    ([{"option": "NONE"}], "model_a", "model_none", 1),
    ([{"option": "drink=lemonade"}], "model_a", "malformed_model_output", 1),
    ([{}], "model_a", "malformed_model_output", 1),
    ([ti.ModelUnavailable("model_unreviewed")], "model_a", "model_unreviewed", 1),
    ([RuntimeError("socket")], "model_a", "model_error", 1),
    ([{"option": "drink=tea"}, {"answer": "coffee"}], "model_b", "verification_disagreed", 2),
    ([{"option": "drink=tea"}, {"answer": "none"}], "model_b", "verification_disagreed", 2),
    ([{"option": "drink=tea"}, ti.ModelUnavailable("deadline")], "model_b", "deadline", 2),
    ([{"option": "weekend=outdoors"}], "anchor", "missing_topic_anchor", 1),
])
def test_every_other_outcome_refuses(answers, stage, reason, calls):
    interp, chat = interpreter(*answers)
    reading = interp.interpret("A pot of tea is my everyday drink")
    assert (reading.accepted, reading.stage, reading.reason, len(chat.calls)) == (False, stage, reason, calls)


@pytest.mark.parametrize("statement, anchored", [
    ("It varies for me", False), ("That one suits me", False),
    ("When I feel most like myself shifts around", True), ("My most-me time of day is a moving target", True),
    ("I'm at my best after dark", True), ("Mornings are when I'm the truest version of me", True)])
def test_the_time_anchor_needs_a_time_cue_or_the_questions_own_frame(statement, anchored):
    assert ti.anchored("time", statement) is anchored
    interp, chat = interpreter({"option": "time=it-varies"}, {"answer": "it-varies"})
    reading = interp.interpret(statement)
    assert reading.accepted is anchored and len(chat.calls) == (2 if anchored else 1)


def test_a_mixed_answer_that_names_another_questions_answer_is_refused():
    assert ti.guard("A balance of outdoors and solitude is how I recharge") == "ambiguous_multiple_topics"
    interp, chat = interpreter({"option": "company=a-bit-of-both"})
    reading = interp.interpret("A mix of outdoors and quiet is how I unwind")     # one family named, two mixed
    assert (reading.stage, reading.reason, reading.proposed, len(chat.calls)) == (
        "anchor", "mixed_across_questions", "company=a-bit-of-both", 1)
    interp, chat = interpreter({"option": "company=a-bit-of-both"}, {"answer": "a-bit-of-both"})
    assert interp.interpret("A balance of solitude and friends is how I recharge").accepted


def test_the_deadline_is_shared_by_both_calls():
    interp, chat = interpreter({"option": "drink=tea"}, {"answer": "tea"}, deadline_s=0.0)
    chat.answers = [ti.ModelUnavailable("deadline")]
    assert interp.interpret("A pot of tea is my everyday drink").reason == "deadline"
    assert chat.calls[0]["timeout"] <= 0


def test_the_reading_says_what_the_player_is_shown():
    reading = ti.Reading("time", "it-varies", "model_b", None)
    assert reading.interpretation() == {"pack_id": "icebreakers-v1", "question_id": "time", "value": "it-varies",
                                        "proposition": "When you feel most like yourself varies."}
    assert [a["value"] for a in reading.alternatives()] == ["morning", "evening"]
    assert ti.proposition("drink", "neither") == "Your go-to drink is neither coffee nor tea."
    assert ti.proposition("company", "with-people") == "You recharge with people."
    assert set(ti.Reading(None, None, "guard", "negation", {"guard": 0.1}).diagnostics()) == {
        "stage", "reason", "proposed", "ms", "interpreter"}


def test_the_quiz_is_the_contracts():
    for question, (_prompt, choices) in ti.QUESTIONS.items():
        assert list(choices) == BY_ID[question]["choices"]
        for value in choices:
            assert f"{question}={value}" in ti.SYSTEM_A


def test_the_node_ships_no_examples_and_sends_the_sentence_alone():
    chat = Chat({"option": "drink=tea"}, {"answer": "tea"})
    assert ti.Interpreter(chat).examples is None                     # owner, 20 Oct 2026: no few-shot file in the repo
    assert ti.Interpreter(chat).interpret("A pot of tea is my everyday drink").accepted
    assert chat.calls[0]["user"] == "Sentence: A pot of tea is my everyday drink\nAnswer:"
    assert not list(Path(ti.__file__).parent.glob("truth_interpret_examples*"))


def test_given_examples_refusals_come_first_and_each_kind_is_capped():
    rows = [(f"I prefer tea number {n}", "drink=tea", None) for n in range(8)]
    rows += [(f"I think I prefer tea number {n}", "NONE", "hedge_or_modality") for n in range(8)]
    nearest = ti.Examples(rows).nearest("I prefer tea", k=3)
    assert len(nearest) == 6 and all(c.startswith("NONE (hedged") for _, c in nearest[:3])
    assert all(c == "drink=tea" for _, c in nearest[3:])
    assert all(s != "I prefer tea number 0" for s, _ in ti.Examples(rows).nearest("x", exclude="I prefer tea number 0"))


class Host:
    """A scripted model host for PinnedChat: /api/tags and /api/chat."""

    def __init__(self, *, digest=None, reply=None, fail=None):
        from topos.permissions_v2.shadow_labeler_local import MODEL, MODEL_REVISION
        self.tags = {"models": [{"name": MODEL, "digest": digest or MODEL_REVISION}]}
        self.reply = reply if reply is not None else {"model": MODEL, "done": True,
                                                      "message": {"content": json.dumps({"option": "drink=tea"})}}
        self.fail, self.requests = fail, []

    def open(self, request, timeout):
        self.requests.append((request.full_url, json.loads(request.data) if request.data else None, timeout))
        if request.full_url.endswith("/api/chat") and self.fail:
            raise self.fail
        body = self.tags if request.full_url.endswith("/api/tags") else self.reply
        return io.BytesIO(json.dumps(body).encode())


def pinned(host, **kwargs):
    chat = ti.PinnedChat("http://127.0.0.1:11434", **kwargs)
    chat._opener = host
    return chat


def test_the_transport_asks_the_pinned_model_for_a_schema_answer_and_leaves_the_context_to_the_host():
    host = Host()
    assert pinned(host)("system", "user", {"type": "object"}, 3.0) == {"option": "drink=tea"}
    (_, tags, _), (url, body, timeout) = host.requests
    assert url.endswith("/api/chat") and body["format"] == {"type": "object"} and body["think"] is False
    assert body["options"] == {"temperature": 0, "seed": ti.SEED} and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"] and timeout == 3.0
    pinned(host, num_ctx=4096)("s", "u", {}, 1.0)
    assert host.requests[-1][1]["options"]["num_ctx"] == 4096


@pytest.mark.parametrize("host, reason", [
    (Host(digest="0" * 64), "model_unreviewed"),
    (Host(reply={"model": "another:9b", "done": True, "message": {"content": "{}"}}), "model_error"),
    (Host(reply={"done": False}), "model_error"),
    (Host(reply=None, fail=TimeoutError()), "model_error"),
])
def test_the_transport_refuses_anything_but_the_reviewed_model_finishing(host, reason):
    with pytest.raises(ti.ModelUnavailable, match=reason):
        pinned(host)("s", "u", {}, 1.0)


def test_the_transport_refuses_an_answer_that_is_not_json_and_a_spent_deadline():
    from topos.permissions_v2.shadow_labeler_local import MODEL
    host = Host(reply={"model": MODEL, "done": True, "message": {"content": "tea"}})
    with pytest.raises(ti.ModelUnavailable, match="malformed_model_output"):
        pinned(host)("s", "u", {}, 1.0)
    with pytest.raises(ti.ModelUnavailable, match="deadline"):
        pinned(Host())("s", "u", {}, 0)


def test_the_capacity_is_one_in_flight_and_a_short_queue():
    capacity = ti.Capacity(in_flight=1, waiting=1)
    entered, release = threading.Event(), threading.Event()

    def hold():
        with capacity.slot(1.0):
            entered.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    entered.wait(5)
    with pytest.raises(ti.Busy):            # the one waiting place times out
        with capacity.slot(0.05):
            pass
    waiting = threading.Thread(target=lambda: capacity.slot(2.0).__enter__())
    waiting.start()
    time.sleep(0.05)
    with pytest.raises(ti.Busy):            # queue full: refused at once
        with capacity.slot(2.0):
            pass
    release.set()
    holder.join(5)
    waiting.join(5)


def test_each_grant_has_an_hourly_allowance():
    now = [1000.0]
    allowance = ti.Allowance(per_hour=2, clock=lambda: now[0])
    allowance.take("grant-a")
    allowance.take("grant-a")
    allowance.take("grant-b")
    with pytest.raises(ti.Busy, match="interpret_allowance"):
        allowance.take("grant-a")
    now[0] += 3601
    allowance.take("grant-a")


def test_a_guard_refusal_waits_for_no_slot(monkeypatch):
    monkeypatch.setattr(ti, "CAPACITY", ti.Capacity(in_flight=1, waiting=0))
    interp, chat = interpreter()
    with ti.CAPACITY.slot(0.1):
        assert ti.read("I never drink coffee", interpreter=interp).reason == "negation"
        with pytest.raises(ti.Busy):
            ti.read("A pot of tea is my everyday drink", interpreter=interp, wait_s=0.01)
    assert chat.calls == []
