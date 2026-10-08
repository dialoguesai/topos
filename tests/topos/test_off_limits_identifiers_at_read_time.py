"""Third fix round, ruling M on the read-time paths: an identifier matches only as itself; a name as it always did.

The owner decided on 7 Oct that handles and ids match only as themselves, and the last round applied it in the
clean-up only. The read-time scans (the query pipeline's exit filter, the model gate, the guard's text scan, the
aggregate's person labels, the cluster labeler) looked for EVERY alias of every entry as a bare substring of the
text, and the exit filter did so over the whole serialised item, keys included. The upgrade step writes a contact's
usernames, handles and id among the aliases, so the username "al" was found in the key `retrieval_source` of every
item there is.

The rule is one object, `blackhole.OffLimitsTerms`, that every one of those scans now asks:
  - an identifier with no digit and no "@" is found only where it stands as a whole token;
  - an identifier with a digit or an "@" is found anywhere, as before (a number, an address);
  - no identifier is looked for in the KEYS of a structured value, only in its values;
  - a NAME was found anywhere, inside a longer word, keys included (ruling P.4, pinned here as it was). The owner
    ruled on 8 Oct (BL-112) that a name of an entry he made, or of a carried one he made fully Off-limits, is found
    as whole words; the last test here pins that, and `test_off_limits_names_whole_words.py` holds the rest.
These entries are FULL ones (the owner acted, or made them), read by a caller the node cannot take for its owner.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import pytest

from tests.topos.test_carried_entry_owner_paths import as_caller, the_node_knows_its_owner  # noqa: F401
from tests.topos.test_carry_step_review_r1 import PHONE, cid, conn, contact, entity  # noqa: F401 (conn: fixture)
from topos.features.lifecycle.blackhole import BlackholeStore, OffLimitsTerms, start_waiting_clean_up, terms_of
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
from topos.features.lifecycle.blackhole_llm import evaluate
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.principal import RELAY_PRINCIPAL, THIRD_PARTY, Principal
from topos.query.retrieval import _blackhole_policy_for_clusters, _blackhole_policy_for_summary

pytestmark = pytest.mark.public

EXOTIC = "Quorra Vellaby"
ADDRESS = "al.b@fernmail.example"
SOMEONE = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="app", acting_user="someone-else")


def a_full_entry(c, *, usernames=("al", "work"), handles=((PHONE, "phone"), (ADDRESS, "email"))):
    """A contact with these identifiers, carried and then made fully Off-limits by the owner."""
    contact(c, cid("0a"), EXOTIC, usernames=list(usernames), handles=list(handles))
    carry_contact_excludes(c)
    store = BlackholeStore(c)
    (entry,) = store.list()
    start_waiting_clean_up(store, entry["blackhole_id"], processing_tier="secure", note=None)
    c.commit()
    return store.get(entry["blackhole_id"])


def item(text, **more):
    return {"topic": text[:120], "summary_text": text, "record_id": "m-1", "source_id": "src", "relevance_score": 0.5,
            "retrieval_source": "canonical:conversation_messages", **more}


INSIDE_OTHER_WORDS = ["We also finished the normal walk before the usual rain.", "The network was down; homework later.",
                      "A formal proposal and the paperwork."]
AS_THEMSELVES = ["Lunch with Al went late.", "Left a message for work about Friday.", "@al said yes",
                 "Call +1 555 0142 0137 after six.", "Write to al.b@fernmail.example today.",
                 "see mailto:al.b@fernmail.example?subject=hi"]


def test_the_entry_knows_which_of_its_terms_are_names_and_which_identifiers(conn):
    terms = terms_of(a_full_entry(conn))
    assert terms.names == {"quorra vellaby"}
    assert {"al", "work", ADDRESS, "+1 555 0142 0137"} <= terms.identifiers and terms.names.isdisjoint(terms.identifiers)


@pytest.mark.parametrize("caller", [SOMEONE, RELAY_PRINCIPAL, None], ids=["a_recipient", "no_stamp", "no_principal"])
def test_the_query_exit_finds_an_identifier_only_as_itself_and_never_in_a_key(conn, caller):
    """Rule: the exit filter asks `OffLimitsTerms.found_in(blob, values=...)`. Scan the serialised item for every
    alias again and all nine items go, the three unrelated ones through the key `retrieval_source` alone."""
    a_full_entry(conn)
    given = [item(text) for text in (*INSIDE_OTHER_WORDS, *AS_THEMSELVES)]
    kept = as_caller(caller, _blackhole_policy_for_summary, given, conn=conn, disclosure_tier="default_disclosure")
    assert kept == given[:len(INSIDE_OTHER_WORDS)]
    # a key that IS the handle is not a mention of it; the same word as a value is
    keyed = [item("Notes for the week.", work={"al": 3}), item("Notes for the week.", area="work")]
    assert as_caller(caller, _blackhole_policy_for_summary, keyed, conn=conn, disclosure_tier="default_disclosure") == keyed[:1]
    clusters = [{"label": text, "centroid_preview": ""} for text in (*INSIDE_OTHER_WORDS, *AS_THEMSELVES)]
    assert as_caller(caller, _blackhole_policy_for_clusters, clusters, conn=conn,
                     disclosure_tier="default_disclosure") == clusters[:len(INSIDE_OTHER_WORDS)]


def test_the_model_gate_and_the_guards_scan_follow_the_same_rule(conn):
    a_full_entry(conn)
    guard = BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN)
    for text in INSIDE_OTHER_WORDS:
        assert not evaluate(conn, {"prompt": text}, provider="openai").tainted, text
        assert not guard.text_mentions_blackholed(text), text
    for text in AS_THEMSELVES:
        verdict = evaluate(conn, {"prompt": text, "context": [{"work": "x"}]}, provider="openai")
        assert verdict.tainted and verdict.provider != "openai", text
        assert guard.text_mentions_blackholed(text), text
    # six rows of unrelated text in one prompt: the re-check's shape (it marked 81% of these for "al")
    assert not evaluate(conn, {"prompt": "\n".join(INSIDE_OTHER_WORDS * 2)}, provider="openai").tainted
    assert guard.blocks_name("al") and guard.blocks_name("Work") and not guard.blocks_name("also")


def test_how_each_kind_of_term_is_found():
    terms = OffLimitsTerms(names={"sam", "quorra vellaby"}, identifiers={"al", "work", "j.smith", "quorra7", ADDRESS, "sam"})
    assert terms.identifiers == {"al", "work", "j.smith", "quorra7", ADDRESS}       # a name is a name: "sam" is not here
    assert terms.found("samples of the same paint") == "sam"                        # a name: anywhere, as before
    assert terms.found("we also walked") is None and terms.found("al came by") == "al"
    assert terms.found("the network") is None and terms.found("back to work") == "work"
    assert terms.found("ask j.smith") == "j.smith" and terms.found("ask jxsmith") is None
    assert terms.found("xquorra7x") == "quorra7"                                    # a digit: anywhere, as before
    assert terms.found("mailto:" + ADDRESS) == ADDRESS
    assert terms.found('{"work": 1}', values="1") is None                           # keys left out by the caller
    assert terms.found('{"area": "work"}', values="work") == "work"
    assert terms.found('{"sam": 1}', values="1") == "sam"                           # a NAME in a key: found, as before
    assert not OffLimitsTerms() and OffLimitsTerms().found("anything") is None


def test_an_identifier_in_a_script_written_without_spaces_is_found_inside_a_sentence(conn):
    """The fourth round, a hole of the third round's own. "Only as itself" was built as "where no letter touches
    either end", and in Han, kana, Thai, Lao and Khmer a letter always does: a handle or a username in such a script
    stopped being found in any sentence that held it (until the third round every alias was a plain substring, so
    it was found). After the owner made such a contact fully Off-limits, text naming them by that username was not
    marked for the model gate and could go to a hosted model. Rule: `OffLimitsTerms` finds an identifier written
    wholly in such a script anywhere, from two characters, as the clean-up and the share boundary do."""
    terms = OffLimitsTerms(names={"quorra vellaby"}, identifiers={"田中太郎", "小明", "たなか", "王", "work"})
    for text in ("明日は田中太郎さんと会議です。", "下周要给小明打电话。", "きのうたなかさんにあいました。"):
        assert terms.found(text) is not None, text
    assert terms.found("那位国王的决定改变了历史。") is None                   # one character: a whole token only
    assert terms.found("和 王 一起") == "王"
    assert terms.found("the network") is None and terms.found("back to work") == "work"    # every other one: as before
    assert terms.found('{"小明": 1}', values="1") is None                    # and still never in a key
    # through the model gate and the guard, for a contact the owner made fully Off-limits
    a_full_entry(conn, usernames=("田中太郎", "小明"))
    guard = BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN)
    for text in ("明日は田中太郎さんと会議です。", "下周要给小明打电话。"):
        verdict = evaluate(conn, {"prompt": text}, provider="openai")
        assert verdict.tainted and verdict.provider != "openai", text
        assert guard.text_mentions_blackholed(text), text
    assert not evaluate(conn, {"prompt": "今日は朝から雨が降っています。"}, provider="openai").tainted


def test_a_name_the_owner_made_is_found_as_whole_words_never_inside_another_word_or_a_key(conn):
    """BL-112, the owner's ruling of 8 Oct 2026 (ruling P.4 pinned the opposite until then). An entry the owner made
    by hand whose NAME is two letters: until the ruling every item was dropped for a caller who is not the owner's
    app, through the key `retrieval_source`, and unrelated text was marked for the model gate. Now the unrelated
    items stay and the text is not marked; the name standing as a word still goes, in all three filters."""
    BlackholeStore(conn).blackhole_entity(entity_ref="Al")
    given = [item(text) for text in INSIDE_OTHER_WORDS]
    assert as_caller(SOMEONE, _blackhole_policy_for_summary, given, conn=conn, disclosure_tier="default_disclosure") == given
    assert not evaluate(conn, {"prompt": INSIDE_OTHER_WORDS[0]}, provider="openai").tainted
    assert not BlackholeGuard(conn).text_mentions_blackholed("the usual rain")
    named = [item("Lunch with Al went late.")]
    assert as_caller(SOMEONE, _blackhole_policy_for_summary, named, conn=conn, disclosure_tier="default_disclosure") == []
    assert evaluate(conn, {"prompt": "Lunch with Al went late."}, provider="openai").tainted
    assert BlackholeGuard(conn).text_mentions_blackholed("Lunch with Al went late.")
