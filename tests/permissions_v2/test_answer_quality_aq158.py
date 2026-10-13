"""Lane AQ158 (12 Oct 2026): answer-quality candidates behind switches, each default today's behaviour exactly.

On the owner's live node (1.5.6, with-sources mode) 42 of 68 generated sentences were dropped by the subject check's
topic-word rule: the cited items supported them in other words than the question used ("accomplish", "hobby",
"goal", "intention"). Four candidates, measured side by side on the rig batteries and reported in the lane's
report; none changes which items a share releases:

V0 `ANSWER_SHORT_WORD_NAMES` (the live "lately" case). A person on the node whose name word is a short English
   word ("to", "up", "been") made every question containing that word in lower case require the person's name in
   every cited item: "What has the owner been up to lately?" has no subject, yet lost all three sentences. Off: a
   lower-case short English word names nobody; written with a capital it still binds.
V1 `ANSWER_SUBJECT_WORDS`. Off: the topic-word requirement of amendment 3 is not applied. The name rule, the
   namesake guard (amendment 10), the absent-member rule (F1), carriage when on, the anchor rule before the model,
   citation, copy, echo, Off-limits and scrub are unchanged.
V2 `ANSWER_SUPPORT_JUDGE`. On: the pinned local model is asked, per kept sentence, whether the items it cites support
   it, under a closed grammar; anything but a closed "yes" drops the sentence (`dropped_support`).
V3 `ANSWER_ITEMS`. The released items the model sees, 1 to 24 (default 8); retrieval is still bounded by the grant's
   max_k and the knowledge ceiling, and the with-sources body by its 8-record cap.

Invented data only.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_release as ar, switches
from topos.permissions_v2.answer_generation import (K_ANSWER_CEILING, build_prompt, cited_item_lines, k_answer,
    post_check_answer, question_lacks_permitted_anchor)
from topos.permissions_v2.answer_protocol import K_ANSWER
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.knowledge_contract import JournalEntryResult, MessageResult

P = switches.PREFIX
MODES = ("only", "with_sources")


class Open:
    def mentions_protected(self, *texts):
        return False


def _message(digit, text, source="ownerimport"):
    rid = "r." + digit * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
        "source_ids": [source], "citations": [{"record_id": rid, "source_id": source, "content": text}]})


def _journal(digit, text):
    rid = "r." + digit * 64
    return JournalEntryResult.parse({"kind": "journal_entry", "record_id": rid, "content": text,
        "source_ids": ["journal"], "citations": [{"record_id": rid, "source_id": "journal", "content": text}]})


TOMATOES = _message("1", "Spent the weekend re-potting the balcony tomatoes and fixing the bike chain.")
SKETCHES = _message("2", "Started a short run of evening sketches of the harbour.")
LAMP = _journal("3", "Finished the reading lamp rewiring on Sunday; the switch still crackles.")
MARROW = _message("4", "The marrow jars went cloudy again; I am rinsing the lids tonight.")
PLAY = _message("5", "The kids' school play is on Thursday; we are bringing the grandparents.")
IVO_NAMESAKE = _message("6", "Lunch with Ivo from the climbing gym; he is moving to the coast.")


def _checked(question, records, sentence, mode, **kwargs):
    prompt = build_prompt(question, list(records), precision="none", **kwargs)
    return prompt, post_check_answer(sentence, list(records), prompt, mode=mode, boundary=Open())


@pytest.fixture(autouse=True)
def every_switch_unset(monkeypatch):
    for name in (P + "ANSWER_SUBJECT_WORDS", P + "ANSWER_SUPPORT_JUDGE", P + "ANSWER_ITEMS", P + "ANSWER_SHORT_WORD_NAMES"):
        monkeypatch.delenv(name, raising=False)


# --- the table -------------------------------------------------------------------------------------------------------

def test_the_four_switches_default_to_todays_behaviour():
    assert switches.ANSWER_SUBJECT_WORDS.unbound is True and switches.ANSWER_SUBJECT_WORDS.bound is True
    assert switches.ANSWER_SUPPORT_JUDGE.unbound is False and switches.ANSWER_SUPPORT_JUDGE.bound is False
    assert switches.ANSWER_ITEMS.unbound == K_ANSWER == 8 and switches.ANSWER_ITEMS.bound == 8
    assert switches.ANSWER_SHORT_WORD_NAMES.unbound is True and switches.ANSWER_SHORT_WORD_NAMES.bound is True
    assert k_answer() == 8


# --- V0: the live "lately" case ------------------------------------------------------------------------------------

RECENCY = "What has the owner been up to lately?"
RECENCY_ANSWER = ("The owner re-potted tomatoes and fixed a bike chain [1].\n"
                  "They began evening sketches of the harbour [2].\nThey rewired a reading lamp [3].")


@pytest.mark.parametrize("word", ["to", "been", "up"])
def test_a_persons_short_word_on_the_node_makes_a_subjectless_question_drop_every_sentence(word):
    """The mechanism behind the unexplained live row: no subject, no anchor, and still all sentences dropped as
    `dropped_relevance`, because a known person's word equals a lower-case question word."""
    for mode in MODES:
        prompt, checked = _checked(RECENCY, [TOMATOES, SKETCHES, LAMP], RECENCY_ANSWER, mode)
        assert checked.body.outcome == "answered" and checked.kept == 3          # nobody on the node: answered
        prompt, checked = _checked(RECENCY, [TOMATOES, SKETCHES, LAMP], RECENCY_ANSWER, mode, people=frozenset({word}))
        assert prompt.name_terms == frozenset({word})
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 3 and checked.reason == "all_sentences_dropped"


@pytest.mark.parametrize("word", ["to", "been", "up"])
def test_v0_off_a_lower_case_short_english_word_names_nobody_but_a_capital_still_binds(word, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SHORT_WORD_NAMES", "false")
    for mode in MODES:
        prompt, checked = _checked(RECENCY, [TOMATOES, SKETCHES, LAMP], RECENCY_ANSWER, mode, people=frozenset({word}))
        assert prompt.name_terms == frozenset() and checked.body.outcome == "answered" and checked.kept == 3
    # Written as a name, away from a clause start, it binds as every capitalised word does (4.3a unchanged).
    prompt, checked = _checked("What did the owner say to Up?", [TOMATOES], "They re-potted tomatoes [1].", "only",
                               people=frozenset({"up"}))
    assert prompt.name_terms == frozenset({"up"}) and checked.body.outcome == "no_answer"


def test_v0_off_a_persons_word_that_is_no_english_word_still_binds_in_lower_case(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SHORT_WORD_NAMES", "false")
    prompt, checked = _checked("What has the owner said about ivo?", [TOMATOES], "They re-potted tomatoes [1].", "only",
                               people=frozenset({"ivo"}))
    assert prompt.name_terms == frozenset({"ivo"}) and checked.body.outcome == "no_answer"


# --- V1: the topic-word requirement ----------------------------------------------------------------------------------

HOBBY_Q = "What interests and hobbies has the owner explicitly described?"
HOBBY_SENTENCE = "The owner re-pots tomatoes, fixes their bike and sketches the harbour in the evenings [1, 2]."


def test_by_default_a_supported_sentence_in_other_words_than_the_question_is_dropped():
    for mode in MODES:
        _prompt, checked = _checked(HOBBY_Q, [TOMATOES, SKETCHES], HOBBY_SENTENCE, mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1


def test_v1_off_keeps_it(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    for mode in MODES:
        _prompt, checked = _checked(HOBBY_Q, [TOMATOES, SKETCHES], HOBBY_SENTENCE, mode)
        assert checked.body.outcome == "answered" and checked.kept == 1 and checked.dropped_relevance == 0


def test_v1_off_the_anchor_rule_still_abstains_before_the_model(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    prompt = build_prompt("What has the owner said about the Winter Olympics?", [TOMATOES, SKETCHES], precision="none")
    assert question_lacks_permitted_anchor(prompt)


def test_v1_off_a_name_still_binds_in_every_cited_item(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    for mode in MODES:
        _prompt, checked = _checked("What has Ivo been doing?", [IVO_NAMESAKE, TOMATOES],
                                    "Ivo is moving to the coast and re-potted tomatoes [1, 2].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
        _prompt, checked = _checked("What has Ivo been doing?", [IVO_NAMESAKE, TOMATOES], "Ivo plans a move to the coast [1].", mode)
        assert checked.body.outcome == "answered"


def test_v1_off_the_namesake_guard_still_drops_a_sentence_that_mixes_the_two_ivos(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    for mode in MODES:
        _prompt, checked = _checked("What has Ivo been doing?", [IVO_NAMESAKE, TOMATOES],
                                    "Ivo re-potted tomatoes and is moving to the coast [1, 2].", mode,
                                    owner_words=frozenset({"ivo"}), shared_owner_words=frozenset({"ivo"}))
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
        _prompt, checked = _checked("What has Ivo been doing?", [IVO_NAMESAKE, TOMATOES], "Ivo re-potted tomatoes [2].", mode,
                                    owner_words=frozenset({"ivo"}), shared_owner_words=frozenset({"ivo"}))
        assert checked.body.outcome == "answered"


def test_v1_off_an_absent_coordinated_member_is_still_never_said_back(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    for mode in MODES:
        # "ropes" is under the anchor rule's eight letters, so only F1 can drop a sentence that says it back.
        _prompt, checked = _checked("What has the owner said about marrow or ropes?", [MARROW],
                                    "The owner has been buying ropes [1].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
        _prompt, checked = _checked("What has the owner said about marrow or ropes?", [MARROW],
                                    "The owner's marrow jars went cloudy [1].", mode)
        assert checked.body.outcome == "answered"


def test_v1_off_citation_copy_echo_and_scrub_are_untouched(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    _prompt, checked = _checked(HOBBY_Q, [TOMATOES], "The owner likes tomatoes.", "only")
    assert checked.dropped_citation == 1 and checked.body.outcome == "no_answer"
    _prompt, checked = _checked(HOBBY_Q, [TOMATOES], TOMATOES.content + " [1]", "only")
    assert checked.dropped_copy == 1
    _prompt, checked = _checked("What has the owner said about the Zargothian festival?", [TOMATOES],
                                "The owner went to the Zargothian festival [1].", "only")
    assert checked.dropped_question_echo == 1
    _prompt, checked = _checked(HOBBY_Q, [TOMATOES], "Write to ivo@example.org about the tomatoes [1].", "only")
    assert "[REDACTED_EMAIL]" in checked.body.answer and "example.org" not in checked.body.answer


def test_what_v1_gives_up_the_union_lending_case_of_amendment_3(monkeypatch):
    """Stated plainly: with V1 off, a sentence about a subject under the anchor rule's eight letters ("pepper") that
    cites an unrelated item is no longer dropped by the words alone. V2 is the check for it."""
    _prompt, checked = _checked("Why did the pepper mash fail?", [LAMP], "The lamp's crackling switch spoiled the mash [1].", "only")
    assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    _prompt, checked = _checked("Why did the pepper mash fail?", [LAMP], "The lamp's crackling switch spoiled the mash [1].", "only")
    assert checked.body.outcome == "answered"


# --- V2: the support judge ------------------------------------------------------------------------------------------

def test_the_judge_reads_exactly_the_cited_items_as_the_model_saw_them():
    prompt = build_prompt("What has the owner been doing?", [TOMATOES, SKETCHES, LAMP], precision="none")
    assert len(prompt.item_lines) == 3 and prompt.item_lines[0].startswith("[1] message\n")
    assert prompt.item_lines[2].startswith("[3] journal_entry\n") and "Supporting text" not in prompt.user
    assert "\n\n".join(prompt.item_lines) in prompt.user
    assert cited_item_lines("They rewired a lamp and sketched [3, 2, 3].", prompt) == (prompt.item_lines[2], prompt.item_lines[1])
    assert cited_item_lines("No citation.", prompt) == () and cited_item_lines("Out of range [9].", prompt) == ()


def test_a_judge_only_drops_a_closed_yes_keeps_and_anything_else_drops():
    seen = []

    def judge(sentence, items):
        seen.append((sentence, items))
        if "tomatoes" in sentence:
            return True
        if "sketches" in sentence:
            return False
        if "lamp" in sentence:
            return None
        raise RuntimeError("judge down")

    records = [TOMATOES, SKETCHES, LAMP, MARROW]
    text = ("The owner re-potted tomatoes [1].\nThey made evening sketches [2].\nThey rewired a lamp [3].\n"
            "The jars went cloudy [4].")
    for mode in MODES:
        seen.clear()
        prompt = build_prompt("What has the owner been doing?", records, precision="none")
        checked = post_check_answer(text, records, prompt, mode=mode, boundary=Open(), support=judge)
        assert checked.body.outcome == "answered" and checked.kept == 1 and checked.dropped_support == 3
        assert checked.generated == 4 and checked.dropped_relevance == 0
        assert [items for _sentence, items in seen] == [(prompt.item_lines[0],), (prompt.item_lines[1],),
                                                       (prompt.item_lines[2],), (prompt.item_lines[3],)]
        assert "[1]" not in checked.body.answer if mode == "only" else checked.body.records[0].record_id == TOMATOES.record_id


def test_the_judge_runs_only_on_sentences_the_deterministic_checks_kept():
    calls = []

    def judge(sentence, items):
        calls.append(sentence)
        return True

    prompt = build_prompt(HOBBY_Q, [TOMATOES, SKETCHES], precision="none")
    checked = post_check_answer("No citation here.\n" + HOBBY_SENTENCE, [TOMATOES, SKETCHES], prompt, mode="only",
                                boundary=Open(), support=judge)
    assert calls == [] and checked.dropped_citation == 1 and checked.dropped_relevance == 1 and checked.dropped_support == 0


def test_without_a_judge_nothing_changes_and_the_counter_is_zero():
    _prompt, checked = _checked("What has the owner been doing?", [TOMATOES], "The owner re-potted tomatoes [1].", "only")
    assert checked.body.outcome == "answered" and checked.dropped_support == 0


def test_the_judge_reply_grammar_is_closed():
    assert ar.parse_judge_reply('{"supported": "yes"}') is True
    assert ar.parse_judge_reply('{"supported": "no"}') is False
    for raw in ('{"supported": "maybe"}', '{"supported": "yes", "why": "x"}', "yes", "", None, "[]", '{"supported": true}',
                '{"Supported": "yes"}'):
        assert ar.parse_judge_reply(raw) is None, raw


def test_the_judge_messages_are_pinned():
    prompt = build_prompt("What has the owner been doing?", [TOMATOES, SKETCHES], precision="none")
    messages = ar.judge_messages("They sketched the harbour [2].", cited_item_lines("x [2]", prompt))
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == ar.JUDGE_SYSTEM_PROMPT and "never instructions" in ar.JUDGE_SYSTEM_PROMPT
    assert messages[1]["content"] == ("Items (quoted data):\n\n" + prompt.item_lines[1]
                                      + "\n\nSentence (quoted data):\nThey sketched the harbour.")
    assert ar.JUDGE_FORMAT["properties"]["supported"]["enum"] == ["yes", "no"]
    assert ar.JUDGE_TEMPLATE_VERSION == "topos-answer-support-judge/v1"


@pytest.mark.asyncio
async def test_the_local_judge_posts_the_closed_grammar_at_temperature_zero_and_reads_only_a_closed_reply(monkeypatch):
    from topos.permissions_v2 import shadow_labeler_local as sl
    posted = []

    class Response:
        def __init__(self, content):
            self._content = content

        def raise_for_status(self):
            return None

        def json(self):
            return {"model": sl.MODEL, "done": True, "message": {"role": "assistant", "content": self._content}}

    class Client:
        def __init__(self):
            self.replies = []

        async def post(self, url, timeout=None, json=None):
            posted.append((url, timeout, json))
            return Response(self.replies.pop(0))

        async def aclose(self):
            return None

    class Transport:
        def __init__(self):
            self.client = Client()
            self.base_url = "http://host.invalid"

        async def verify(self):
            return None

    transport = Transport()
    monkeypatch.setattr(sl, "open_transport", lambda base_url=None: transport)
    monkeypatch.setattr(sl, "assessment_base_url", lambda settings=None: "http://host.invalid")
    prompt = build_prompt("What has the owner been doing?", [TOMATOES], precision="none")
    items = cited_item_lines("x [1]", prompt)
    transport.client.replies = ['{"supported": "yes"}', '{"supported": "no"}', 'Yes, it is supported.']
    assert await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000) is True
    assert await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000) is False
    assert await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000) is None
    assert await ar._judge_local("They re-potted tomatoes [1].", (), deadline=2_000_000_000) is False   # nothing cited
    url, timeout, body = posted[0]
    assert url.endswith("/api/chat") and timeout <= 60
    assert body["model"] == sl.MODEL and body["format"] == ar.JUDGE_FORMAT and body["think"] is False
    assert body["options"] == {"temperature": 0, "num_predict": 24, "num_ctx": 8192} and "keep_alive" not in body
    assert body["messages"] == ar.judge_messages("They re-potted tomatoes [1].", items)
    transport.client.replies = [None]
    monkeypatch.setattr(Response, "raise_for_status", lambda self: (_ for _ in ()).throw(RuntimeError("503")))
    with pytest.raises(PolicyError) as refused:
        await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000)
    assert refused.value.code == "model_error"


# --- V2 through the service: the switch, the receipt, the deadline ---------------------------------------------------

def _service_path(legacy, tmp_path, monkeypatch, judge, answer):
    from tests.permissions_v2.message_search_harness import owner
    from tests.permissions_v2.test_answer_release import _ask, _finished, _service
    from tests.permissions_v2.test_knowledge_search import node_for
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, answer)
    service.judge = judge
    try:
        answer_id = _ask(node, service, "Synthetic message")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?", ("answer-1",)).fetchone()
    finally:
        service.close()
    return body, json.loads(receipt[0])


def _receipt_sentences(receipt):
    assert receipt["version"] == "topos-local-receipt/answer-v1"
    return receipt["sentences"]


def test_with_the_switch_off_the_service_never_asks_the_judge_and_the_receipt_has_no_counter(legacy, tmp_path, monkeypatch):
    asked = []

    async def judge(sentence, items, *, deadline):
        asked.append(sentence)
        return False

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body["outcome"] == "answered" and asked == []
    assert "dropped_support" not in _receipt_sentences(receipt)


def test_with_the_switch_on_a_no_drops_the_answer_and_the_receipt_counts_it(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")
    asked = []

    async def judge(sentence, items, *, deadline):
        asked.append((sentence, items, deadline))
        return False

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert len(asked) == 1 and asked[0][0] == "The owner is working on a synthetic task [1]."
    assert len(asked[0][1]) == 1 and asked[0][1][0].startswith("[1] message")
    sentences = _receipt_sentences(receipt)
    assert sentences["dropped_support"] == 1 and sentences["kept"] == 0 and sentences["generated"] == 1


def test_with_the_switch_on_a_yes_delivers(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")

    async def judge(sentence, items, *, deadline):
        return True

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body["outcome"] == "answered" and _receipt_sentences(receipt)["dropped_support"] == 0


def test_with_the_switch_on_a_judge_that_fails_drops_not_raises(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")

    async def judge(sentence, items, *, deadline):
        raise PolicyError("model_unavailable")

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert _receipt_sentences(receipt)["dropped_support"] == 1


# --- V3: more items -------------------------------------------------------------------------------------------------

def _many(count):
    digits = "123456789abcdef0123456789abcdef"
    return [_message(digits[i], f"Note number {i}: the harbour lamp project moved a step on day {i}.") for i in range(count)]


def test_by_default_the_prompt_takes_at_most_eight_items():
    build_prompt("What has the owner been doing?", _many(8), precision="none")
    with pytest.raises(PolicyError):
        build_prompt("What has the owner been doing?", _many(9), precision="none")


@pytest.mark.parametrize("items", [16, 24])
def test_v3_the_prompt_takes_up_to_the_switch(items, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_ITEMS", str(items))
    assert k_answer() == items
    prompt = build_prompt("What has the owner been doing?", _many(items), precision="none")
    assert len(prompt.item_lines) == items and f"[{items}] message" in prompt.user
    with pytest.raises(PolicyError):
        build_prompt("What has the owner been doing?", _many(items + 1), precision="none")


def test_v3_is_clamped_to_the_ceiling_and_the_floor(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_ITEMS", "99")
    assert k_answer() == K_ANSWER_CEILING == 24
    monkeypatch.setenv(P + "ANSWER_ITEMS", "0")
    assert k_answer() == 1
    monkeypatch.setenv(P + "ANSWER_ITEMS", "sixteen")
    assert k_answer() == 8


def test_v3_retrieval_asks_for_the_switch_bounded_by_the_grants_max_k(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2.message_search_harness import owner
    from tests.permissions_v2.test_knowledge_search import node_for
    from topos.permissions_v2 import search_release
    seen = []
    original = search_release.SearchIntent.parse

    def parse(value):
        seen.append(value.get("k"))
        return original(value)

    monkeypatch.setattr(search_release.SearchIntent, "parse", parse)
    node, _identity = node_for(legacy, tmp_path, monkeypatch, max_k=10, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    with node.ledger._transaction() as db:
        authority, _policy = node.ledger._authority(db, "grant-search", node.now[0])
    from tests.permissions_v2.message_search_harness import recipient
    for setting, expected in (("8", 8), ("16", 10), ("24", 10), ("3", 3)):
        monkeypatch.setenv(P + "ANSWER_ITEMS", setting)
        seen.clear()
        with recipient():
            node.search.retrieve_for_answer(grant_id="grant-search", question="Synthetic message", admitted_authority=authority)
        assert seen[0] == expected, (setting, seen)


def test_the_with_sources_body_still_carries_at_most_eight_records(monkeypatch):
    """The wire cap does not move with V3: an answer citing more distinct items than eight is a body the mirrored
    grammar refuses (`body_invalid` in the service), never a wider body."""
    monkeypatch.setenv(P + "ANSWER_ITEMS", "16")
    records = _many(16)
    prompt = build_prompt("What has the owner been doing?", records, precision="none")
    text = "\n".join(f"Step {i} moved [{i}]." for i in range(1, 10))
    with pytest.raises(PolicyError):
        post_check_answer(text, records, prompt, mode="with_sources", boundary=Open())
    eight = "\n".join(f"Step {i} moved [{i}]." for i in range(1, 9))
    checked = post_check_answer(eight, records, prompt, mode="with_sources", boundary=Open())
    assert checked.body.outcome == "answered" and len(checked.body.records) == 8
