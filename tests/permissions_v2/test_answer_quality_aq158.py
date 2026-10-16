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
V3 `ANSWER_ITEMS`. The released items the model sees, 1 to 30 (default 8 until 1.5.10, 30 since, the owner's decision
   of 16 Oct 2026); retrieval is still bounded by the grant's max_k and the knowledge ceiling, and the with-sources
   body by its asker's cap of cited records.

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
from topos.permissions_v2.answer_protocol import FETCH, K_ANSWER, K_ANSWER_LEGACY, K_ANSWER_V4, parse_answer_output
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
    for name in (P + "ANSWER_SUBJECT_WORDS", P + "ANSWER_SUPPORT_JUDGE", P + "ANSWER_ITEMS", P + "ANSWER_SHORT_WORD_NAMES",
                 P + "ANSWER_QUESTION_ANCHOR", P + "ANSWER_SUPPORT_JUDGE_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)


# --- the table -------------------------------------------------------------------------------------------------------

def test_the_six_switches_default_to_the_candidate_from_1_5_7_on():
    """DF160 (A2A-4 amendment 11): the candidate is the default; 1.5.6's behaviour is reachable by setting each."""
    assert switches.ANSWER_SUBJECT_WORDS.unbound is False and switches.ANSWER_SUBJECT_WORDS.bound is False
    assert switches.ANSWER_SUPPORT_JUDGE.unbound is True and switches.ANSWER_SUPPORT_JUDGE.bound is True
    # TR162 (A2A-4 amendment 12): the judge template's default moved on to v4 (v3 with the time rule); v3 is selectable.
    assert switches.ANSWER_SUPPORT_JUDGE_TEMPLATE.unbound == "v4" and switches.ANSWER_SUPPORT_JUDGE_TEMPLATE.bound == "v4"
    # LA164 (A2A-4 amendment 13) moved the wire cap to 16 and the items default with it; the integration (IN167) kept
    # the items default at 8 until a battery clears 16. 1.5.10 (the owner's decision of 16 Oct 2026, after the grantee
    # density runs) moved it to 30, answer protocol 4's cap. The rest is amendment 11's.
    assert switches.ANSWER_ITEMS.unbound == K_ANSWER_V4 == 30 and switches.ANSWER_ITEMS.bound == 30 and K_ANSWER == 16
    assert switches.ANSWER_SHORT_WORD_NAMES.unbound is False and switches.ANSWER_SHORT_WORD_NAMES.bound is False
    assert switches.ANSWER_QUESTION_ANCHOR.unbound is False and switches.ANSWER_QUESTION_ANCHOR.bound is False
    assert k_answer() == 30 and ar.judge_template() == "v4"
    assert ar.judge_template_version() == "topos-answer-support-judge/v4"


def test_the_suite_never_reaches_the_model_host_through_the_judge():
    """DF160: with the judge on by default, every `AnswerService` the folder builds binds the conftest guard, which
    says "yes" (1.5.6's outcome) unless a test hands in a judge; the real body runs only under `real_support_judge`
    with the transport replaced, and a call that reaches it elsewhere fails that test at teardown."""
    import asyncio
    guard = ar._judge_local
    state = guard.state                                       # the conftest's record, on the guard itself
    assert guard.__wrapped__ is state["original"] and guard is not state["original"]
    assert state["original"].__module__ == "topos.permissions_v2.answer_release"
    assert state["mode"] == "yes" and state["trips"] == []
    service = ar.AnswerService.__new__(ar.AnswerService)
    service.judge = None
    ar.AnswerService.__init__(service, runtime=None, generate=lambda *a, **k: None)
    try:
        assert service.judge is guard
        assert asyncio.run(service.judge("x [1].", ("[1] message\nx",), deadline=2_000_000_000, question="Q?")) is True
    finally:
        service.close()
    # outside any test's mode the guard records the trip and raises; the fixture resets the record for this test
    state["mode"] = None
    try:
        with pytest.raises(AssertionError):
            asyncio.run(guard("x [1].", ("[1] message\nx",), deadline=2_000_000_000))
        assert state["trips"] == [state["test"]] and state["test"].endswith("::" + test_the_suite_never_reaches_the_model_host_through_the_judge.__name__)
    finally:
        state["mode"] = "yes"
        state["trips"].clear()


# --- V0: the live "lately" case ------------------------------------------------------------------------------------

RECENCY = "What has the owner been up to lately?"
RECENCY_ANSWER = ("The owner re-potted tomatoes and fixed a bike chain [1].\n"
                  "They began evening sketches of the harbour [2].\nThey rewired a reading lamp [3].")


@pytest.mark.parametrize("word", ["to", "been", "up"])
def test_under_1_5_6s_binding_a_persons_short_word_on_the_node_makes_a_subjectless_question_drop_every_sentence(word, monkeypatch):
    """The mechanism behind the unexplained live row: no subject, no anchor, and still all sentences dropped as
    `dropped_relevance`, because a known person's word equals a lower-case question word. DF160: 1.5.6's binding,
    reachable by the switch; the default below names nobody by such a word."""
    monkeypatch.setenv(P + "ANSWER_SHORT_WORD_NAMES", "true")
    for mode in MODES:
        prompt, checked = _checked(RECENCY, [TOMATOES, SKETCHES, LAMP], RECENCY_ANSWER, mode)
        assert checked.body.outcome == "answered" and checked.kept == 3          # nobody on the node: answered
        prompt, checked = _checked(RECENCY, [TOMATOES, SKETCHES, LAMP], RECENCY_ANSWER, mode, people=frozenset({word}))
        assert prompt.name_terms == frozenset({word})
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 3 and checked.reason == "all_sentences_dropped"


@pytest.mark.parametrize("word", ["to", "been", "up"])
def test_by_default_a_persons_short_word_on_the_node_names_nobody_in_lower_case(word):
    """DF160 (amendment 11.1): the live "lately" row is answered with nothing set."""
    for mode in MODES:
        prompt, checked = _checked(RECENCY, [TOMATOES, SKETCHES, LAMP], RECENCY_ANSWER, mode, people=frozenset({word}))
        assert prompt.name_terms == frozenset() and checked.body.outcome == "answered" and checked.kept == 3


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


def test_v0_uses_a_closed_function_word_list_not_the_webster_lists(monkeypatch):
    """Review R-158 F2: the Webster 2-4 letter lists hold 154 common given names ("mark", "rose", "lee"), which must
    keep binding in lower case. The list is closed, pinned, and covers the six words WS0 found as name words on the
    owner's node (to, the, this, on, is, and)."""
    from topos.permissions_v2.answer_generation import FUNCTION_WORDS
    from topos.permissions_v2.common_given_names import GIVEN_NAMES
    from topos.permissions_v2.english_short_words import WORDS_2_3, WORDS_4, WORDS_ENDING_S_3_4
    required = ("to up been the this on is and in at for of with from by as or if so do did has have had was were be am "
                "are it its my our your his her their me us them we you he she they what who when where why how which "
                "that these those than then now here there out over under about into onto off down back just only also "
                "very more most some any all each no not yes lately recently today week month year last next").split()
    assert set(required) <= FUNCTION_WORDS
    assert 150 <= len(FUNCTION_WORDS) <= 200 and all(word == word.lower() and word.isalpha() for word in FUNCTION_WORDS)
    assert FUNCTION_WORDS & GIVEN_NAMES == {"my"}            # the one required word that is also a given name
    assert {"will", "may", "per"} & FUNCTION_WORDS == set()
    assert len((WORDS_2_3 | WORDS_4 | WORDS_ENDING_S_3_4) & GIVEN_NAMES) > 100   # why the Webster lists were dropped
    monkeypatch.setenv(P + "ANSWER_SHORT_WORD_NAMES", "false")
    for name in ("mark", "rose", "lee", "jack", "jane"):
        assert name in (WORDS_2_3 | WORDS_4 | WORDS_ENDING_S_3_4) or name == "jane"
        prompt, checked = _checked(f"What has the owner said about {name}?", [TOMATOES], "They re-potted tomatoes [1].", "only",
                                   people=frozenset({name}))
        assert prompt.name_terms == frozenset({name}) and checked.body.outcome == "no_answer", name
    for word in ("to", "the", "this", "on", "is", "and"):
        prompt, _checked_ = _checked("What has the owner been up to lately?", [TOMATOES], "x [1].", "only",
                                     people=frozenset({word}))
        assert word not in prompt.name_terms, word


def test_v0_off_a_persons_word_that_is_no_english_word_still_binds_in_lower_case(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SHORT_WORD_NAMES", "false")
    prompt, checked = _checked("What has the owner said about ivo?", [TOMATOES], "They re-potted tomatoes [1].", "only",
                               people=frozenset({"ivo"}))
    assert prompt.name_terms == frozenset({"ivo"}) and checked.body.outcome == "no_answer"


# --- V1: the topic-word requirement ----------------------------------------------------------------------------------

HOBBY_Q = "What interests and hobbies has the owner explicitly described?"
HOBBY_SENTENCE = "The owner re-pots tomatoes, fixes their bike and sketches the harbour in the evenings [1, 2]."


def test_under_1_5_6s_topic_word_rule_a_supported_sentence_in_other_words_than_the_question_is_dropped(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")
    for mode in MODES:
        _prompt, checked = _checked(HOBBY_Q, [TOMATOES, SKETCHES], HOBBY_SENTENCE, mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1


def test_by_default_a_supported_sentence_in_other_words_than_the_question_is_kept():
    """DF160 (amendment 11.2): with nothing set, the live node's 42 dropped sentences' class stands on the words."""
    for mode in MODES:
        _prompt, checked = _checked(HOBBY_Q, [TOMATOES, SKETCHES], HOBBY_SENTENCE, mode)
        assert checked.body.outcome == "answered" and checked.kept == 1 and checked.dropped_relevance == 0


def test_v1_off_with_the_anchor_rule_set_on_it_still_abstains_before_the_model(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    monkeypatch.setenv(P + "ANSWER_QUESTION_ANCHOR", "true")
    prompt = build_prompt("What has the owner said about the Winter Olympics?", [TOMATOES, SKETCHES], precision="none")
    assert question_lacks_permitted_anchor(prompt)


def test_v1b_off_the_anchor_rule_does_not_abstain_and_the_echo_check_still_drops_the_word_said_back(monkeypatch):
    """The sub-variant the brief allowed once the anchor rule was shown to block supported answers on real-shaped
    questions ("What goals and intentions has the owner stated?": "intentions" is in no item that states one)."""
    goals = _journal("7", "Resolved this month: finish the lantern firmware and keep Sundays free.")
    question = "What goals and intentions has the owner stated? Cite each one."
    monkeypatch.setenv(P + "ANSWER_QUESTION_ANCHOR", "true")                        # 1.5.6's rule, by the switch
    prompt = build_prompt(question, [goals], precision="none")
    assert question_lacks_permitted_anchor(prompt)
    monkeypatch.delenv(P + "ANSWER_QUESTION_ANCHOR")                                # the default (DF160)
    prompt = build_prompt(question, [goals], precision="none")
    assert not question_lacks_permitted_anchor(prompt)
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")
    for mode in MODES:
        _prompt, checked = _checked(question, [goals], "Their intentions are to finish the lantern firmware [1].", mode)
        assert checked.dropped_question_echo == 1 and checked.body.outcome == "no_answer"      # the word said back
    monkeypatch.delenv(P + "ANSWER_SUBJECT_WORDS")
    for mode in MODES:
        _prompt, checked = _checked(question, [goals], "Their intentions are to finish the lantern firmware [1].", mode)
        assert checked.dropped_question_echo == 1 and checked.body.outcome == "no_answer"      # and by default too
    for mode in MODES:
        _prompt, checked = _checked(question, [goals], "They mean to complete the firmware and leave Sundays open [1].", mode)
        assert checked.body.outcome == "answered", mode


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
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")
    _prompt, checked = _checked("Why did the pepper mash fail?", [LAMP], "The lamp's crackling switch spoiled the mash [1].", "only")
    assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1
    monkeypatch.delenv(P + "ANSWER_SUBJECT_WORDS")                                  # the default (DF160)
    _prompt, checked = _checked("Why did the pepper mash fail?", [LAMP], "The lamp's crackling switch spoiled the mash [1].", "only")
    assert checked.body.outcome == "answered"
    prompt = build_prompt("Why did the pepper mash fail?", [LAMP], precision="none")
    judged = post_check_answer("The lamp's crackling switch spoiled the mash [1].", [LAMP], prompt, mode="only", boundary=Open(),
                               support=lambda _s, _i, _q: False)
    assert judged.body.outcome == "no_answer" and judged.dropped_support == 1


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

    def judge(sentence, items, question):
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


def test_the_judge_runs_only_on_sentences_the_deterministic_checks_kept(monkeypatch):
    calls = []

    def judge(sentence, items, question):
        calls.append(sentence)
        return True

    prompt = build_prompt(HOBBY_Q, [TOMATOES, SKETCHES], precision="none")
    checked = post_check_answer("No citation here.\n" + HOBBY_SENTENCE, [TOMATOES, SKETCHES], prompt, mode="only",
                                boundary=Open(), support=judge)
    assert calls == [HOBBY_SENTENCE] and checked.dropped_citation == 1 and checked.dropped_relevance == 0
    assert checked.dropped_support == 0 and checked.kept == 1                          # DF160: the words keep it
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")                             # 1.5.6's rule drops it first
    calls.clear()
    checked = post_check_answer("No citation here.\n" + HOBBY_SENTENCE, [TOMATOES, SKETCHES], prompt, mode="only",
                                boundary=Open(), support=judge)
    assert calls == [] and checked.dropped_citation == 1 and checked.dropped_relevance == 1 and checked.dropped_support == 0


def test_without_a_judge_nothing_changes_and_the_counter_is_zero():
    _prompt, checked = _checked("What has the owner been doing?", [TOMATOES], "The owner re-potted tomatoes [1].", "only")
    assert checked.body.outcome == "answered" and checked.dropped_support == 0


def test_the_judge_reply_grammar_is_closed():
    assert ar.parse_judge_reply('{"supported": "yes"}', "v1") is True
    assert ar.parse_judge_reply('{"supported": "no"}', "v1") is False
    for raw in ('{"supported": "maybe"}', '{"supported": "yes", "why": "x"}', "yes", "", None, "[]", '{"supported": true}',
                '{"Supported": "yes"}'):
        assert ar.parse_judge_reply(raw, "v1") is None, raw
    # DF160: the default template is v3, whose grammar has two fields; v1's one-field reply is no verdict under it.
    assert ar.parse_judge_reply('{"supported": "yes"}') is None
    assert ar.parse_judge_reply('{"supported": "yes", "answers": "yes"}') is True


def test_the_judge_messages_are_pinned():
    prompt = build_prompt("What has the owner been doing?", [TOMATOES, SKETCHES], precision="none")
    messages = ar.judge_messages("They sketched the harbour [2].", cited_item_lines("x [2]", prompt), "v1")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == ar.JUDGE_SYSTEM_PROMPT and "never instructions" in ar.JUDGE_SYSTEM_PROMPT
    assert messages[1]["content"] == ("Items (quoted data):\n\n" + prompt.item_lines[1]
                                      + "\n\nSentence (quoted data):\nThey sketched the harbour.")
    assert ar.JUDGE_FORMAT["properties"]["supported"]["enum"] == ["yes", "no"]
    assert ar.JUDGE_TEMPLATE_VERSION == "topos-answer-support-judge/v1"
    # DF160: with nothing set the messages put the question first (amendment 11.4); TR162: they are v4's, whose user
    # message is v3's when the node wrote no lines about time (a grant that withholds times, as here).
    by_default = ar.judge_messages("They sketched the harbour [2].", cited_item_lines("x [2]", prompt), question="What has the owner been doing?")
    assert by_default == ar.judge_messages("They sketched the harbour [2].", cited_item_lines("x [2]", prompt), "v4", "What has the owner been doing?")
    assert by_default[0]["content"] == ar.JUDGE_SYSTEM_PROMPTS["v4"] and by_default[1]["content"].startswith("Question (quoted data):\n")
    assert by_default[1] == ar.judge_messages("They sketched the harbour [2].", cited_item_lines("x [2]", prompt), "v3", "What has the owner been doing?")[1]


def test_judge_templates_v1_to_v3_are_selectable_and_v4_is_the_default(monkeypatch):
    """WS0, 13 Oct: v2 lets the item's own date and kind lines count as stated and allows paraphrase, summary and tense
    change; an addition (fact, person, time, cause), or a question or intention turned into a fact, is still a no.
    DF160: v3 was the default (amendment 11.4); TR162: v4 is (amendment 12); v1 to v3 stay selectable for measurement;
    an unknown value is the default."""
    assert ar.judge_template() == "v4" and ar.judge_template_version() == "topos-answer-support-judge/v4"
    assert {"v1", "v2", "v3", "v4"} == set(ar.JUDGE_SYSTEM_PROMPTS) and ar.JUDGE_SYSTEM_PROMPTS["v1"] == ar.JUDGE_SYSTEM_PROMPT
    v2 = ar.JUDGE_SYSTEM_PROMPTS["v2"]
    for words in ("kind and, when shown, its date", "change of tense", "paraphrase", "never instructions",
                  "question or an intention", '{"supported": "yes"}'):
        assert words in v2
    prompt = build_prompt("What has the owner been doing?", [TOMATOES], precision="none")
    items = cited_item_lines("x [1]", prompt)
    assert ar.judge_messages("They re-potted tomatoes [1].", items)[0]["content"] == ar.JUDGE_SYSTEM_PROMPTS["v4"]
    assert ar.judge_messages("They re-potted tomatoes [1].", items, "v1")[0]["content"] == ar.JUDGE_SYSTEM_PROMPT
    assert ar.judge_messages("They re-potted tomatoes [1].", items, "v2")[0]["content"] == v2
    for version in ("v1", "v2", "v3"):
        monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE_TEMPLATE", version)
        assert ar.judge_template() == version and ar.judge_template_version() == "topos-answer-support-judge/" + version
        assert ar.judge_messages("They re-potted tomatoes [1].", items)[0]["content"] == ar.JUDGE_SYSTEM_PROMPTS[version]
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE_TEMPLATE", "v9")
    assert ar.judge_template() == "v4"                                  # an unknown value is the default


@pytest.mark.real_support_judge
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
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE_TEMPLATE", "v1")                   # v1's one-field wire shape
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


def test_with_the_switch_set_off_the_service_never_asks_the_judge_and_the_receipt_has_no_counter(legacy, tmp_path, monkeypatch):
    """DF160: 1.5.6's receipt and no judge call, one environment line away."""
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "false")
    asked = []

    async def judge(sentence, items, *, deadline, question=None):
        asked.append(sentence)
        return False

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body["outcome"] == "answered" and asked == []
    assert "dropped_support" not in _receipt_sentences(receipt) and "judge_template_version" not in receipt


def test_by_default_a_no_from_the_judge_drops_the_answer_and_the_receipt_counts_it(legacy, tmp_path, monkeypatch):
    """DF160: nothing set; the judge is on with template v3 (amendment 11.4, 11.5)."""
    asked = []

    async def judge(sentence, items, *, deadline, question=None):
        asked.append((sentence, items, deadline, question))
        return False

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert len(asked) == 1 and asked[0][0] == "The owner is working on a synthetic task [1]."
    assert len(asked[0][1]) == 1 and asked[0][1][0].startswith("[1] message")
    assert asked[0][3] == "Synthetic message"                   # v3's question, passed as asked (R-158 F3)
    sentences = _receipt_sentences(receipt)
    assert sentences["dropped_support"] == 1 and sentences["kept"] == 0 and sentences["generated"] == 1
    assert sentences["dropped_non_answer"] == 0 and sentences["dropped_question_item"] == 0
    assert receipt["judge_template_version"] == "topos-answer-support-judge/v4"     # TR162: v4 by default


def test_with_the_switch_on_a_yes_delivers(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")

    async def judge(sentence, items, *, deadline, question=None):
        return True

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body["outcome"] == "answered" and _receipt_sentences(receipt)["dropped_support"] == 0


def test_with_the_switch_on_a_judge_that_fails_drops_not_raises(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")

    async def judge(sentence, items, *, deadline, question=None):
        raise PolicyError("model_error")

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert _receipt_sentences(receipt)["dropped_support"] == 1 and receipt["reason"] == "all_sentences_dropped"


# --- V3: more items -------------------------------------------------------------------------------------------------

def _many(count):
    digits = "123456789abcdef0123456789abcdef"
    return [_message(digits[i], f"Note number {i}: the harbour lamp project moved a step on day {i}.") for i in range(count)]


def test_with_1_5_9s_settings_the_prompt_takes_at_most_eight_items(monkeypatch):
    """IN167: eight, as in 1.5.9, now one environment line (the default is 30 since 1.5.10). LA164's sixteen (A2A-4
    amendment 13) and the others are the switch's settings, tested in `test_v3_the_prompt_takes_up_to_the_switch`
    below."""
    monkeypatch.setenv(P + "ANSWER_ITEMS", "8")
    build_prompt("What has the owner been doing?", _many(8), precision="none")
    with pytest.raises(PolicyError):
        build_prompt("What has the owner been doing?", _many(9), precision="none")


@pytest.mark.parametrize("items", [16, 24, 30])
def test_v3_the_prompt_takes_up_to_the_switch(items, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_ITEMS", str(items))
    assert k_answer() == items
    prompt = build_prompt("What has the owner been doing?", _many(items), precision="none")
    assert len(prompt.item_lines) == items and f"[{items}] message" in prompt.user
    with pytest.raises(PolicyError):
        build_prompt("What has the owner been doing?", _many(items + 1), precision="none")


def test_v3_is_clamped_to_the_ceiling_and_the_floor(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_ITEMS", "99")
    assert k_answer() == K_ANSWER_CEILING == K_ANSWER_V4 == 30          # answer protocol 4 (24 before)
    monkeypatch.setenv(P + "ANSWER_ITEMS", "0")
    assert k_answer() == 1
    monkeypatch.setenv(P + "ANSWER_ITEMS", "8")
    assert k_answer() == 8
    monkeypatch.setenv(P + "ANSWER_ITEMS", "sixteen")
    assert k_answer() == switches.ANSWER_ITEMS.unbound == 30  # unreadable: the default (30 since 1.5.10; 8 before)


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


def test_the_with_sources_body_carries_at_most_sixteen_records(monkeypatch):
    """The wire cap does not move with V3 (it moved to sixteen with A2A-4 amendment 13, LA164, and to thirty for an
    asker that accepts answer protocol 4): an answer citing more distinct items than its asker's cap is a body the
    mirrored grammar refuses at that asker's caps (`body_invalid` in the service), never a wider body."""
    monkeypatch.setenv(P + "ANSWER_ITEMS", "24")
    records = _many(17)
    prompt = build_prompt("What has the owner been doing?", records, precision="none")
    text = "\n".join(f"Step {i} moved [{i}]." for i in range(1, 18))
    seventeen = post_check_answer(text, records, prompt, mode="with_sources", boundary=Open())
    with pytest.raises(PolicyError):
        parse_answer_output(seventeen.body, request_type=FETCH, mode="with_sources", accepted_version=3)
    assert len(parse_answer_output(seventeen.body, request_type=FETCH, mode="with_sources",
                                   accepted_version=4).records) == 17
    sixteen = "\n".join(f"Step {i} moved [{i}]." for i in range(1, 17))
    checked = post_check_answer(sixteen, records, prompt, mode="with_sources", boundary=Open())
    assert checked.body.outcome == "answered" and len(checked.body.records) == 16
    parse_answer_output(checked.body, request_type=FETCH, mode="with_sources", accepted_version=3)


# --- review R-158 ---------------------------------------------------------------------------------------------------

def test_f1_the_judge_runs_with_no_gate_held(legacy, tmp_path, monkeypatch):
    """R-158 F1: the judge's model calls ran inside `resolver._read()`, under the process-wide write gate, for up to
    the deadline. Now the deterministic checks and the body close run under the gate and the judge between them,
    gate-free: another thread can take the gate while a judge callback is in flight."""
    import threading
    from tests.permissions_v2.message_search_harness import owner
    from tests.permissions_v2.test_answer_release import _ask, _finished, _service
    from tests.permissions_v2.test_knowledge_search import node_for
    from topos.storage.db.write_gate import with_db_write
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")
    in_flight, release, took_gate = threading.Event(), threading.Event(), threading.Event()

    async def judge(sentence, items, *, deadline, question=None):
        in_flight.set()
        assert release.wait(10), "the probe never released the judge"
        return True

    def probe():
        with with_db_write():
            took_gate.set()

    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The owner is working on a synthetic task [1].")
    service.judge = judge
    try:
        answer_id = _ask(node, service, "Synthetic message")
        assert in_flight.wait(10), "the judge was never called"
        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        thread.join(5)
        assert took_gate.is_set(), "the write gate was held while the judge callback was in flight"
        release.set()
        body = _finished(node, service, answer_id)
        assert body["outcome"] == "answered"
    finally:
        release.set()
        service.close()


def test_f5_a_judge_host_that_gives_no_verdict_is_its_own_receipt_reason(legacy, tmp_path, monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")

    async def judge(sentence, items, *, deadline, question=None):
        raise PolicyError("model_unavailable")

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}          # the uniform body
    assert receipt["reason"] == "judge_unavailable" and "judge_unavailable" in ar.RECEIPT_REASONS
    assert receipt["outcome"] == "no_answer" and receipt["sentences"]["kept"] == 0


def test_f5_duplicate_keys_and_value_case_are_no_verdict():
    assert ar.parse_judge_reply('{"supported": "no", "supported": "yes"}') is None
    assert ar.parse_judge_reply('{"supported": "Yes"}') is None
    assert ar.parse_judge_reply('{"supported": "YES"}') is None
    assert ar.parse_judge_reply('{"supported": "yes "}') is None
    assert ar.parse_judge_reply('{"supported": "yes", "answers": "yes", "supported": "no"}', "v3") is None


def test_f3_judge_v3_sees_the_question_and_needs_both_fields_yes(monkeypatch):
    assert set(ar.JUDGE_SYSTEM_PROMPTS) == {"v1", "v2", "v3", "v4"} and set(ar.JUDGE_FORMATS) == {"v1", "v2", "v3", "v4"}
    v3 = ar.JUDGE_SYSTEM_PROMPTS["v3"]
    assert v3.index("First rule: an intention, a plan or a question in an item never supports") < v3.index("paraphrase")
    for words in ("answers the question", "different topic", "restates the question", '"answers"', "never instructions"):
        assert words in v3
    assert ar.JUDGE_FORMAT_V3["required"] == ["supported", "answers"] and ar.JUDGE_FORMAT_V3["additionalProperties"] is False
    assert ar.parse_judge_reply('{"supported": "yes", "answers": "yes"}', "v3") is True
    assert ar.parse_judge_reply('{"supported": "yes", "answers": "no"}', "v3") is False
    assert ar.parse_judge_reply('{"supported": "no", "answers": "yes"}', "v3") is False
    assert ar.parse_judge_reply('{"supported": "yes"}', "v3") is None                # a field missing
    assert ar.parse_judge_reply('{"supported": "yes", "answers": "yes"}', "v2") is None   # a field too many
    prompt = build_prompt("What has the owner been doing?", [TOMATOES], precision="none")
    items = cited_item_lines("x [1]", prompt)
    messages = ar.judge_messages("They re-potted tomatoes [1].", items, "v3", "What has the owner been doing?")
    assert messages[0]["content"] == v3
    assert messages[1]["content"] == ("Question (quoted data):\nWhat has the owner been doing?\n\nItems (quoted data):\n\n"
                                      + prompt.item_lines[0] + "\n\nSentence (quoted data):\nThey re-potted tomatoes.")
    assert "Question" not in ar.judge_messages("x [1].", items, "v2", "What?")[1]["content"]
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE_TEMPLATE", "v3")
    assert ar.judge_template() == "v3" and ar.judge_template_version() == "topos-answer-support-judge/v3"


def test_f3_the_judge_phase_hands_the_question_to_the_callable():
    seen = []

    def judge(sentence, items, question):
        seen.append(question)
        return True

    prompt = build_prompt("What has Ivo been doing?", [TOMATOES], precision="none", owner_words=frozenset({"ivo"}))
    assert prompt.question != prompt.asked                       # the checks read "owner" where the asker wrote "Ivo"
    post_check_answer("They re-potted tomatoes [1].", [TOMATOES], prompt, mode="only", boundary=Open(), support=judge)
    assert seen == ["What has Ivo been doing?"]                  # the question as asked, never the `_as_owner` reading


@pytest.mark.real_support_judge
@pytest.mark.asyncio
async def test_f4_the_local_judge_requires_done_true_and_the_exact_model_and_posts_v3s_grammar(monkeypatch):
    from topos.permissions_v2 import shadow_labeler_local as sl
    posted = []

    class Response:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class Client:
        replies: list = []

        async def post(self, url, timeout=None, json=None):
            posted.append(json)
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
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE_TEMPLATE", "v1")                   # v1 first, then v3 (the default)
    prompt = build_prompt("What has the owner been doing?", [TOMATOES], precision="none")
    items = cited_item_lines("x [1]", prompt)
    good = {"model": sl.MODEL, "done": True, "message": {"content": '{"supported": "yes"}'}}
    for bad in ({**good, "done": False}, {**good, "model": "other:latest"}, {**good, "done": "true"}):
        transport.client.replies = [bad]
        with pytest.raises(PolicyError) as refused:
            await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000)
        assert refused.value.code == "model_error"
    transport.client.replies = [good]
    assert await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000) is True
    monkeypatch.delenv(P + "ANSWER_SUPPORT_JUDGE_TEMPLATE")                            # v4 by default (TR162)
    transport.client.replies = [{**good, "message": {"content": '{"supported": "yes", "answers": "no"}'}}]
    assert await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000, question="Q?") is False
    assert posted[-1]["format"] == ar.JUDGE_FORMAT_V3
    assert posted[-1]["messages"] == ar.judge_messages("They re-potted tomatoes [1].", items, "v4", "Q?")
    transport.client.replies = [{**good, "message": {"content": '{"supported": "yes"}'}}]      # v2's shape under v3
    assert await ar._judge_local("They re-potted tomatoes [1].", items, deadline=2_000_000_000, question="Q?") is None


def test_f4_the_support_closure_checks_the_deadline_before_each_call(legacy, tmp_path, monkeypatch):
    """A judge call after the job's end is never made: with the end set to the moment of acceptance, the closure
    returns False before any call, and the job ends `deadline` with the uniform body."""
    monkeypatch.setenv(P + "ANSWER_SUPPORT_JUDGE", "true")
    monkeypatch.setattr(ar, "MAX_END_SECONDS", 0)
    calls = []

    async def judge(sentence, items, *, deadline, question=None):
        calls.append(sentence)
        return True

    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"} and calls == []
    assert receipt["reason"] == "deadline"


def test_f6_the_judge_item_block_is_the_prompts_own_block_for_a_fact_item_too():
    from topos.permissions_v2.knowledge_contract import FactResult
    rid = "r." + "9" * 64
    fact = FactResult.parse({"kind": "fact", "assertion": "owner_stated", "record_id": rid, "content": "Works at the harbour studio.",
        "source_ids": ["journal"], "citations": [{"record_id": rid, "source_id": "journal",
        "content": "Spent the day at the harbour studio again; the lease runs to November."}]})
    prompt = build_prompt("Where does the owner work?", [TOMATOES, fact], precision="none")
    assert len(prompt.item_lines) == 2 and "Supporting text: " in prompt.item_lines[1]
    for line in prompt.item_lines:
        assert line in prompt.user
    assert "\n\n".join(prompt.item_lines) in prompt.user
    block = ar.judge_messages("They work at the studio [2, 1].", cited_item_lines("x [2, 1]", prompt), "v1")[1]["content"]
    assert prompt.item_lines[1] + "\n\n" + prompt.item_lines[0] in block


# --- WS0 13 Oct: the closed non-answer filter beside the judge, and the v3 text revision ------------------------------

def test_the_non_answer_lists_are_pinned():
    from topos.permissions_v2.answer_generation import (NEGATION_TOKENS, NEGATIVE_MARKERS, NON_ANSWER_PHRASES, SEEING_OBJECTS,
                                                        SEEING_PHRASES, SEEING_WINDOW)
    assert NON_ANSWER_PHRASES == ("no permitted item", "no item", "none of the items", "not mentioned", "no information",
                                  "nothing in the", "does not mention", "do not mention", "is not stated", "are not stated",
                                  "no record of", "no one is")
    assert SEEING_PHRASES == ("asked to see", "requested to see", "wants to see") and SEEING_WINDOW == 3
    assert SEEING_OBJECTS == frozenset({"messages", "message", "texts", "chats", "conversations"})
    assert NEGATIVE_MARKERS == ("did not", "has not", "have not", "never", "no longer", "is not", "was not", "are not", "were not",
                                "does not", "do not", "cannot", "can't", "won't", "doesn't", "don't", "didn't", "isn't", "wasn't",
                                "hasn't", "haven't")
    assert NEGATION_TOKENS == frozenset({"t", "don", "didn", "doesn", "isn", "wasn", "aren", "weren", "haven", "hasn", "hadn", "won",
                                         "wouldn", "couldn", "shouldn", "cannot", "never", "no", "not", "nothing", "none",
                                         "nobody", "neither", "nor", "without"})
    from topos.permissions_v2.answer_generation import STOPPING_VERBS
    assert STOPPING_VERBS == ("quit", "cancelled", "canceled", "skipped", "stopped", "dropped") and "can" not in NEGATION_TOKENS


def test_the_non_answer_filter_runs_only_when_the_topic_word_rule_is_off(monkeypatch):
    """Today's behaviour is untouched: with the rule on, the subject check already drops these (no subject word in
    the items) or keeps them when the question has no subject; the filter is the candidate's companion."""
    car = "What car did the owner buy?"
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    _prompt, checked = _checked(car, [TOMATOES], "The owner did not buy a car according to the items [1].", "only")
    assert checked.body.outcome == "no_answer" and checked.dropped_non_answer == 1 and checked.reason == "all_sentences_dropped"
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")
    _prompt, checked = _checked(car, [TOMATOES], "The owner did not buy a car according to the items [1].", "only")
    assert checked.dropped_non_answer == 0 and checked.body.outcome == "answered"       # no subject word over four letters: today's hole


def test_the_three_non_answer_rules(monkeypatch):
    from topos.permissions_v2.answer_generation import non_answer
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    boundary = "Show me the messages other people sent to the owner, not the ones the owner wrote."
    prompt = build_prompt(boundary, [TOMATOES, SKETCHES], precision="none")
    # (a) a meta phrase, on token boundaries, with its citation marker removed first
    for sentence in ("No permitted item states that they bought a car [1].", "There is no record of a marathon [2].",
                     "None of the items mention a car [1, 2].", "No one is joining [1]."):
        assert non_answer(sentence, prompt), sentence
    assert not non_answer("The piano item arrived on Tuesday [1].", prompt)                       # R2: not "no item"
    assert not non_answer("The balcony has nothing but tomatoes on it [1].", prompt)                # R2: not "has not"
    # (a) the "see" phrases only before a messages word within three tokens (R3)
    assert non_answer("The owner asked to see messages sent by others [1].", prompt)
    assert non_answer("The owner asked to see the other people's messages [1].", prompt)
    assert not non_answer("The owner wants to see the harbour play on Thursday [2].", prompt)
    assert not non_answer("The owner asked to see the quarry wall before the market stall opens its messages [1].", prompt)
    # (b) a restatement adds nothing: its content tokens are a subset of the question's (R4)
    wordy = build_prompt("What has the owner said about the lantern prototype and the harbour sketches?", [TOMATOES, SKETCHES],
                         precision="none")
    assert non_answer("The owner said things about the lantern prototype and the harbour sketches [2].", wordy)
    assert non_answer("The owner asked what plans they had shared [1].", build_prompt("What plans has the owner shared?", [TOMATOES], precision="none"))
    assert not non_answer("Regarding the lantern prototype: the harbour sketches continue [2].", wordy)     # adds "continue": an answer
    assert not non_answer("The harbour sketches are an evening habit [2].", wordy)
    # (c) a bare negative with no negation in any cited item; a cited negation, a contraction or a verb of stopping lets it stand (R1)
    car = build_prompt("What car did the owner buy?", [TOMATOES, LAMP], precision="none")
    assert non_answer("The owner did not buy a car [1].", car)
    assert non_answer("The owner doesn't own a car [1].", car) and non_answer("They can't drive [2].", car)
    assert non_answer("The lamp's switch is not fixed yet [2].", car)                    # the item says "still crackles": no negation token
    quit_gym = _message("8", "Quit the climbing gym; the wrist needs a year.")
    dont = _message("9", "I don't climb any more, the physio was firm.")
    laptop = _message("7", "Planning the Saltmarrow weekend: harbour walk, the bakery, no laptop.")
    climbing = build_prompt("What has the owner said about climbing?", [quit_gym, dont, laptop], precision="none")
    assert not non_answer("The owner is no longer climbing [1].", climbing)             # "quit" in the item
    skipping = _message("4", "Skipping the gym this month; the wrist needs rest.")
    able = _message("6", "I can climb again next month, the physio says.")
    folds = build_prompt("What has the owner said about training?", [skipping, able], precision="none")
    assert not non_answer("The owner is not training at the gym this month while the wrist rests [1].", folds)   # "skipping" folds onto "skipped"
    assert non_answer("The owner is not training at the gym this month while the wrist rests [2].", folds)       # a bare "can" is no negation
    assert not non_answer("They don't climb at the quarry any more [2].", climbing)     # the contraction's pieces
    assert non_answer("They don't climb any more [2].", climbing)                       # (b): no content word of its own
    assert not non_answer("They are not taking a laptop [3].", climbing)                # "no" in the item
    # a plain supported sentence passes every rule
    assert not non_answer("The owner re-potted tomatoes and fixed the bike chain [1].", prompt)


def test_the_filter_counts_on_the_receipt_only_while_the_rule_is_off(legacy, tmp_path, monkeypatch):
    async def judge(sentence, items, *, deadline, question=None):
        return True

    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "No permitted item states the task [1].")
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert _receipt_sentences(receipt)["dropped_non_answer"] == 1


def test_the_receipt_has_no_filter_counter_while_the_rule_is_set_on(legacy, tmp_path, monkeypatch):
    async def judge(sentence, items, *, deadline, question=None):
        return True

    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")                           # 1.5.6's rule (DF160)
    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body["outcome"] == "answered" and "dropped_non_answer" not in _receipt_sentences(receipt)


def test_v3_text_revision_names_negatives_and_nothing_known():
    v3 = ar.JUDGE_SYSTEM_PROMPTS["v3"]
    assert ("A sentence that says something did not happen, or that nothing is known or mentioned, is supported only "
            "if a cited item states that in so many words.") in v3
    assert ar.judge_template_version({P + "ANSWER_SUPPORT_JUDGE_TEMPLATE": "v3"}) == "topos-answer-support-judge/v3"


# --- WS0 13 Oct: the question-item rule ------------------------------------------------------------------------------

ASKED_REST = _message("7", "How long should I rest between hangboard sets?", source="chatgpt_file_ingestion")
PHYSIO = _message("8", "My physio said wrist tendon inflammation, how long before I can climb again?")


def test_the_question_item_rule_is_pinned_and_reads_the_items_text():
    from topos.permissions_v2.answer_generation import ASKING_PHRASES, item_body, question_item
    assert ASKING_PHRASES == ("asked", "wondered", "looked into", "considered", "researched", "was thinking about", "wanted to know")
    prompt = build_prompt("How long does the owner rest?", [ASKED_REST, TOMATOES, PHYSIO], precision="none")
    assert item_body(prompt, 1) == ASKED_REST.content and item_body(prompt, 2) == TOMATOES.content
    assert question_item(prompt, 1) and question_item(prompt, 3) and not question_item(prompt, 2)
    # Q1: a statement with a trailing question is a statement item; a question the clip cut short is read unclipped
    mixed = _message("5", "Finished the lamp rewiring tonight. Does the switch still crackle for you?")
    semi = _message("3", "Finished the lamp rewiring; does the switch still crackle for you?")
    colon = _message("4", "One thing: does the switch still crackle for you?")
    both = build_prompt("x", [semi, colon], precision="none")
    assert not question_item(both, 1) and question_item(both, 2)                       # ";" ends a sentence, ":" does not
    long_q = _message("6", "Could you tell me, given that " + "the harbour silt moves with every spring tide and " * 12 + "we sketch at dusk, how long the sketches take?")
    assert len(long_q.content) > 420
    prompt = build_prompt("x", [mixed, long_q], precision="none")
    assert not question_item(prompt, 1) and not question_item(prompt, 1, [mixed, long_q])
    assert not question_item(prompt, 2) and question_item(prompt, 2, [mixed, long_q])


def test_a_question_item_supports_only_the_asking(monkeypatch):
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    question = "How long does the owner rest between hangboard sets?"
    for mode in MODES:
        # the asking restates the question by its nature: rule (b) of the non-answer filter exempts it (question
        # items only, an asking phrase); a meta restatement citing a statement item still drops
        _prompt, checked = _checked(question, [ASKED_REST, PHYSIO], "They asked an assistant how long to rest between sets [1].", mode)
        assert checked.body.outcome == "answered" and checked.dropped_question_item == 0 and checked.dropped_non_answer == 0, mode
        _prompt, checked = _checked(question, [ASKED_REST, TOMATOES], "They asked how long the owner rests between hangboard sets [2].", mode)
        assert checked.dropped_non_answer == 1
        _prompt, checked = _checked(question, [ASKED_REST, PHYSIO], "They discussed their recovery time with a physiotherapist [2].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_question_item == 1 and checked.reason == "all_sentences_dropped"
        _prompt, checked = _checked(question, [ASKED_REST, PHYSIO], "They rest two minutes between sets [1, 2].", mode)
        assert checked.dropped_question_item == 1
        _prompt, checked = _checked(question, [ASKED_REST, PHYSIO], "They wondered how long to rest [1] and looked into their wrist [2].", mode)
        assert checked.body.outcome == "answered" and checked.dropped_question_item == 0
    # a statement item is unaffected, alone or beside a question item
    _prompt, checked = _checked("What has the owner been doing?", [TOMATOES], "They re-potted the tomatoes [1].", "only")
    assert checked.body.outcome == "answered" and checked.dropped_question_item == 0
    _prompt, checked = _checked("What has the owner been doing?", [ASKED_REST, TOMATOES], "They re-potted tomatoes and train on a hangboard [1, 2].", "only")
    assert checked.dropped_question_item == 0
    # the rule is the candidate's companion: with the topic-word rule on, today's behaviour
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")
    _prompt, checked = _checked("How long does the owner rest between hangboard sets?", [ASKED_REST],
                                "They rest two minutes between hangboard sets [1].", "only")
    assert checked.dropped_question_item == 0


def test_the_question_item_counter_rides_on_the_receipt_while_the_rule_is_off(legacy, tmp_path, monkeypatch):
    async def judge(sentence, items, *, deadline, question=None):
        return True

    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    body, receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body["outcome"] == "answered" and _receipt_sentences(receipt)["dropped_question_item"] == 0


def test_the_unclipped_read_reaches_the_rule_through_the_sieve_and_the_service(legacy, tmp_path, monkeypatch):
    """Reviewer mutants P12a/b: the clip can cut the "?" off a long question item; the sieve passes the records, and
    so does the service (it calls the same sieve)."""
    from topos.permissions_v2 import answer_generation as ag
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    long_q = _message("6", "How long do the harbour sketches take, given that " + "the silt moves with every spring tide and " * 14 + "we draw at dusk?")
    assert len(long_q.content) > 420
    prompt = build_prompt("How long do the sketches take?", [long_q], precision="none")
    assert not ag.question_item(prompt, 1) and ag.question_item(prompt, 1, [long_q])
    checked = post_check_answer("The sketches take an hour [1].", [long_q], prompt, mode="only", boundary=Open())
    assert checked.dropped_question_item == 1 and checked.body.outcome == "no_answer"
    checked = post_check_answer("They asked how long the sketches take [1].", [long_q], prompt, mode="only", boundary=Open())
    assert checked.dropped_question_item == 0 and checked.body.outcome == "answered"
    seen = []
    original = ag.says_more_than_asked

    def spy(sentence, prompt, records=None):
        seen.append(records is not None and len(records) > 0)
        return original(sentence, prompt, records)

    monkeypatch.setattr(ag, "says_more_than_asked", spy)

    async def judge(sentence, items, *, deadline, question=None):
        return True

    body, _receipt = _service_path(legacy, tmp_path, monkeypatch, judge, "The owner is working on a synthetic task [1].")
    assert body["outcome"] == "answered" and seen == [True]                      # the service hands the records to the rule


def test_the_asking_exemption_of_the_restatement_rule_is_live(monkeypatch):
    """P13: under the subset rule the allowed asking would be a restatement ("asked", "long", "rest", "between",
    "sets" add nothing to the question); the exemption keeps it, and only when the cited items are all questions."""
    from topos.permissions_v2.answer_generation import _restates, non_answer
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "false")
    question = "How long does the owner rest between hangboard sets?"
    prompt = build_prompt(question, [ASKED_REST, TOMATOES], precision="none")
    sentence = "They asked how long to rest between the sets"
    assert _restates(sentence + " [1].", question)                               # a restatement by the subset rule ...
    assert not non_answer(sentence + " [1].", prompt)                            # ... exempt: a question item and "asked"
    assert non_answer(sentence + " [2].", prompt)                                # a statement item: no exemption
    assert non_answer("They rest between the hangboard sets [1].", prompt)       # no asking phrase: no exemption
