"""Lane CT176 (14 Oct 2026): the citation rule reads the forms a citation is written in, and nothing more.

The rule drops a sentence that carries no item number of its own, or one past the items. Measured on the lane's
battery (the 9B writer, 8 items), every dropped sentence was truly uncited: an abstention ("No item mentions ..."), a
whole answer written without citations, or a paragraph whose one citation list stood after its last sentence. None
was a citation the rule misread. The one change is hardening, for a writer that does use other forms:

- `ANSWER_CITATION_FORMS` (on): a range "[1-3]", a padded "[ 1 ]" and full-width brackets are read where they stand,
  as the form the rule reads. A range past the items is read as "[0]" and its sentence drops, even beside a valid
  citation. A bracket never spans a line break. Nothing is ever added to a sentence without a citation, and a
  paragraph-final list is never carried back over the sentences before it.

Every other check (copy, echo, subject, judge, time guard, Off-limits, scrub) runs after, unchanged. Invented text.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_answer_quality_aq158 import LAMP, SKETCHES, TOMATOES, Open
from topos.permissions_v2 import answer_checks as ac
from topos.permissions_v2.answer_generation import build_prompt, post_check_answer

P = "TOPOS_PERMISSIONS_V2_"
QUESTION = "What has the owner been doing?"
RECORDS = [TOMATOES, SKETCHES, LAMP]
# One sentence the checks keep when it cites [1] in the rule's own form (the control for every case below).
SENTENCE = "The owner re-potted tomatoes and fixed a bike chain"


@pytest.fixture(autouse=True)
def switches_unset(monkeypatch):
    for name in ("ANSWER_CITATION_FORMS", "ANSWER_SUPPORT_JUDGE"):
        monkeypatch.delenv(P + name, raising=False)


def _check(text, records=RECORDS, mode="with_sources"):
    prompt = build_prompt(QUESTION, records, precision="none")
    return post_check_answer(text, records, prompt, mode=mode, boundary=Open())


def test_the_control_sentence_is_kept_in_the_rules_own_form():
    checked = _check(SENTENCE + " [1].")
    assert (checked.reason, checked.kept, checked.dropped_citation) == ("answered", 1, 0)


@pytest.mark.parametrize("written, cited", [
    ("[1-2]", 2), ("[1–2]", 2), ("[1 - 3]", 3), ("[ 1 ]", 1), ("【1】", 1), ("［1］", 1), ("[1-2, 3]", 3), ("[1, 2-3]", 3),
], ids=["range", "en-dash-range", "spaced-range", "padded", "lenticular", "full-width", "range-then-number",
        "number-then-range"])
def test_another_bracket_form_is_read_as_that_sentences_citation(written, cited, monkeypatch):
    on = _check(f"{SENTENCE} {written}.")
    assert (on.reason, on.kept, on.dropped_citation, on.cited) == ("answered", 1, 0, cited)
    assert on.body.records == [record for record in RECORDS[:cited]]
    monkeypatch.setenv(P + "ANSWER_CITATION_FORMS", "false")
    off = _check(f"{SENTENCE} {written}.")
    assert (off.reason, off.kept, off.dropped_citation) == ("all_sentences_dropped", 0, 1)        # 1.5.9


def test_a_citation_after_the_period_stays_the_preceding_sentences():
    """The splitter already read "... chain. [1]" as the sentence before it; a range there is read the same way."""
    checked = _check(f"{SENTENCE}. [1-2] The owner sketched the harbour in the evening [2].")
    assert (checked.kept, checked.dropped_citation, checked.cited) == (2, 0, 2)
    assert checked.body.answer.split("\n")[0] == SENTENCE + ". [1, 2]"


@pytest.mark.parametrize("written", ["[1-4]", "[0-2]", "[3-1]", "[2-9]", "[1-100000]"])
def test_a_range_past_the_items_is_read_as_zero_and_drops(written):
    checked = _check(f"{SENTENCE} {written}.")
    assert (checked.reason, checked.kept, checked.dropped_citation) == ("all_sentences_dropped", 0, 1)
    assert ac.normalize_citations(f"x {written}.", 3) == "x [0]."


@pytest.mark.parametrize("written", ["[1-4]", "[ 4 ]", "[1-2, 4]"])
def test_a_bad_citation_beside_a_valid_one_still_drops_the_sentence(written):
    """Review of topos#115: "[3] [1-4]" kept the sentence on "[3]" and sent the literal "[1-4]" to the recipient."""
    checked = _check(f"The owner rewired a reading lamp [3] {written}. {SENTENCE} [1].")
    assert (checked.kept, checked.dropped_citation) == (1, 1)
    assert "lamp" not in checked.body.answer and "[1-4]" not in checked.body.answer


def test_a_bracket_never_spans_a_line_break():
    """Review of topos#115: "[1\\n]" joined two lines and gave the second a citation it never had."""
    text = "The owner re-potted tomatoes [1\n] and sketched the harbour."
    assert ac.normalize_citations(text, 3) == text
    checked = _check(text)
    assert checked.kept == 0


def test_a_sentence_without_a_citation_gets_none_and_a_paragraph_list_is_never_carried_back():
    """The measured failure (template v8): three sentences, one list after the last. Only the last stands."""
    text = (SENTENCE + ". The owner sketched the harbour in the evening. The owner rewired a reading lamp. [1-3]")
    checked = _check(text)
    assert (checked.generated, checked.kept, checked.dropped_citation) == (3, 1, 2)
    assert checked.body.answer.startswith("The owner rewired a reading lamp")
    uncited = _check(SENTENCE + ". The owner sketched the harbour in the evening.")
    assert (uncited.reason, uncited.kept, uncited.dropped_citation) == ("all_sentences_dropped", 0, 2)


def test_the_forms_the_rule_already_reads_are_left_byte_for_byte():
    for text in ("a [1].", "a [1, 2].", "a [1;2].", "a [1,2,3].", "a. [1] b [2].", "a [1][2].", "no citation."):
        assert ac.normalize_citations(text, 3) == text
    # Not citations in the rule's sense, and never read as one: parentheses, carets, words, superscripts.
    for text in ("a (1).", "a^1.", "a (source 1).", "a [Item 1].", "a.¹"):
        assert ac.normalize_citations(text, 3) == text
        assert _check(f"{SENTENCE} {text[2:]}").dropped_citation == 1


def test_every_later_check_still_runs_on_a_normalised_citation():
    """The copy check (answer-only mode) drops a sentence that repeats its item, whatever bracket form cited it."""
    copied = "Spent the weekend re-potting the balcony tomatoes and fixing the bike chain"
    for written in ("[1]", "[1-1]", "【1】"):
        checked = _check(f"{copied} {written}.", mode="only")
        assert (checked.kept, checked.dropped_citation, checked.dropped_copy) == (0, 0, 1), written
