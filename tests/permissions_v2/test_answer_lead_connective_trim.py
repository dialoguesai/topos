"""Lead connective trim (`ANSWER_LEAD_CONNECTIVE_TRIM`, off): an answer whose lead was dropped no longer opens with
"Additionally,".

The live UI gate of 16 Oct 2026 saw two answers open with a connective ("Additionally, the owner ...", "He also
organised ...") because a check or the judge had dropped the writer's lead sentence, which the second sentence was
written to follow. On, `close_answer` strips one opening connective of a closed list from the first delivered sentence,
only when that sentence is not the writer's first generated sentence (`Sieved.lead`), and capitalises the next word.
It runs after the judge, the Off-limits check and the scrub, before the body is assembled, so no verdict changes. These
tests pin:
  - each connective form of the closed list is trimmed when the lead was dropped (by the citation check, the judge or
    the scrub), and its citation stays;
  - nothing changes when the lead was kept, in the second delivered sentence, mid-sentence, for a word that is not a
    connective ("Also known as"), for an opener not on the list, or when the rest would be empty or open with a
    citation;
  - off (the default): the body and the receipt are byte for byte those of node main 28b7cbbc;
  - mutants of the switch check, the lead-dropped condition and the closed lists each turn a named check red.

Invented data only: no names, no model (the writer is a stand-in, the judge the folder's stand-in or a test's own).
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.test_answer_quality_aq158 import Open, _message
from topos.permissions_v2 import answer_generation as ag, answer_release, switches
from topos.permissions_v2.answer_generation import (ALSO_SUBJECTS, LEAD_CONNECTIVES, Sieved, build_prompt,
                                                    close_answer, lead_dropped, trim_lead_connective, writer_lead)

SWITCH = switches.ANSWER_LEAD_CONNECTIVE_TRIM.name
#: Node main when this lane started (it includes #128, the 1.5.10 answer defaults, and #126): off is byte for byte it.
MAIN = "28b7cbbc1e702b239b473f8fbf5b76ab0a0a32e6"
QUESTION = "What has the owner been working on?"
LAMP = _message("1", "Working on the reading room and rewired the reading lamp on Sunday.")
BIKE = _message("2", "Working on the bike shed and fixed the bike chain on Saturday.")
RECORDS = (LAMP, BIKE)
#: A lead the citation check drops (no citation), then a sentence written to follow it.
PREAMBLE = "Here is what the items say."


@pytest.fixture(autouse=True)
def switch_unset(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


def _on(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


def _checked(lines, *, mode="with_sources", support=None, boundary=None, module=ag):
    prompt = module.build_prompt(QUESTION, list(RECORDS), precision="none")
    return module.post_check_answer("\n".join(lines), list(RECORDS), prompt, mode=mode, boundary=boundary or Open(),
                                    support=support)


# --- the pure rule ----------------------------------------------------------------------------------------------------

#: (as written, as trimmed): every form of the closed lists.
TRIMS = [(connective + " the owner rewired the reading lamp [1].", "The owner rewired the reading lamp [1].")
         for connective in LEAD_CONNECTIVES] + [
    (subject + " also rewired the reading lamp [1].", subject + " rewired the reading lamp [1].")
    for subject in ALSO_SUBJECTS]
#: Never trimmed, whatever happened to the lead.
UNTOUCHED = [
    ("Also known as the long shed, the bike shed was fixed [2].", "'Also' with no comma is no connective"),
    ("Meanwhile, the owner fixed the bike chain [2].", "an opener not on the closed list"),
    ("Volunteers also fixed the bike chain [2].", "a subject not on the closed list before 'also'"),
    ("also, the owner fixed the bike chain [2].", "the list is matched as written, capitalised"),
    ("The owner fixed the bike chain and also, additionally, oiled the gears [2].", "mid-sentence words"),
    ("Also, [2] the owner fixed the bike chain.", "the rest would open with a citation"),
    ("Additionally, [1, 2].", "the rest would be a bare citation"),
    ("He also [2].", "'also' followed by a citation"),
    ("Also,", "the rest would be empty"),
    ("Also, iOS builds were fixed [2].", "the next word opens in lower case with a capital inside it"),
    ("Also,the owner fixed the bike chain [2].", "no space after the comma"),
    ("The owner fixed the bike chain [2].", "no connective at all"),
]


def check_each_form_is_trimmed(trim):
    for written, trimmed in TRIMS:
        assert trim(written) == trimmed, written


def check_what_is_never_trimmed(trim):
    for written, case in UNTOUCHED:
        assert trim(written) == written, case


@pytest.mark.parametrize("written,trimmed", TRIMS, ids=[row[0].split(" ")[0] + "-" + row[0].split(" ")[1]
                                                        for row in TRIMS])
def test_each_connective_form_is_trimmed_and_the_next_word_capitalised(written, trimmed):
    assert trim_lead_connective(written) == trimmed


@pytest.mark.parametrize("written,case", UNTOUCHED, ids=[row[1] for row in UNTOUCHED])
def test_what_the_rule_never_trims(written, case):
    assert trim_lead_connective(written) == written


def test_citations_and_a_bullet_prefix_stay_as_written():
    assert trim_lead_connective("Furthermore, the owner fixed the bike chain [1, 2].") == \
        "The owner fixed the bike chain [1, 2]."
    assert trim_lead_connective("- Also, the owner fixed the bike chain [2].") == \
        "- The owner fixed the bike chain [2]."
    assert trim_lead_connective("2. They also fixed the bike chain [2].") == "2. They fixed the bike chain [2]."
    assert trim_lead_connective("In addition, 2026 began with the bike shed [2].") == \
        "2026 began with the bike shed [2]."
    assert trim_lead_connective("Moreover, ISO audits of the bike shed passed [2].") == \
        "ISO audits of the bike shed passed [2]."
    assert trim_lead_connective("As well, Saturday's repair held [2].") == "Saturday's repair held [2]."


def test_only_one_connective_comes_off():
    assert trim_lead_connective("Also, the owner also fixed the bike chain [2].") == \
        "The owner also fixed the bike chain [2]."


def test_the_closed_lists_are_pinned():
    assert LEAD_CONNECTIVES == ("Additionally,", "Also,", "In addition,", "Furthermore,", "Moreover,", "Besides,",
                                "As well,")
    assert ALSO_SUBJECTS == ("I", "You", "He", "She", "It", "We", "They", "The owner")


def test_the_rule_reads_no_switch(monkeypatch):
    def never(*_args, **_kwargs):
        raise AssertionError("the pure rule read a switch")
    monkeypatch.setattr(switches, "on", never)
    check_each_form_is_trimmed(trim_lead_connective)


def test_the_writers_lead_is_split_as_the_citation_check_splits_it():
    assert writer_lead("\n\n  Here is what the items say. Additionally, more [1].\nNext [2].") == PREAMBLE
    assert writer_lead("- Also, the owner fixed it [2]. Then more [1].") == "- Also, the owner fixed it [2]."
    assert writer_lead("-\nThe owner fixed it [2].") == "-"           # a sentence the citation check counts and drops
    assert writer_lead("") == writer_lead("\n \n") == ""


# --- on the node: close_answer -------------------------------------------------------------------------------------

def check_trimmed_when_the_lead_was_dropped(module, monkeypatch):
    """On: the citation check drops the lead (no citation); each connective form comes off the first delivered
    sentence, its citation stays (renumbered as any with-sources body is), and the receipt flag says so."""
    _on(monkeypatch)
    for connective in LEAD_CONNECTIVES:
        checked = _checked([PREAMBLE, connective + " the owner rewired the reading lamp [1].",
                            "The owner fixed the bike chain [2]."], module=module)
        assert checked.lead_connective_trimmed is True, connective
        assert checked.body.answer == "The owner rewired the reading lamp [1].\nThe owner fixed the bike chain [2].", \
            connective
        assert checked.dropped_citation == 1 and checked.kept == 2


def check_untouched_when_the_lead_was_kept(module, monkeypatch):
    """On: the writer's own first sentence opens with "Also," and is kept: it stays as written."""
    _on(monkeypatch)
    checked = _checked(["Also, the owner rewired the reading lamp [1].", "Additionally, the owner fixed the bike chain "
                        "[2]."], module=module)
    assert checked.lead_connective_trimmed is False
    assert checked.body.answer == ("Also, the owner rewired the reading lamp [1].\n"
                                   "Additionally, the owner fixed the bike chain [2].")


def check_untouched_with_the_switch_off(module, monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    checked = _checked([PREAMBLE, "Additionally, the owner rewired the reading lamp [1]."], module=module)
    assert checked.lead_connective_trimmed is False
    assert checked.body.answer == "Additionally, the owner rewired the reading lamp [1]."


def check_also_known_as_is_no_connective(module, monkeypatch):
    _on(monkeypatch)
    checked = _checked([PREAMBLE, "Also known as the long shed, the bike shed was fixed [2]."], module=module)
    assert checked.body.answer == "Also known as the long shed, the bike shed was fixed [1]."


def check_an_opener_off_the_list_is_untouched(module, monkeypatch):
    _on(monkeypatch)
    for sentence in ("Meanwhile, the owner fixed the bike chain [2].", "Volunteers also fixed the bike chain [2]."):
        checked = _checked([PREAMBLE, sentence], module=module)
        assert checked.body.answer == sentence.replace("[2]", "[1]") and checked.lead_connective_trimmed is False


def test_each_connective_form_is_trimmed_when_the_lead_was_dropped(monkeypatch):
    check_trimmed_when_the_lead_was_dropped(ag, monkeypatch)


def test_nothing_changes_when_the_lead_was_kept(monkeypatch):
    check_untouched_when_the_lead_was_kept(ag, monkeypatch)


def test_nothing_changes_with_the_switch_off(monkeypatch):
    assert switches.on(switches.ANSWER_LEAD_CONNECTIVE_TRIM) is False
    check_untouched_with_the_switch_off(ag, monkeypatch)


def test_also_known_as_is_untouched(monkeypatch):
    check_also_known_as_is_no_connective(ag, monkeypatch)


def test_an_opener_off_the_closed_lists_is_untouched(monkeypatch):
    check_an_opener_off_the_list_is_untouched(ag, monkeypatch)


def test_the_pronoun_form_through_the_post_check(monkeypatch):
    _on(monkeypatch)
    checked = _checked([PREAMBLE, "He also fixed the bike chain [2].", "The owner rewired the reading lamp [1]."])
    assert checked.body.answer == "He fixed the bike chain [1].\nThe owner rewired the reading lamp [2]."
    assert checked.lead_connective_trimmed is True


def test_a_connective_in_the_second_delivered_sentence_is_untouched(monkeypatch):
    _on(monkeypatch)
    checked = _checked([PREAMBLE, "The owner rewired the reading lamp [1].",
                        "Also, the owner fixed the bike chain [2]."])
    assert checked.lead_connective_trimmed is False
    assert checked.body.answer == "The owner rewired the reading lamp [1].\nAlso, the owner fixed the bike chain [2]."


def test_a_lead_the_judge_drops_counts_as_dropped(monkeypatch):
    _on(monkeypatch)

    def support(sentence, items, question, **_):
        return "reading lamp" not in sentence
    checked = _checked(["The owner rewired the reading lamp [1].", "Also, the owner fixed the bike chain [2]."],
                       support=support)
    assert checked.dropped_support == 1 and checked.dropped_citation == 0
    assert checked.body.answer == "The owner fixed the bike chain [1]." and checked.lead_connective_trimmed is True


def test_the_judge_reads_every_sentence_as_written(monkeypatch):
    """The trim runs after the judge: the judge is asked about the untrimmed sentence."""
    _on(monkeypatch)
    seen = []

    def support(sentence, items, question, **_):
        seen.append(sentence)
        return True
    checked = _checked([PREAMBLE, "Additionally, the owner rewired the reading lamp [1]."], support=support)
    assert seen == ["Additionally, the owner rewired the reading lamp [1]."]
    assert checked.body.answer == "The owner rewired the reading lamp [1]."


def test_a_lead_the_scrub_drops_counts_as_dropped(monkeypatch):
    _on(monkeypatch)
    checked = _checked(["The owner tagged one folder nsfw [1].", "Moreover, the owner fixed the bike chain [2]."])
    assert checked.dropped_scrub == 1 and checked.body.answer == "The owner fixed the bike chain [1]."
    assert checked.lead_connective_trimmed is True


def test_answers_only_strips_the_citation_as_before(monkeypatch):
    """Answers only: the citation is struck off at assembly, as it always is; the trim never touches it."""
    _on(monkeypatch)
    checked = _checked([PREAMBLE, "Besides, the owner repaired the bike [2]."], mode="only")
    assert checked.body.answer == "The owner repaired the bike ."
    assert checked.cited == 1


def test_a_trim_that_would_name_an_off_limits_entry_is_left_as_written(monkeypatch):
    """The trimmed body must pass the same Off-limits check as the body as written; else it is left as written."""
    _on(monkeypatch)

    class Capitalised:
        def mentions_protected(self, *texts):
            return any("Rose beds" in text for text in texts)
    lines = [PREAMBLE, "Additionally, rose beds were dug beside the reading room [1]."]
    checked = _checked(lines, boundary=Capitalised())
    assert checked.body.answer == "Additionally, rose beds were dug beside the reading room [1]."
    assert checked.lead_connective_trimmed is False
    assert _checked(lines).body.answer == "Rose beds were dug beside the reading room [1]."


def test_a_sieved_built_without_its_lead_is_never_trimmed(monkeypatch):
    """A caller that builds its own `Sieved` (a battery) carries no lead: unknown is never "dropped"."""
    _on(monkeypatch)
    prompt = build_prompt(QUESTION, list(RECORDS), precision="none")
    sieved = Sieved(2, ("Additionally, the owner rewired the reading lamp [1].",), 1, 0, 0, 0)
    assert lead_dropped(sieved) is False
    checked = close_answer(sieved, list(RECORDS), prompt, mode="with_sources", boundary=Open())
    assert checked.body.answer == "Additionally, the owner rewired the reading lamp [1]."


def test_the_lead_dropped_condition():
    lead = "The owner rewired the reading lamp [1]."
    assert lead_dropped(Sieved(1, (lead,), 0, 0, 0, 0, lead=lead)) is False
    assert lead_dropped(Sieved(2, ("Also, more [2].",), 0, 0, 0, 0, lead=lead)) is True
    assert lead_dropped(Sieved(1, (), 0, 0, 0, 0, lead=lead)) is True
    nsfw = "The owner tagged one folder nsfw [1]."
    assert lead_dropped(Sieved(1, (nsfw,), 0, 0, 0, 0, lead=nsfw)) is True      # the scrub drops it


def test_off_the_body_and_counters_are_those_of_node_main(monkeypatch):
    """Off: `post_check_answer` of node main and of this tree give the same body and the same counters, over replies
    the trim would change; on, the bodies differ (the comparison can see the change)."""
    main = ft.module_at(MAIN, "answer_generation")
    assert not hasattr(main, "trim_lead_connective")
    replies = [[PREAMBLE, connective + " the owner rewired the reading lamp [1].", "Also, the owner fixed the bike "
                "chain [2]."] for connective in LEAD_CONNECTIVES]
    replies += [[PREAMBLE, "He also fixed the bike chain [2]."], ["Also, the owner rewired the reading lamp [1]."]]
    counters = ("generated", "kept", "dropped_citation", "dropped_copy", "dropped_question_echo", "dropped_relevance",
                "dropped_scrub", "cited", "reason", "dropped_support", "dropped_non_answer", "dropped_question_item",
                "dropped_out_of_scope", "dropped_to_fit")
    for mode in ("only", "with_sources"):
        for reply in replies:
            now, then = _checked(reply, mode=mode), _checked(reply, mode=mode, module=main)
            assert now.body.model_dump_json() == then.body.model_dump_json(), reply
            assert [getattr(now, key) for key in counters] == [getattr(then, key) for key in counters]
    _on(monkeypatch)
    assert _checked(replies[0]).body.model_dump_json() != _checked(replies[0], module=main).body.model_dump_json()


# --- through the service: the receipt ---------------------------------------------------------------------------------

BODIES = ["Working on the reading room and rewired the reading lamp on Sunday.",
          "Working on the bike shed and fixed the bike chain on Saturday."]


def _number(prompt, words):
    return next(number for number, block in enumerate(prompt.item_lines, 1) if words in block)


def _dropped_lead(prompt):
    return (f"{PREAMBLE} Additionally, the owner rewired the reading lamp [{_number(prompt, 'reading lamp')}]. "
            f"Also, the owner fixed the bike chain [{_number(prompt, 'bike chain')}].")


def _kept_lead(prompt):
    return f"Also, the owner rewired the reading lamp [{_number(prompt, 'reading lamp')}]."


def _service(node, writer, cls=None):
    async def generate(prompt, *, deadline):
        return writer(prompt)

    async def judge(sentence, items, *, deadline, question=None, context=None):
        return True                       # a stand-in "yes", handed in, so no service class binds the real judge
    runtime = SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search)
    service = (cls or answer_release.AnswerService)(runtime, clock=lambda: node.now[0], generate=generate,
                                                    judge=judge)
    service._enabled = lambda: True
    return service


def _asked(node, writer, cls=None):
    """One signed ask and its fetch through the real service: (the fetched body, the receipt, the decision row)."""
    from tests.permissions_v2.test_answer_release import _ask, _finished
    service = _service(node, writer, cls)
    try:
        body = _finished(node, service, _ask(node, service, QUESTION))
        with node.ledger._transaction() as db:
            row = db.execute("SELECT receipt_json, decision_json FROM p2a_receipts ORDER BY rowid DESC LIMIT 1"
                             ).fetchone()
    finally:
        service.close()
    return body, json.loads(row[0]), row[1]


@pytest.fixture
def node(tmp_path):
    from tests.permissions_v2.test_answer_time_tr162 import DAY, NOW, _node
    return _node(tmp_path, times=[NOW - DAY, NOW - 2 * DAY], bodies=BODIES)


def check_off_the_receipt_has_no_new_key(node, cls=None):
    body, receipt, _row = _asked(node, _dropped_lead, cls)
    assert body["outcome"] == "answered" and receipt["reason"] == "answered", receipt["reason"]
    assert body["answer"].startswith("Additionally, the owner rewired the reading lamp")
    assert "lead_connective_trimmed" not in receipt


def test_off_the_receipt_has_no_new_key_and_the_answer_is_as_written(node):
    check_off_the_receipt_has_no_new_key(node)


def test_on_the_receipt_says_trimmed_and_the_answer_opens_without_the_connective(node, monkeypatch):
    _on(monkeypatch)
    body, receipt, _row = _asked(node, _dropped_lead)
    assert body["answer"] == "The owner rewired the reading lamp .\nAlso, the owner fixed the bike chain ."
    assert receipt["lead_connective_trimmed"] is True and receipt["sentences"]["dropped_citation"] == 1


def test_on_a_kept_lead_says_not_trimmed(node, monkeypatch):
    _on(monkeypatch)
    body, receipt, _row = _asked(node, _kept_lead)
    assert body["answer"] == "Also, the owner rewired the reading lamp ."
    assert receipt["lead_connective_trimmed"] is False


def test_off_bodies_receipts_and_decision_rows_are_byte_for_byte_those_of_node_main(node, monkeypatch):
    """The answer service of node main (its answer_release with its own prompt, sieve, judge and close) and this one,
    on one node, over a reply the trim would change and one it would not: the fetched bodies and the stored receipt and
    decision rows are the same bytes, but for the request's own id and hash and the job's random id. On, they differ."""
    before = ft.module_at(MAIN, "answer_release")
    then = ft.module_at(MAIN, "answer_generation")
    for name in ("build_prompt", "sieve_answer", "judge_sentences", "close_answer"):
        setattr(before, name, getattr(then, name))
    assert "ANSWER_LEAD_CONNECTIVE_TRIM" not in before.source_text + then.source_text

    def run(cls):
        seen = []
        for writer in (_dropped_lead, _kept_lead):
            body, receipt, decision = _asked(node, writer, cls)
            for key in ("request_id", "question_hash", "answer_id"):
                receipt[key] = "-"
            seen.append([body, receipt, decision])
        return json.dumps(seen, sort_keys=True)
    old, new = run(before.AnswerService), run(None)
    assert json.loads(new)[0][0]["answer"].startswith("Additionally,")
    assert new == old
    _on(monkeypatch)
    assert run(None) != old


# --- mutants: each condition is load-bearing --------------------------------------------------------------------------

def _mutated(module, old: str, new: str):
    """`module` with one line of its source replaced, loaded beside the real one in the same package."""
    source = inspect.getsource(module)
    assert source.count(old) == 1, old
    label = f"{module.__name__}_lct_mutant_{abs(hash(old))}"
    mutant = importlib.util.module_from_spec(importlib.util.spec_from_loader(label, loader=None))
    mutant.__package__ = "topos.permissions_v2"
    sys.modules[label] = mutant          # a dataclass in it looks its own module up by name while it is defined
    exec(compile(source.replace(old, new), f"<{label}>", "exec"), mutant.__dict__)
    return mutant


#: (the line, its mutant, the check it turns red).
MUTANTS = {
    # The switch check: off would trim.
    "switch": ("    if lead_connective_trim_on() and lead_dropped(sieved):", "    if lead_dropped(sieved):",
               check_untouched_with_the_switch_off),
    # The lead-dropped condition, both ways: a kept lead would be trimmed; a dropped one never would.
    "lead kept": ("    return scrub_sentences([sieved.lead])[1] > 0", "    return True",
                  check_untouched_when_the_lead_was_kept),
    "lead dropped": ("    if not sieved.sentences or sieved.sentences[0] != sieved.lead:", "    if False:",
                     check_trimmed_when_the_lead_was_dropped),
    # The closed list of connectives: the comma required; no other opener.
    "comma": ("        if body.startswith(connective) and body[len(connective):len(connective) + 1].isspace():",
              "        if body.startswith(connective[:-1]):", check_also_known_as_is_no_connective),
    "open connectives": ("    for connective in LEAD_CONNECTIVES:",
                         "    for connective in LEAD_CONNECTIVES + tuple(body.split(' ', 1)[:1]):",
                         check_an_opener_off_the_list_is_untouched),
    # The closed list of subjects before "also".
    "open subjects": ("    for subject in ALSO_SUBJECTS:",
                      "    for subject in ALSO_SUBJECTS + (body.split(' also ', 1)[0],):",
                      check_an_opener_off_the_list_is_untouched),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_mutant_turns_its_named_check_red(name, monkeypatch):
    old, new, check = MUTANTS[name]
    with monkeypatch.context() as patched:
        check(ag, patched)                                           # green on the real module
    mutant = _mutated(ag, old, new)
    with monkeypatch.context() as patched:
        with pytest.raises(AssertionError):
            check(mutant, patched)


@pytest.mark.parametrize("name", ["open connectives", "open subjects", "comma"])
def test_each_closed_list_mutant_turns_the_pure_never_trimmed_check_red(name):
    old, new, _check = MUTANTS[name]
    check_what_is_never_trimmed(trim_lead_connective)
    with pytest.raises(AssertionError):
        check_what_is_never_trimmed(_mutated(ag, old, new).trim_lead_connective)


def test_the_switch_check_mutant_trims_through_the_service_with_the_switch_off(node, monkeypatch):
    """Through the service: with the switch check gone, the answer is trimmed with the switch unset, so the off check
    goes red; the real service leaves the answer as written."""
    check_off_the_receipt_has_no_new_key(node)
    mutant = _mutated(ag, "    if lead_connective_trim_on() and lead_dropped(sieved):", "    if lead_dropped(sieved):")
    for name in ("build_prompt", "sieve_answer", "judge_sentences", "close_answer"):
        monkeypatch.setattr(answer_release, name, getattr(mutant, name))
    with pytest.raises(AssertionError):
        check_off_the_receipt_has_no_new_key(node)
