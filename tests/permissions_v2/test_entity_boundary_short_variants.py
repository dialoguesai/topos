"""Lane P: a short Off-limits term also withholds its pet-name and inflected forms (entity boundary v4).

protects: an independent blind-set scorer found a protected person registered with a three-letter alias and named in
a journal entry only by a pet-name form of it, one token that starts with the alias; the canonical name appeared
nowhere. The boundary matched a term under four characters only as a whole token (so "M.E." does not match
"message"), so the entry, and the goal that cited it, released. These tests pin:
  - each ending class of `short_variants`, and the endings it leaves out on purpose;
  - ordinary words that merely start with a common short name still release ("also", "same", "edit", "join");
  - exact whole-token matches and long-term substring matches are unchanged, and the rule only ever adds a match
    (the v2 matcher, kept here verbatim, never matches where v4 does not);
  - invisible, combining, look-alike, apostrophe-letter and stretched characters inside a pet-name form;
  - the message, journal-entry and goal-text paths through `EntityBoundary.check` / `observe` /
    `mentions_protected`, the assessment floor beside the boundary, and one journal entry end to end through a
    signed grant: released without the pet-name forms, withheld with them, as `entity_protected`;
  - the boundary's version moved past candidate 10's v3, so every index built against v3 re-qualifies.
Fixtures are synthetic: common short given names with no surname, and invented full names.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from tests.permissions_v2.test_entity_boundary import protected_corpus  # noqa: F401 (a fixture)
from tests.permissions_v2.test_evidence import corpus, decision, edit  # noqa: F401 (corpus is a fixture)
from tests.permissions_v2.test_journal_family import (  # noqa: F401 (node is a fixture)
    SOURCE, _db, _entry, _floors_code, _labels, _prepare, node, owner)
from tests.permissions_v2.test_journal_goal_field import SORT, _grounded_by_field, _rule, field_on  # noqa: F401
from tests.permissions_v2.test_journal_typed_items import _code, _kind, _node
from topos.permissions_v2 import entity_boundary
from topos.permissions_v2 import permitted_derivation as pd
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary, normalized, short_variants, skeleton

CASES = Path(__file__).resolve().parent / "entailment_cases"
SCHEMA = """
CREATE TABLE entity_blackholes(blackhole_id TEXT, entity_id TEXT, normalized_name TEXT, canonical_name TEXT,
    aliases_json TEXT);
CREATE TABLE entities(entity_id TEXT, canonical_name TEXT, normalized_name TEXT, aliases_json TEXT,
    identifiers_json TEXT, contact_id TEXT);
CREATE TABLE entity_merge_tombstones(absorbed_entity_id TEXT, merged_into TEXT, canonical_name TEXT,
    aliases_json TEXT, identifiers_json TEXT);
CREATE TABLE contacts(contact_id TEXT, display_name TEXT);
CREATE TABLE contact_identifiers(contact_id TEXT, identifier TEXT, identifier_type TEXT);
CREATE TABLE entity_mentions(entity_id TEXT, record_id TEXT, source_id TEXT, canonical_table TEXT, surface_text TEXT);
"""


def boundary(*aliases, canonical="Quentin Abernathy"):
    """The real boundary over the tables it reads, one protected person with these aliases."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?)",
                 (canonical.lower(), canonical, json.dumps(list(aliases))))
    return EntityBoundary(conn)


def v2_hits(text, terms):
    """`EntityBoundary._hits` at node-observed-entity-boundary/v2, verbatim, for one text: the rule v4 may only widen."""
    long_terms = [term for term in terms if len(term) >= 4]
    short_terms = set(terms).difference(long_terms)
    plain = normalized(text)
    compact = "".join(ch for ch in plain if ch.isalnum())
    tokens = {skeleton(token) for token in re.split(r"[\s@:/<>]+", plain)}
    tokens.update(skeleton(token) for token in re.findall(r"[^\W_]+", plain))
    return bool(short_terms.intersection(tokens) or any(term in compact for term in long_terms))


def _protect(path, canonical, aliases):
    from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
    with _db(path) as conn:
        apply_entity_blackhole_v1_up(conn)
        conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                     "aliases_json, created_at) VALUES ('b-zeb','',?,?,?,'t')",
                     (canonical.lower(), canonical, json.dumps(aliases)))


# --- the forms ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("alias, text", [
    # after a consonant: y, ie, ey, i, s, sy, sie, bo, ji
    ("Kat", "Katy"), ("Kat", "Katie"), ("Kat", "Katey"), ("Kat", "Kati"), ("Sam", "Sams"), ("Pat", "Patsy"),
    ("Bet", "Betsie"), ("Jim", "Jimbo"), ("Ben", "Benji"), ("Alf", "Alfie"),
    # after a vowel and one consonant, the consonant doubles: y, ie, ey, i, o, a
    ("Sam", "Sammy"), ("Ed", "Eddie"), ("Ed", "Eddy"), ("Al", "Ally"), ("Tom", "Tommey"), ("Kim", "Kimmi"),
    ("Rob", "Robbo"), ("Gaz", "Gazza"), ("Em", "Emma"), ("Liz", "Lizzie"),
    # a c doubles as ck too
    ("Vic", "Vicky"), ("Bec", "Becca"), ("Nic", "Nickie"),
    # after the e of a three-letter term: y; for a vowel, a consonant and e, the e also drops before ie, i
    ("Abe", "Abey"), ("Joe", "Joey"), ("Zoe", "Zoey"), ("Abe", "Abie"), ("Eve", "Evie"),
    ("Ike", "Ikie"), ("Abe", "Abi"),
    # after any other vowel or y: ey, ie, sie, and a two-letter term doubles
    ("Jo", "Joey"), ("Lou", "Louie"), ("Mo", "Momo"), ("Jo", "Josie"), ("Ro", "Rosie"), ("Jo", "Jojo"), ("Lu", "Lulu"),
    # any form above with a plural or possessive s, with or without the apostrophe
    ("Sam", "Sammys"), ("Abe", "Abeys"), ("Jo", "Joeys"), ("Sam", "Sammy's"), ("Abe", "Abey\u2019s"),
])
def test_each_ending_class_is_a_pet_name_form(alias, text):
    assert skeleton(text) in short_variants(skeleton(alias))
    assert boundary(alias).mentions_protected(f"Lunch with {text} near the harbour.")
    assert not v2_hits(f"Lunch with {text} near the harbour.", {skeleton(alias)})   # each one was a miss before


@pytest.mark.parametrize("alias, word", [
    ("Al", "also"), ("Al", "always"), ("Al", "alpha"), ("Al", "album"), ("Al", "alert"), ("Al", "almost"),
    ("Sam", "same"), ("Sam", "sample"), ("Sam", "samples"), ("Sam", "sand"),
    ("Ed", "edit"), ("Ed", "edge"), ("Ed", "editor"), ("Ed", "education"), ("Ed", "edited"),
    ("Jo", "join"), ("Jo", "joy"), ("Jo", "job"), ("Jo", "joke"), ("Jo", "journal"), ("Jo", "jog"),
    ("M.E.", "message"), ("M.E.", "meet"), ("M.E.", "means"),
    # the endings left out: "-y" after a, i, o, u; undoubled -o, -a; -es; -e
    ("Bo", "boy"), ("Da", "day"), ("Di", "die"), ("Hal", "halo"), ("Sol", "solo"), ("Jud", "judo"),
    ("Tod", "todo"), ("Meg", "mega"), ("Bet", "beta"), ("Dat", "data"), ("Col", "cola"), ("Tim", "times"),
    ("Sal", "sales"), ("Ann", "annual"), ("Abe", "abbey"), ("Abe", "Abel"), ("Abe", "Aberdeen"),
    ("Ke", "key"), ("He", "hey"),                    # a two-letter alias ending in e takes no -y
    ("Tre", "try"), ("Dre", "dry"), ("Ane", "any"),  # no e-drop after two consonants, and never e-drop + y
    ("Tre", "tri-colour"),
    # a bare s only after a three-letter term's last consonant (Sams), never after a vowel or e
    ("Ha", "has"), ("Wa", "was"), ("Ye", "yes"), ("Hi", "his"), ("Day", "days"), ("Doe", "does"), ("Lou", "lous"),
    # a two-letter term takes the doubled forms only, and none without a vowel (initials, a title)
    ("An", "any"), ("It", "its"), ("Ed", "eds"), ("T.H.", "this"), ("T.H.", "they"), ("Dr", "dry"),
    ("Zeb", "zebra"), ("Ed", "Edinburgh"), ("Sam", "Samuel"),
])
def test_an_ordinary_word_that_starts_with_a_short_name_still_releases(alias, word):
    assert skeleton(word) not in short_variants(skeleton(alias))
    assert not boundary(alias).mentions_protected(f"The {word} is on the list.")


@pytest.mark.parametrize("term", ["j", "a", "th", "dr", "k2", "b12", "ab1", "\u043b\u0438", "\u674e\u660e", "abel",
                                  "sammy"])
def test_an_initial_a_term_with_a_digit_or_another_script_and_a_long_term_get_no_forms(term):
    """One letter would make nearly every two-letter word a form ("by", "so", "my"), and two letters with no vowel
    are initials or a title ("T.H." would make "this" and "they", "Dr" "dry"); a long term already matches anywhere."""
    assert short_variants(term) == frozenset()


def test_letters_english_does_not_double_are_not_doubled():
    assert {"maxxy", "maxxie", "rexxy", "rexxi"}.isdisjoint(short_variants("max") | short_variants("rex"))
    assert {"maxie", "maxi", "rexy"} <= short_variants("max") | short_variants("rex")
    assert "artty" not in short_variants("art") and "arty" in short_variants("art")   # no doubling after a cluster


# --- unchanged and only ever widened ----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Abe called.", "ABE called.", "Abe's bike.", "Abe\u2019s bike.", "Abe-Marie called.", "@abe sent it.",
    "/abe/ shared it.", "Quentin Abernathy called.", "quentin.abernathy@example.org wrote.",
    "QuentinAbernathy wrote.", "Quen\u200btin Abernathy called.", "Qu\u00e9ntin Abernathy called.",
])
def test_exact_and_long_matches_still_withhold(text):
    assert v2_hits(text, {"abe", "quentinabernathy"})
    assert boundary("Abe").mentions_protected(text)


def test_the_rule_only_ever_adds_a_match():
    """Over every message and claim in the synthetic entailment cases, with short and long terms: wherever v2
    matched, v4 matches. Non-vacuous: v2 matched somewhere, and v4 added matches v2 missed."""
    texts = []
    for path in sorted(CASES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.extend(v for v in json.loads(line).values() if isinstance(v, str) and " " in v)
    texts += ["Sammy and Abey met Jojo.", "Ab\u2060ey and Sam\u200bmy.", "Abe\u02bcs car.", "M.E. sent a message."]
    term_sets = [{"sam"}, {"abe"}, {"jo"}, {"al", "ed"}, {"me"}, {"bo", "di"}, {"riverton"}, {"maraexample", "me"},
                 {"sam", "quentinabernathy"}]
    assert len(texts) > 100
    old_hits = added = 0
    for text in texts:
        for terms in term_sets:
            short, long_ = entity_boundary.split_terms(terms)
            new = entity_boundary.text_hits(text, short, long_)
            old = v2_hits(text, terms)
            assert new or not old, (text, terms)
            old_hits += old
            added += new and not old
    assert old_hits > 0 and added > 0


@pytest.mark.parametrize("text", [
    "Lunch with Ab\u2060ey.", "Lunch with Ab\u200bey.", "Lunch with Ab\u00adey.", "Lunch with Ab\u200dey.",
    "Lunch with A\u0301bey.", "Lunch with \u00c1BEY.", "Lunch with \uff21\uff42\uff45\uff59.",
    "Lunch with \u0410b\u0435y.",                                      # Cyrillic look-alike letters
    "Lunch with Abeyyy.", "Lunch with Abbbey.", "Lunch with Abey\u02bcs dog.", "Lunch with Abe\u02bcs dog.",
    "Lunch with #abey.", "Lunch with (Abey).", "abey: lunch.", "Lunch with Abey_2.",
])
def test_a_pet_name_form_withholds_in_any_spelling(text):
    assert boundary("Abe").mentions_protected(text)


@pytest.mark.parametrize("alias, text", [("Tom", "Tommmo"), ("Gaz", "Gazzzza"), ("Sam", "Saaam"), ("Sam", "Sammyyy")])
def test_a_stretched_letter_reads_once_and_twice(alias, text):
    assert boundary(alias).mentions_protected(f"Lunch with {text} today.")


@pytest.mark.parametrize("alias, text, expected", [
    ("Abe", "Abeyy", True), ("Sam", "Sammyy", True), ("Ed", "Eddiee", True), ("Rob", "Robboo", True),
    ("Jo", "Joeyy", True),
    # only a form reads its doubled last letter once: the alias itself does not, or "boo" and "too" would withhold
    ("Bo", "boo", False), ("To", "too", False), ("Le", "lee", False), ("Mo", "moo", False), ("Tre", "tree", False),
])
def test_a_form_whose_last_letter_is_doubled_reads_as_the_form(alias, text, expected):
    assert boundary(alias).mentions_protected(f"Lunch with {text} today.") is expected


def test_the_boundary_version_moved_so_every_v3_index_requalifies(monkeypatch):
    """Candidate 10's journal name parts took v3 and ran on the owner's node, so this rule is v4."""
    assert entity_boundary.VERSION == "node-observed-entity-boundary/v4"
    current = boundary("Abe").revision
    monkeypatch.setattr(entity_boundary, "VERSION", "node-observed-entity-boundary/v3")
    assert boundary("Abe").revision != current


# --- the message, journal-entry and goal-text paths ---------------------------------------------------------

@pytest.mark.parametrize("content, verdict", [
    ("Zebby called about the boxes.", "withheld"), ("Ze\u2060bbie called about the boxes.", "withheld"),
    ("Zebs dog is back.", "withheld"), ("The zebra photos are back.", "qualified"),
])
def test_a_message_naming_a_protected_person_by_a_pet_name_is_withheld(protected_corpus, content, verdict):
    edit(protected_corpus, "UPDATE entities SET aliases_json='[\"Zeb\"]' WHERE entity_id='protected-entity'")
    edit(protected_corpus, "UPDATE conversation_messages SET content=?", (content,))
    assert decision(protected_corpus).verdict == verdict


def test_a_journal_entry_naming_a_protected_person_by_a_pet_name_is_withheld_in_any_column(node):
    _protect(node, "Zebulon Thrake", ["Zeb"])
    _entry(node, "e-body", "Walked home with Zebby after the draft.")
    _entry(node, "e-people", "Walked home after the draft.", people="Zebbie")
    _entry(node, "e-place", "Coffee after the run.", place_name="Zebsy's Cafe")
    _entry(node, "e-clear", "Walked past the zebra crossing after the draft.")
    for entry_id in ("e-body", "e-people", "e-place"):
        assert _floors_code(node, entry_id) == "entity_protected", entry_id
    assert _floors_code(node, "e-clear") is None
    with _db(node) as conn:
        row = dict(conn.execute("SELECT * FROM journal_entries WHERE entry_id='e-body'").fetchone())
        matched, _revision = EntityBoundary(conn).observe(table="journal_entries", record_id="e-body",
                                                          source_id=SOURCE, dataset_id=None, row=row)
        assert matched is True
        with pytest.raises(PolicyError, match="entity_protected"):
            EntityBoundary(conn).check(table="journal_entries", record_id="e-body", source_id=SOURCE,
                                       dataset_id=None, row=row)


def test_goal_text_naming_a_protected_person_by_a_pet_name_withholds_in_both_rules(node):
    _protect(node, "Zebulon Thrake", ["Zeb"])
    goal = "Sort the spare cables with Zebby"
    with _db(node) as conn:
        assert _rule(goal, boundary=EntityBoundary(conn)) == "goal_field_offlimits"
        assert _rule("Sort the spare zebra cables", boundary=EntityBoundary(conn)) != "goal_field_offlimits"
        assert pd.refusal(pd.Spec("goal", "goal", goal), EntityBoundary(conn)) == "entity_protected"
        assert pd.refusal(pd.Spec("goal", "goal", "Sort the spare cables in the attic"), EntityBoundary(conn)) is None


def test_the_assessment_floor_matches_the_way_the_boundary_does(node):
    """OD-58 turns a journal entry's `unknown` into `none` because the boundary decides; the floor's own match must
    not be weaker than the boundary's, or an entry naming a pet-name form would be labelled `none`."""
    from topos.permissions_v2.automatic_message_review import apply_family_floors
    _entry(node, "e1")
    _resolver_, _reviews, prepared = _prepare(node, "e1")
    inputs = {**prepared["input"], "target": "Walked home with Zebby.", "protected_terms": ["zeb"]}
    unknown = _labels(prepared, protected_content="unknown")
    assert apply_family_floors("journal_entries", unknown, inputs).protected_content == "present"
    assert apply_family_floors("conversation_messages", unknown, inputs).protected_content == "present"
    clear = {**inputs, "target": "Walked past the zebra crossing."}
    assert apply_family_floors("journal_entries", unknown, clear).protected_content == "none"
    neighbour = {**inputs, "target": "She sent the slides.", "before": ["Zebby called earlier."]}
    assert apply_family_floors("conversation_messages", _labels(prepared), neighbour).protected_content == "unknown"


def test_the_entailment_eval_boundary_matches_the_way_the_engine_does(monkeypatch):
    import importlib
    import sys
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"))
    sys.modules.pop("entailment_eval", None)
    terms = importlib.import_module("entailment_eval").TermBoundary(["Abe", "Mara Example"])
    assert terms.mentions_protected("Lunch with Abey.") and terms.mentions_protected("maraexample.org")
    assert not terms.mentions_protected("The abbey and the alphabet.")


def test_a_journal_entry_naming_a_protected_person_by_a_pet_name_withholds_end_to_end(node, tmp_path, monkeypatch,
                                                                                      field_on):
    """The finding, end to end: the person is Off-limits with a three-letter alias, and a time-log entry names them
    only by a pet-name form in its body. Without the pet-name forms the entry was assessed, indexed and released, and
    so was the goal that cites it. With them (v4), the same assessment and the same index, both withhold at the read,
    for the Off-limits reason, and a rebuilt index holds neither."""
    _protect(node, "Zebulon Thrake", ["Zeb"])
    forms = entity_boundary._variants
    monkeypatch.setattr(entity_boundary, "_variants", lambda short_terms: frozenset())        # no pet-name forms
    goal_id = _grounded_by_field(node, goal=SORT, accomplished="Labelled the spare cables with Zebby.")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 2                                     # the entry, and the goal it grounds
    output, refused = search.search_request("spare cables attic", k=10)
    assert refused is None and any("Zebby" in record["content"] for record in _kind(output["records"], "journal_entry"))
    assert _code(search, "user_goals", goal_id) is None                   # the leak, reproduced

    monkeypatch.setattr(entity_boundary, "_variants", forms)
    assert _code(search, "user_goals", goal_id) == "entity_protected"
    output, refused = search.search_request("spare cables attic", k=10)
    assert refused is not None or output["records"] == []
    with owner():
        assert search.index.rebuild("grant-search", now=search.now[0])["member_count"] == 0
