"""Lever 4b / G2 (16 Oct 2026, the owner's "build the tense guard now"): a plan stated as a deed.

The dense writer restates a cited item's intention as a completed act: "I will design the shelf labels" comes back as
"The owner designed the shelf labels [1]", "she will organise the rota" as "organised the rota", "I agreed to help
record the oral histories" as "helped record". `answer_tense.plan_stated_as_deed` is the closed, pure rule;
`answer_generation.sieve_answer` runs it last, after the out-of-scope guard and before the judge, while
`ANSWER_TENSE_GUARD` is on (off by default), and counts `dropped_plan_as_deed`. Off: no check and the receipt of
before, byte for byte.

Invented data only: no names, no model.
"""
from __future__ import annotations

import inspect
import sys
import types

import pytest

from tests.permissions_v2.test_answer_quality_aq158 import Open, _message
from topos.permissions_v2 import answer_generation as ag, answer_tense as tense, switches
from topos.permissions_v2.answer_generation import (build_prompt, item_text, judge_sentences, post_check_answer,
                                                    sieve_answer, states_plan_as_deed)

P = switches.PREFIX
GUARD = P + "ANSWER_TENSE_GUARD"
QUESTION = "What has the owner been working on?"


@pytest.fixture(autouse=True)
def guard_unset(monkeypatch):
    monkeypatch.delenv(GUARD, raising=False)


def _on(monkeypatch):
    monkeypatch.setenv(GUARD, "true")


# --- the pure rule ---------------------------------------------------------------------------------------------------

WILL_DESIGN = "I will design the shelf labels for the reading room next week."
DESIGNED = "Designed the shelf labels for the reading room today."
RECORD = "I agreed to help record the oral histories at the library."
# Replay cases (16 Oct 2026; the density fixture's notes, with each person's name put as a role).
LIBRARY = ("My neighbour and I agreed to open the harbour seed library this autumn. I will design the shelf labels and "
           "she will organise the volunteer rota.")
FINISHED = ("I finished the first batch of forty shelf labels for the seed library, grouped by sowing season, and "
            "printed a test sheet.")
INVENTORY = ("The inventory app now has a search box, a low-stock list and a CSV export. I still need to add a simple "
             "login for volunteers.")
LANTERN = "For the lantern walk I will plan the route and workshops, and a volunteer will run the programme."

# (sentence, the texts of the items it cites, id)
DROPS = [
    ("The owner designed the shelf labels [1].", [WILL_DESIGN], "will design -> designed"),
    ("She organised the volunteer rota [1].", ["She will organise the volunteer rota for the seed swap."],
     "she will organise -> organised (British)"),
    ("She organized the volunteer rota [1].", ["She will organise the volunteer rota for the seed swap."],
     "she will organise -> organized (American)"),
    ("She organised the volunteer rota [1].", ["She\u2019ll organise the volunteer rota."], "'ll, curly apostrophe"),
    ("The owner helped record the oral histories [1].", [RECORD], "agreed to help record -> helped record"),
    ("The owner recorded the oral histories [1].", [RECORD], "agreed to help record -> recorded"),
    ("The owner stopped the leaflet run [1].", ["We plan to stop the leaflet run after the autumn fair."],
     "plan to stop -> stopped"),
    ("The owner wrote the grant summary [1].", ["I'm going to write the grant summary tonight."],
     "going to write -> wrote"),
    ("The owner has built a seed cabinet [1].", ["She intends to build a seed cabinet for the swap table."],
     "intends to build -> has built"),
    ("The owner sent the invoice to the print shop [1].", ["I promised to send the invoice to the print shop."],
     "promised to send -> sent"),
    ("The owner set up the stall [1].", ["I will set up the stall on Saturday."], "set/set from the table"),
    ("The owner painted the hall [1].", ["I will start painting the hall."], "helper and -ing: start painting"),
    ("The owner organised the rota [1].", ["I'll be organising the rota."], "will be organising"),
    ("The owner has run the stall [1].", ["I will run the stall."], "run: a participle as the base, after has"),
    ("The shelf labels were designed by the owner [1].", [WILL_DESIGN], "passive"),
    ("The owner designed the shelf labels [1, 2].", [WILL_DESIGN, "The reading room opens in March."],
     "one item intends, the other is silent"),
    ("The owner has also labelled the jars [1].", ["I will label the jars."], "doubled l and an adverb"),
    ("The owner tidied the seed shelf [1].", ["I need to tidy the seed shelf."], "need to tidy -> tidied"),
    # v2 replay: still dropped.
    ("She organized the volunteer rota [2].", [LIBRARY], "replay: she will organise -> organized"),
    ("The owner planned the route and lantern workshops while a volunteer handled the programme [1].", [LANTERN],
     "replay: will plan the route -> planned the route"),
    ("My neighbour organised the volunteer rota while the owner designed shelf labels [1].", [LIBRARY],
     "replay: designed shelf labels citing only the plan"),
]
KEEPS = [
    ("The owner plans to design the shelf labels [1].", [WILL_DESIGN], "plans to design"),
    ("The owner will design the shelf labels [1].", [WILL_DESIGN], "will design"),
    ("The owner is designing the shelf labels [1].", [WILL_DESIGN], "is designing"),
    ("The owner designed the shelf labels [1].", ["I designed the shelf labels for the reading room."],
     "the item says I designed"),
    ("The owner designed the shelf labels [1, 2].", [WILL_DESIGN, DESIGNED], "will design beside designed"),
    ("The owner designed the shelf labels.", [], "no cited item"),
    ("The owner printed the shelf labels [1].", [WILL_DESIGN], "a different verb than the intention"),
    ("The owner read the oral history transcripts [1].", ["I will read the oral history transcripts."],
     "read/read is skipped"),
    ("The owner was going to design the shelf labels [1].", [WILL_DESIGN], "was going to design"),
    ("The owner intended to design the shelf labels [1].", [WILL_DESIGN], "intended to design"),
    ("The owner agreed to design the shelf labels [1].", [WILL_DESIGN], "agreed to design"),
    ("The owner will have designed the shelf labels by Friday [1].", [WILL_DESIGN], "will have designed"),
    ("The owner plans to set up the stall [1].", ["I will set up the stall on Saturday."], "plans to set up"),
    ("The owner has not designed the shelf labels yet [1].", [WILL_DESIGN], "has not designed"),
    ("The owner designed the shelf labels [1].", ["I don't want to design the shelf labels."],
     "a negated intention is none"),
    ("They run the stall on Saturdays [1].", ["I will run the stall."], "run in the present"),
    ("The owner planned to write the guide [1].", ["I'm going to plan and write the guide."],
     "planned to: a framing marker, not a deed"),
    ("The owner designed the shelf labels [1].", ["The shelf labels for the reading room need new colours."],
     "no intention in the item"),
    ("The owner designed the shelf labels [1, 2].", ["I designed the poster for the autumn fair.", WILL_DESIGN],
     "the verb done on another object"),
    # v2 replay: the object must match.
    ("They added a search box, low-stock list, and CSV export to the inventory app [4].", [INVENTORY],
     "replay: need to add a login, added a search box"),
    # v2 replay: done in other words.
    ("The owner designed the shelf labels, finished forty of them by sowing season and printed a test sheet [1, 3].",
     [LIBRARY, FINISHED], "replay: designed, and another item finished the labels"),
    ("Having designed labels for the seed library, the owner printed a test sheet [2, 3].", [LIBRARY, FINISHED],
     "replay: having designed labels"),
    ("The shelf labels were designed and printed [1, 2].", [WILL_DESIGN, "The shelf labels were printed on Monday."],
     "done in other words, passive"),
]


def _wrong(rule) -> list[str]:
    """The ids of the cases `rule` gets wrong: a drop it keeps, a keep it drops."""
    return ([case for sentence, items, case in DROPS if rule(sentence, items) is not True]
            + [case for sentence, items, case in KEEPS if rule(sentence, items) is not False])


def test_every_case_of_the_pure_rule():
    assert _wrong(tense.plan_stated_as_deed) == []


@pytest.mark.parametrize("sentence,items,case", DROPS, ids=[row[2] for row in DROPS])
def test_a_deed_the_cited_items_give_only_as_an_intention_is_dropped(sentence, items, case):
    assert tense.plan_stated_as_deed(sentence, items) is True


@pytest.mark.parametrize("sentence,items,case", KEEPS, ids=[row[2] for row in KEEPS])
def test_what_the_rule_never_drops(sentence, items, case):
    assert tense.plan_stated_as_deed(sentence, items) is False


def test_the_rule_reads_no_switch_and_no_environment(monkeypatch):
    """Offline replay: the same verdicts with the switch on, off and unreadable."""
    for value in ("true", "false", "maybe"):
        monkeypatch.setenv(GUARD, value)
        assert _wrong(tense.plan_stated_as_deed) == []
    assert "switches" not in vars(tense) and "os" not in vars(tense)


def test_the_verb_forms():
    assert set(tense.deed_forms("design")) == {"designed"}
    assert set(tense.deed_forms("organise")) == {"organised", "organized"}
    assert set(tense.deed_forms("organize")) == {"organized", "organised"}
    assert "planned" in tense.deed_forms("plan") and "stopped" in tense.deed_forms("stop")
    assert set(tense.deed_forms("tidy")) == {"tidied"} and set(tense.deed_forms("play")) == {"played"}
    assert {"labelled", "labeled"} <= set(tense.deed_forms("label"))
    assert tense.deed_forms("write") == {"wrote": False, "written": False}
    assert tense.deed_forms("run") == {"ran": False, "run": True}          # "run" only after has/have/had/'ve
    assert tense.deed_forms("set") == {"set": False} and tense.deed_forms("fit") == {"fitted": False}
    for skipped in ("read", "cut", "let", "hit", "be", "have"):
        assert tense.deed_forms(skipped) == {}


def test_the_intended_verbs():
    assert tense.intended_verbs(RECORD) == {"help", "record"}
    assert tense.intended_verbs("I'll be organising the rota.") >= {"organise"}
    assert tense.intended_verbs("I will design and print the labels.") == {"design", "print"}
    assert tense.intended_verbs("I will also design the labels.") == {"design"}
    assert tense.intended_verbs("I will not design the labels.") == frozenset()
    assert tense.intended_verbs("I'm not going to design the labels.") == frozenset()
    assert tense.intended_verbs("I designed the labels.") == frozenset()


def test_the_objects_and_their_content_words():
    """v2: an intention's object runs from its last verb to the clause's end; content words are four letters or more,
    folded, and none of the stopwords."""
    assert tense.intention_objects(LIBRARY) == {"open": {"harbour", "seed", "library", "autumn"},
                                                "design": {"shelf", "label"}, "organise": {"volunteer", "rota"}}
    assert tense.intention_objects(INVENTORY) == {"add": {"simpl", "login", "volunteer"}}    # the checks' fold
    assert tense.intention_objects(RECORD) == {"help": {"oral", "history", "library"},
                                               "record": {"oral", "history", "library"}}
    assert tense.intention_objects("I will design and print the labels.") == {"design": {"label"}, "print": {"label"}}
    assert tense.intention_objects("I will help.") == {"help": frozenset()}
    assert tense.content_words(["the", "box", "next", "week", "owner", "jars", "histories"]) == {"jar", "history"}


def test_the_closed_lists_are_pinned():
    assert tense.INTENTION_MARKERS == (
        ("will",), ("shall",),
        ("going", "to"), ("plan", "to"), ("plans", "to"), ("planning", "to"), ("intend", "to"), ("intends", "to"),
        ("want", "to"), ("wants", "to"), ("hope", "to"), ("aim", "to"), ("mean", "to"),
        ("agreed", "to"), ("offered", "to"), ("promised", "to"), ("need", "to"), ("have", "to"), ("has", "to"))
    assert tense.FRAMING_MARKERS[:len(tense.INTENTION_MARKERS)] == tense.INTENTION_MARKERS
    assert len(tense.FRAMING_MARKERS) == len(tense.INTENTION_MARKERS) + 35
    assert set(tense.IRREGULAR) == {
        "write", "make", "build", "send", "buy", "take", "give", "run", "meet", "bring", "get", "lead", "draw",
        "choose", "begin", "sell", "pay", "find", "hold", "teach", "tell", "speak", "ride", "go", "do", "leave",
        "keep", "set", "put", "hang", "fit", "stick", "sweep", "feed"}
    assert {"read", "cut", "let", "hit"} <= tense.SKIP and not tense.SKIP & set(tense.IRREGULAR)
    assert tense.HELPERS == {"help", "try", "start", "begin", "finish", "go", "come"}
    assert tense.CLAUSE_ENDS == {"and", "or", "but", "while", "whereas", "because", "although", "though", "when", "so",
                                 "then", "after", "before", "until"}
    assert len(tense.OBJECT_STOPWORDS) == 151 and {"owner", "week", "today", "with", "from"} <= tense.OBJECT_STOPWORDS
    assert tense.PASSIVE == {"is", "are", "was", "were", "be", "been", "being", "get", "got", "gets"}
    assert {"need", "seed", "speed", "shed"} <= tense.NOT_PAST


# --- on the node: the post-check -------------------------------------------------------------------------------------

SHELF = _message("1", WILL_DESIGN)
ROTA = _message("2", "She will organise the volunteer rota for the seed swap.")
LAMP = _message("3", "Rewired the reading lamp on Sunday; the switch still crackles.")


def _goal(digit, content, support):
    from topos.permissions_v2.knowledge_contract import GoalResult
    rid = "r." + digit * 64
    return GoalResult.parse({"kind": "goal", "status": "stated_intention", "record_id": rid, "content": content,
                             "source_ids": ["journal"],
                             "citations": [{"record_id": rid, "source_id": "journal", "content": support}]})


def _checked(sentences, records=(SHELF, ROTA, LAMP), *, support=None, module=ag):
    prompt = build_prompt(QUESTION, list(records), precision="none")
    return module.post_check_answer("\n".join(sentences), list(records), prompt, mode="with_sources",
                                    boundary=Open(), support=support)


ANSWER = ["The owner designed the shelf labels [1].", "The volunteer rota was organised for the seed swap [2].",
          "The owner rewired the reading lamp [3]."]


def test_on_the_guard_drops_each_plan_stated_as_a_deed_and_counts_it(monkeypatch):
    _on(monkeypatch)
    checked = _checked(ANSWER)
    assert checked.kept == 1 and checked.dropped_plan_as_deed == 2 and checked.reason == "answered"
    assert checked.body.answer == "The owner rewired the reading lamp [1]."


def test_off_by_default_nothing_is_dropped_and_the_counter_is_zero(monkeypatch):
    assert switches.on(GUARD) is False
    checked = _checked(ANSWER)
    assert checked.kept == 3 and checked.dropped_plan_as_deed == 0
    monkeypatch.setenv(GUARD, "false")
    assert _checked(ANSWER) == checked


def test_when_nothing_is_kept_the_answer_is_all_sentences_dropped(monkeypatch):
    _on(monkeypatch)
    checked = _checked(ANSWER[:2])
    assert checked.body.outcome == "no_answer" and checked.reason == "all_sentences_dropped"
    assert checked.kept == 0 and checked.dropped_plan_as_deed == 2


def test_the_guard_reads_the_item_as_the_writer_saw_it_supporting_text_included(monkeypatch):
    """A goal's body says what; its supporting text says "I will". Both were in the prompt, so both are read."""
    goal = _goal("4", "Design the shelf labels for the reading room.", "I will design the shelf labels this month.")
    prompt = build_prompt(QUESTION, [goal, LAMP], precision="none")
    assert item_text(prompt, 1) == ("Design the shelf labels for the reading room.\n"
                                    "I will design the shelf labels this month.")
    assert item_text(prompt, 2) == LAMP.content                              # no header line, no kind, no date
    _on(monkeypatch)
    checked = _checked(["The owner designed the shelf labels [1].", "The owner rewired the reading lamp [2]."],
                       (goal, LAMP))
    assert checked.dropped_plan_as_deed == 1 and checked.kept == 1


def test_the_guard_runs_after_the_other_checks_and_before_the_judge(monkeypatch):
    _on(monkeypatch)
    prompt = build_prompt(QUESTION, [SHELF, ROTA, LAMP], precision="none")
    sieved = sieve_answer("\n".join(ANSWER + ["The owner fixed a bike [9]."]), [SHELF, ROTA, LAMP], prompt,
                          mode="with_sources", boundary=Open())
    assert sieved.dropped_citation == 1 and sieved.dropped_plan_as_deed == 2
    assert sieved.sentences == ("The owner rewired the reading lamp [3].",)
    seen = []
    judge_sentences(sieved, prompt, lambda sentence, items, question, **extra: seen.append(sentence) or True)
    assert seen == ["The owner rewired the reading lamp [3]."]


def test_the_replay_cases_through_the_post_check(monkeypatch):
    """v2 on the node: the labels finished in another cited item and the search box nobody intended stand; the rota
    stated as organised drops."""
    _on(monkeypatch)
    records = [_message("5", LIBRARY), _message("6", FINISHED), _message("7", INVENTORY)]
    checked = _checked(["The owner designed the shelf labels and printed a test sheet [1, 2].",
                        "They added a search box and a CSV export to the inventory app [3].",
                        "She organized the volunteer rota [1]."], records)
    assert checked.dropped_plan_as_deed == 1 and checked.kept == 2


def test_a_sentence_citing_another_item_than_the_intention_stands(monkeypatch):
    """The verb is intended in item 1, which this sentence does not cite: the guard reads only the cited items."""
    _on(monkeypatch)
    prompt = build_prompt(QUESTION, [SHELF, LAMP], precision="none")
    assert states_plan_as_deed("The owner designed the shelf labels [1].", prompt) is True
    assert states_plan_as_deed("The owner designed the shelf labels [2].", prompt) is False
    assert states_plan_as_deed("The owner designed the shelf labels.", prompt) is False
    assert states_plan_as_deed("The owner designed the shelf labels [7].", prompt) is False


# --- through the service: the receipt ----------------------------------------------------------------------------------

BODIES = ["Working on the reading room: I will design the shelf labels next week.",
          "Working on the seed library and rewired the reading lamp on Sunday."]


def _service_ask(tmp_path, answer):
    from tests.permissions_v2.test_answer_time_tr162 import DAY, NOW, _asked, _node
    node = _node(tmp_path, times=[NOW - DAY, NOW - 2 * DAY], bodies=BODIES)
    return _asked(node, QUESTION, answer)


def _number(prompt, words):
    return next(number for number, block in enumerate(prompt.item_lines, 1) if words in block)


def _deed_answer(prompt):
    return f"The owner designed the shelf labels [{_number(prompt, 'shelf labels')}]."


def test_off_the_receipt_has_no_new_key_and_the_answer_is_as_before(tmp_path, monkeypatch):
    # NR179's judge reserve (8 s by default since 1.5.10) adds its `dropped_unjudged` counter, so it is set to 1.5.9's
    # none here, in this test's environment only: this pins the tense guard's own key.
    monkeypatch.setenv(P + "ANSWER_JUDGE_RESERVE_SECONDS", "0")
    body, receipt, _prompts = _service_ask(tmp_path, _deed_answer)
    assert body["outcome"] == "answered" and receipt["reason"] == "answered"
    assert "dropped_plan_as_deed" not in receipt["sentences"]
    assert set(receipt["sentences"]) == {"generated", "kept", "dropped_citation", "dropped_copy",
                                         "dropped_question_echo", "dropped_relevance", "dropped_scrub",
                                         "dropped_support", "dropped_non_answer", "dropped_question_item",
                                         "dropped_out_of_scope"}


def test_on_the_receipt_counts_the_drop_and_the_answer_is_withheld(tmp_path, monkeypatch):
    _on(monkeypatch)
    body, receipt, _prompts = _service_ask(tmp_path, _deed_answer)
    assert body["outcome"] == "no_answer" and receipt["reason"] == "all_sentences_dropped"
    assert receipt["sentences"]["dropped_plan_as_deed"] == 1 and receipt["sentences"]["kept"] == 0


def test_on_a_kept_answer_carries_the_counter_at_zero(tmp_path, monkeypatch):
    _on(monkeypatch)
    body, receipt, _prompts = _service_ask(
        tmp_path, lambda prompt: f"The owner rewired the reading lamp [{_number(prompt, 'reading lamp')}].")
    assert body["outcome"] == "answered" and receipt["sentences"]["dropped_plan_as_deed"] == 0


# --- mutants: each condition is load-bearing ---------------------------------------------------------------------------

def _mutant(monkeypatch, module, original: str, replacement: str):
    """`module` with one line of its source replaced, loaded beside it (relative imports resolve as for the real one;
    registered for the test only, as dataclasses look their module up)."""
    source = inspect.getsource(module)
    assert source.count(original) == 1, original
    mutant = types.ModuleType(module.__name__ + "_mutant")
    mutant.__package__ = module.__package__
    mutant.__file__ = module.__file__
    monkeypatch.setitem(sys.modules, mutant.__name__, mutant)
    exec(compile(source.replace(original, replacement), module.__file__, "exec"), mutant.__dict__)
    return mutant


RULE_MUTANTS = [
    # A cited item that states the same verb as done keeps the sentence.
    ("            continue                                            # a cited item says this verb was done",
     "            pass", "the verb done on another object"),
    # The sentence's own framing ("will have designed") is no deed.
    ("        return not (_negated_before(tokens, at) or _framed(tokens, at, self.ends, self.chain))",
     "        return not _negated_before(tokens, at)", "will have designed"),
    # v2: the deed's object must share a content word with the intention's.
    ("        matching = [words for verbs, words in intentions if verb in verbs and words & deed_object]",
     "        matching = [words for verbs, words in intentions if verb in verbs]",
     "replay: need to add a login, added a search box"),
    # v2: an act done in other words on the intention's object keeps the sentence.
    ("        if any(act & words for words in matching for act in acts):", "        if False:",
     "replay: designed, and another item finished the labels"),
]


@pytest.mark.parametrize("original,replacement,killer", RULE_MUTANTS,
                         ids=["no-done-item", "own-framing", "object-match", "done-in-other-words"])
def test_each_condition_of_the_rule_is_load_bearing(original, replacement, killer, monkeypatch):
    mutant = _mutant(monkeypatch, tense, original, replacement)
    assert killer in _wrong(mutant.plan_stated_as_deed)


def test_the_switch_check_is_load_bearing(monkeypatch):
    """Without the switch check the guard drops with the switch off: the off test above goes red."""
    mutant = _mutant(monkeypatch, ag, "    if pairs and tense_guard_on():", "    if pairs:")
    assert switches.on(GUARD) is False
    assert _checked(ANSWER).dropped_plan_as_deed == 0
    assert _checked(ANSWER, module=mutant).dropped_plan_as_deed == 2
