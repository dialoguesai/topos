"""BL-112 (1), the owner's ruling of 8 Oct 2026: at read time a NAME of an Off-limits entry is found as whole words.

Three read-time filters looked for each name and nickname of every entry as a bare substring: the filter on every
query answer for the owner's outside AI client and his routines (`retrieval._blackhole_policy_for_summary`, and the
cluster filter beside it), the gate on every model call (`blackhole_llm.evaluate`) and the guard's text scan
(`BlackholeGuard.text_mentions_blackholed`). A short name hid a great deal: on the reviewer's invented homes "Ed"
dropped 42% of the outside client's query results and marked 93% of six-row prompts. The owner ruled: for an entry he
made, and for a carried entry he has made fully Off-limits, whole words. One object decides it for all of them,
`blackhole.OffLimitsTerms`; the rule there:

  - a name is found where no letter or digit touches either end of it ("ed" in "Ed called", never in "edited");
  - from three letters, with an "s" glued on too: a plural, or a possessive with no apostrophe ("sams");
  - a possessive with an apostrophe was always taken off by the normalisation ("Sam's" reads "sam");
  - in the text and in the values both, so a name a serialisation glued to an escape ("\\nSam") is still found;
  - a name written in a script with no spaces is found anywhere, as before;
  - a name that is carried and WAITING (the whole entry, or a name the upgrade added to an entry he made) is found
    anywhere, as before: the ruling is about the entries he made and the ones he acted on, nothing else.

Every person here is invented.
"""
from __future__ import annotations

import pytest

from tests.topos.test_carried_entry_owner_paths import as_caller, the_node_knows_its_owner, the_owner_acts  # noqa: F401
from tests.topos.test_carried_entry_waits import ORDINARY, excluded
from tests.topos.test_carry_step_review_r1 import conn  # noqa: F401 (conn: fixture)
from tests.topos.test_off_limits_identifiers_at_read_time import SOMEONE, item
from topos.features.lifecycle.blackhole import EVERYONE, BlackholeStore, OffLimitsTerms, off_limits_terms, terms_of
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
from topos.features.lifecycle.blackhole_llm import evaluate
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.query.retrieval import _blackhole_policy_for_clusters, _blackhole_policy_for_summary

pytestmark = pytest.mark.public

#: Text that names nobody and holds a short name's letters inside other words.
INSIDE = ["Edited the homework and pushed the fixed branch.", "Samples of the same paint, and some balsam.",
          "We also finished the normal walk before the usual rain."]
#: Text that names the person, in each form the rule finds.
NAMING = {"ed": ["Ed called after lunch.", "Lunch with ed, then the walk."],
          "sam": ["Sam called after lunch.", "Sam's bike is outside.", "The sams came round.", "Met SAM at the gate."],
          "al": ["Lunch with Al went late."]}


def exit_filter(c, given):
    return as_caller(SOMEONE, _blackhole_policy_for_summary, given, conn=c, disclosure_tier="default_disclosure")


def cluster_filter(c, given):
    return as_caller(SOMEONE, _blackhole_policy_for_clusters, given, conn=c, disclosure_tier="default_disclosure")


def cluster(label):
    return {"cluster_id": "c-1", "label": label, "centroid_preview": label, "size": 3}


def the_three(c, text):
    """(dropped by the query exit, marked by the model gate, found by the guard's scan) for one text."""
    return (exit_filter(c, [item(text)]) == [], evaluate(c, {"prompt": text}, provider="openai").tainted,
            BlackholeGuard(c, caller_class=CallerClass.UNKNOWN).text_mentions_blackholed(text))


@pytest.mark.parametrize("name", ["Ed", "Sam", "Al"])
def test_an_entry_the_owner_made_is_found_as_whole_words_in_all_three_filters(conn, name):
    """Rule: `OffLimitsTerms._named`. Take it out (every name a bare substring again) and every text of INSIDE is
    dropped, marked and found for the name whose letters it holds."""
    BlackholeStore(conn).blackhole_entity(entity_ref=name, processing_tier="secure", note=None)
    conn.commit()
    for text in INSIDE:
        assert the_three(conn, text) == (False, False, False), text
    for text in NAMING[name.lower()]:
        assert the_three(conn, text) == (True, True, True), text
    assert cluster_filter(conn, [cluster(INSIDE[0]), cluster(INSIDE[1])]) == [cluster(INSIDE[0]), cluster(INSIDE[1])]
    assert cluster_filter(conn, [cluster(NAMING[name.lower()][0])]) == []


def test_a_name_is_never_found_in_a_key_and_is_found_after_an_escape(conn):
    """The query exit serialises the whole item, keys included: "al" stands in `retrieval_source` as letters only.
    And a name after a newline is serialised as "\\nSam": the values are read too, so it is found."""
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="Al", processing_tier="secure", note=None)
    store.blackhole_entity(entity_ref="Sam", processing_tier="secure", note=None)
    conn.commit()
    unrelated = item("A quiet afternoon.")
    assert exit_filter(conn, [unrelated]) == [unrelated]
    assert exit_filter(conn, [item("Walked home.\nSam was late.")]) == []
    assert exit_filter(conn, [item("A quiet afternoon.", note="Walked home.\nSam was late.")]) == []


def test_a_carried_entry_the_owner_made_fully_off_limits_reads_whole_words(conn):
    """A contact saved as "Sam" that he once excluded: carried and waiting, then he acts on it."""
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    conn.commit()
    # Waiting: a caller the node cannot take for its owner reads it as before, anywhere.
    assert exit_filter(conn, [item(INSIDE[1])]) == []
    the_owner_acts(conn)
    conn.commit()
    assert exit_filter(conn, [item(INSIDE[1])]) == [item(INSIDE[1])]
    assert exit_filter(conn, [item("Sam called after lunch.")]) == []
    assert the_three(conn, INSIDE[1]) == (False, False, False)
    assert the_three(conn, "Sam called after lunch.") == (True, True, True)


def test_a_name_carried_and_waiting_is_still_found_anywhere(conn):
    """The ruling's edge: what is carried and waiting is read as before by every reader that reads it."""
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    conn.commit()
    terms = off_limits_terms(conn, view=EVERYONE)
    assert "sam" in terms.loose
    assert terms.found("samples of the same paint") == "sam"
    entry = next(record for record in BlackholeStore(conn).list() if record["carried_waiting"])
    assert terms_of(entry).found("samples") == "sam"


def test_how_a_name_is_found():
    terms = OffLimitsTerms(names={"sam", "ed", "quorra vellaby", "王伟"}, loose=frozenset())
    assert terms.loose == {"王伟"}                                              # no spaces in that script: anywhere
    assert terms.found("sam") == "sam" and terms.found("sams") == "sams"
    assert terms.found("sam s bike") == "sam"                                   # "sam's", normalised
    assert terms.found("same samples balsam sammy") is None
    assert terms.found("ed") == "ed" and terms.found("eds") is None and terms.found("edited") is None
    assert terms.found("met quorra vellaby there") == "quorra vellaby"
    assert terms.found("met quorra  vellaby there") == "quorra  vellaby"       # its words apart by any whitespace
    assert terms.found("quorravellaby") is None and terms.found("quorra") is None
    assert terms.found("我今天和王伟一起吃饭") == "王伟"
    assert terms.found("a b", values="walked home nsam") is None and terms.found("x", values="hi sam") == "sam"
    # A caller that names no loose set reads every name anywhere, as before (nothing narrows by default).
    assert OffLimitsTerms(names={"sam"}).found("samples") == "sam"
    # A name that is both loose and whole is loose: the wider reading wins.
    assert OffLimitsTerms(names={"sam"}, loose={"sam"}).found("samples") == "sam"
