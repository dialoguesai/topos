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
import time
from pathlib import Path

import pytest

from tests.permissions_v2.test_entity_boundary import protected_corpus  # noqa: F401 (a fixture)
from tests.permissions_v2.test_evidence import corpus, decision, edit  # noqa: F401 (corpus is a fixture)
from tests.permissions_v2.test_journal_family import (  # noqa: F401 (node is a fixture)
    SOURCE, _db, _entry, _floors_code, _labels, _prepare, node, owner)
from tests.permissions_v2.test_journal_goal_field import SORT, _grounded_by_field, _rule, field_on  # noqa: F401
from tests.permissions_v2.test_journal_typed_items import _code, _kind, _node
from topos.permissions_v2 import english_short_words, entity_boundary, journal_goal_field
from topos.permissions_v2 import permitted_derivation as pd
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary, normalized, short_variants, skeleton
from tests.permissions_v2.test_entity_boundary_v8 import without_v8

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


@pytest.mark.parametrize("term", ["j", "a", "th", "dr", "k2", "b12", "ab1", "\u043b\u0438", "\u043b\u0438\u0434",
                                  "\u00f8le", "\u674e\u660e", "abel", "sammy"])
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


def test_the_boundary_version_moved_so_every_earlier_index_requalifies(monkeypatch):
    """Candidate 10's journal name parts took v3 and ran on the owner's node; v4 added the short forms, v5 the
    inflected ones and v6 the named forms and tag characters, so an index built against any earlier version
    re-qualifies (v7 added readings and endings, v8 the forms in every kind: N6)."""
    assert entity_boundary.VERSION == "node-observed-entity-boundary/v8"
    current = boundary("Abe").revision
    for earlier in ("node-observed-entity-boundary/v3", "node-observed-entity-boundary/v4",
                    "node-observed-entity-boundary/v5", "node-observed-entity-boundary/v6",
                    "node-observed-entity-boundary/v7"):
        monkeypatch.setattr(entity_boundary, "VERSION", earlier)
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
    forms, word_forms = entity_boundary._variants, entity_boundary._word_variants
    monkeypatch.setattr(entity_boundary, "_variants", lambda short_terms: frozenset())        # no pet-name forms,
    monkeypatch.setattr(entity_boundary, "_word_variants", lambda words: frozenset())         # for aliases or name words
    goal_id = _grounded_by_field(node, goal=SORT, accomplished="Labelled the spare cables with Zebby.")
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 2                                     # the entry, and the goal it grounds
    output, refused = search.search_request("spare cables attic", k=10)
    assert refused is None and any("Zebby" in record["content"] for record in _kind(output["records"], "journal_entry"))
    assert _code(search, "user_goals", goal_id) is None                   # the leak, reproduced

    monkeypatch.setattr(entity_boundary, "_variants", forms)
    monkeypatch.setattr(entity_boundary, "_word_variants", word_forms)
    assert _code(search, "user_goals", goal_id) == "entity_protected"
    output, refused = search.search_request("spare cables attic", k=10)
    assert refused is not None or output["records"] == []
    with owner():
        assert search.index.rebuild("grant-search", now=search.now[0])["member_count"] == 0


# --- journal name words (NAME_PART_TABLES): a two- or three-letter name word withholds through its forms --------
# WS0, 1 Oct: candidate 10 matches each part of a protected name (three letters or more) bare in a journal row; its
# two- and three-letter words also take the forms above. A two-letter word is never matched bare (a particle) and
# never repeated ("Ma" is not "mama"); one with no vowel (a title) takes no forms.

def v3_hits(text, terms, parts):
    """Candidate 10's `EntityBoundary._hits` (node-observed-entity-boundary/v3) transcribed for one text, with its
    journal name parts: the rule v4 may only widen."""
    long_terms = [term for term in terms if len(term) >= 4]
    short_terms = set(terms).difference(long_terms)
    plain = normalized(text)
    compact = "".join(ch for ch in plain if ch.isalnum())
    tokens = {skeleton(token) for token in re.split(r"[\s@:/<>]+", plain)}
    tokens.update(skeleton(token) for token in re.findall(r"[^\W_]+", plain))
    if short_terms.intersection(tokens) or any(term in compact for term in long_terms):
        return True
    return bool(set(parts).intersection(tokens))


@pytest.mark.parametrize("person, text", [
    ("Zeb Thrake", "Zebby"), ("Zeb Thrake", "Zebs"), ("Zeb Thrake", "Zebbie's"), ("Zeb Thrake", "Ze\u2060bby"),
    ("Abe Varnell", "Abey"), ("Abe Varnell", "Abie"), ("Kat Varnell", "Katie"), ("Vic Varnell", "Vicky"),
    ("Jo Varnell", "Joey"), ("Jo Varnell", "Josie"), ("Ed Varnell", "Eddie"), ("Al Varnell", "Ally"),
    ("Em Varnell", "Emma"), ("Jo Varnell", "JOEYY"),
])
def test_a_journal_entry_naming_a_short_first_name_by_its_forms_is_withheld(node, person, text):
    _protect(node, person, [])                                       # the full name only, no alias
    _entry(node, "e1", f"Walked home with {text} after the draft.")
    assert _floors_code(node, "e1") == "entity_protected"


@pytest.mark.parametrize("first, word", [
    ("Al", "also"), ("Al", "always"), ("Sam", "same"), ("Sam", "sample"), ("Ed", "edit"), ("Ed", "edge"),
    ("Jo", "join"), ("Jo", "joy"), ("Jo", "job"), ("Bo", "boy"), ("Di", "die"), ("Hal", "halo"), ("Sol", "solo"),
    ("Meg", "mega"), ("Bet", "beta"), ("Tim", "times"), ("Sal", "sales"), ("Ann", "annual"), ("Abe", "abbey"),
    ("Zeb", "zebra"), ("Ha", "has"), ("Wa", "was"), ("Ye", "yes"), ("Hi", "his"), ("Day", "days"), ("Doe", "does"),
    ("An", "any"), ("Ane", "any"), ("Tre", "try"),
    # a two-letter name word is never bare and never repeated
    ("Ma", "mama"), ("Ha", "haha"), ("Jo", "Jo"),
])
def test_ordinary_words_still_release_beside_a_short_first_name(node, first, word):
    _protect(node, f"{first} Varnell", [])
    _entry(node, "e1", f"The {word} is on the list.")
    assert _floors_code(node, "e1") is None


def test_a_particle_or_a_title_in_a_name_takes_no_bare_match_and_a_title_no_forms(node):
    assert boundary(canonical="Dr Wren de la Cruzado").name_short_words == {"de", "la"}   # no "dr"
    _protect(node, "Dr Wren de la Cruzado", [])
    _entry(node, "e-particles", "Walked to la plage de Nice.")
    _entry(node, "e-title", "The dry run went well.")
    _entry(node, "e-part", "Wren called about the draft.")
    assert _floors_code(node, "e-particles") is None
    assert _floors_code(node, "e-title") is None
    assert _floors_code(node, "e-part") == "entity_protected"


def test_name_word_forms_reach_every_kind_since_v8():
    """v3-v7 read a name word's forms in journal rows only; since v8 (N6) every kind reads them."""
    gate = boundary(canonical="Zeb Thrake")
    assert gate.name_parts == {"zeb", "thrake"} and gate.name_short_words == {"zeb"}
    journal = {"entry_id": "j1", "source_id": "s", "content": "Zebby wrote back."}
    matched, _revision = gate.observe(table="journal_entries", record_id="j1", source_id="s", dataset_id=None,
                                      row=journal)
    assert matched and gate.name_part_match_only("journal_entries", journal)
    assert not gate._hits(journal)                       # whole terms alone release it: the name rule withholds it
    assert gate.mentions_protected("Zebby wrote back.")
    assert gate.name_part_match_only("conversation_messages", journal)
    assert gate.legacy_veto("journal_entries", journal) and gate.legacy_veto("signal_objects", journal)


def test_name_words_come_from_every_name_the_closure_reaches():
    """The protected entity's own record and its linked contact carry names the flag row does not: their short
    words take forms too, exactly where candidate 10 collects their parts."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','e-1','quentin abernathy','Quentin Abernathy','[]')")
    conn.execute("INSERT INTO entities VALUES('e-1','Jo Thrake','jo thrake','[]',NULL,'c-1')")
    conn.execute("INSERT INTO contacts VALUES('c-1','Ed Marsh')")
    gate = EntityBoundary(conn)
    assert {"jo", "ed"} <= gate.name_short_words
    for text in ("Joey called.", "Eddie called."):
        row = {"entry_id": "j1", "source_id": "s", "content": text}
        assert gate.observe(table="journal_entries", record_id="j1", source_id="s", dataset_id=None, row=row)[0], text


def test_a_name_word_change_moves_the_boundary_revision():
    """Two spellings with one skeleton and the same parts can differ in their two-letter words, and so in their
    forms: the revision binds the words, so a journal index re-qualifies."""
    spaced = boundary("J O Al Varnell", canonical="Quentin Abernathy")
    other = boundary("J Oa L Varnell", canonical="Quentin Abernathy")
    assert spaced.terms == other.terms and spaced.name_parts == other.name_parts
    assert spaced.name_short_words != other.name_short_words
    assert spaced.revision != other.revision


def test_name_word_forms_only_widen_candidate_10s_journal_match():
    """Over every message and claim in the synthetic entailment cases, as journal text: wherever candidate 10's v3
    matched (whole terms and bare parts), v4 matches. Non-vacuous on both counts."""
    texts = []
    for path in sorted(CASES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.extend(v for v in json.loads(line).values() if isinstance(v, str) and " " in v)
    texts += ["Zebby and Joey met Abie.", "Ze\u2060bby wrote.", "Thrake wrote."]
    old_hits = added = 0
    for canonical in ("Zeb Thrake", "Jo Riverton", "Mara Example", "Abe de la Cruzado"):
        gate = boundary(canonical=canonical)
        short, long_ = entity_boundary.split_terms(gate.terms)
        for text in texts:
            new = entity_boundary.text_hits(text, short, long_, parts=gate.name_parts, part_words=gate.name_short_words)
            old = v3_hits(text, gate.terms, gate.name_parts)
            assert new or not old, (text, canonical)
            old_hits += old
            added += new and not old
    assert old_hits > 0 and added > 0


# --- v5: inflected forms, written as a proper noun ------------------------------------------------------------
# An independent blind set (1 Oct) released a journal entry naming a protected person only by a Polish case form of a
# three-letter alias: its last vowel replaced by a genitive ending after a preposition. In a second case the boundary
# matched nothing either: a diminutive built on the whole alias, in the genitive. Neither is an English pet-name
# ending. These forms also make ordinary words, so they withhold only where written as a proper noun in running text.

@pytest.mark.parametrize("alias, text", [
    # a three-letter name ending in a vowel declines on its stem (the first failing shape: "u" + genitive)
    ("Ula", "Kolacja u Uli w piatek."), ("Iza", "Prezent u Izy."), ("Iza", "Rozmowa o Izie."),
    ("Ewa", "Widzialem wczoraj Ewe."), ("Ira", "Poshla s Iroy."), ("Ula", "Pisze do Ulu wieczorem."),
    # a palatalised stem
    ("Ada", "Rozmowa o Adzie."), ("Ota", "Myslimy o Ocie."),
    # a name ending in a consonant takes case endings, and a palatalised locative
    ("Zan", "Obiad u Zana."), ("Zan", "Dalem to Zanowi."), ("Zan", "Spacer z Zanem."), ("Ved", "Rozmowa o Vedzie."),
    # diminutives, also in a case form (the second failing shape: a diminutive of the whole alias, in the genitive)
    ("Reo", "Urodziny u Reosia."), ("Reo", "Spacer z Reosiem."), ("Zan", "Kino z Zankiem."), ("Zan", "Obiad u Zanka."),
    ("Ula", "Kawa z Ulka rano."), ("Ula", "Spacer z Ulunia."), ("Ula", "Kolacja u Ulenki."), ("Ira", "Zvonila Irochka."),
    ("Ula", "Pozvonila Ulya."), ("Ana", "Lunch with Anita today."), ("Ben", "Spacer z Beniem."),
    # a three-letter name ending in o declines like a masculine noun, one ending in e or i like an adjective, one
    # ending in a after another vowel on its stem, and one ending in y after a vowel like a consonant
    ("Ivo", "Obed u Iva."), ("Ivo", "Dal jsem to Ivovi."), ("Ivo", "Spacer z Ivem."), ("Joe", "Kolacja u Joego."),
    ("Joe", "Spacer z Joem."), ("Ali", "Prezent dla Alego."), ("Ali", "Dalem to Aliemu."), ("Mia", "Prezent dla Mii."),
    ("Mia", "Pozvonil Miyu."), ("Ray", "Kolacja u Raya."), ("Ray", "Spacer z Rayem."),
    # Russian and Ukrainian accusative and instrumental, Czech and Slovak diminutives in their case forms
    ("Ola", "Bachyv Olyu vchora."), ("Ola", "Pishla z Oloyu v kino."), ("Ula", "Kafe s Ulinkou."),
    ("Ula", "Dopis od Ulicky."), ("Ula", "Pozdrav pro Ulunku."), ("Zan", "Hrali jsme si se Zanikem."),
    ("Zan", "Dopis od Zanicka."), ("Zan", "Zvonil nam Zanushka."), ("Zan", "Obed so Zankom."),
    ("Zan", "Spacer so Zankovi."),
    # a possessive or a plural written without its apostrophe
    ("Ira", "We borrowed Iras car."), ("Bo", "Lunch at Bos place."), ("Reo", "Found Reos keys."),
])
def test_an_inflected_form_written_as_a_proper_noun_withholds(alias, text, monkeypatch):
    assert boundary(alias).mentions_protected(text)
    without_v8(monkeypatch)
    monkeypatch.setattr(entity_boundary, "_inflections", lambda short_terms: frozenset())
    monkeypatch.setattr(entity_boundary, "_named", lambda short_terms: frozenset())
    assert not boundary(alias).mentions_protected(text)               # each was a miss under v4


@pytest.mark.parametrize("alias, text", [
    # opening a sentence, any word is capitalised
    ("Ana", "Any ideas for dinner?"), ("Doe", "Does it rain there?"), ("Wa", "Was it fun?"), ("Ha", "Has it arrived?"),
    ("Day", "Days later we left."), ("Dan", "Dana from the bakery called."), ("Ula", "Uli came by."),
    ("Ana", "Notes:\n- Any time works."), ("Ana", "Done. Any time works."),
    # so a name opening one is not this rule's either (a residual), however it is inflected
    ("Ula", "Ulka przyszla wieczorem."),
    # in lower case, an inflected form is not this rule's: ordinary words would be ("any", "does", "was", "has")
    ("Ana", "Do any of them fit?"), ("Doe", "It does not."), ("Wa", "It was fine."), ("Ha", "She has two."),
    ("Day", "Two days off."), ("Ula", "kolacja u uli."),
])
def test_a_form_opening_a_sentence_or_in_lower_case_is_not_this_rules(alias, text):
    assert not boundary(alias).mentions_protected(text)


@pytest.mark.parametrize("text", [
    "Kolacja u U\u2060li w piatek.", "Kolacja u U\u200bli w piatek.", "Kolacja u U\u0301li w piatek.",
    "Kolacja u \uff35\uff4c\uff49 w piatek.", "Kolacja (u Uli) w piatek.", "Kolacja u \u201cUli\u201d w piatek.",
    "KOLACJA U ULI W PIATEK.", "Kolacja u Uli\u02bcs w piatek.",
])
def test_an_inflected_proper_noun_withholds_in_any_spelling(text):
    assert boundary("Ula").mentions_protected(text)


@pytest.mark.parametrize("alias, text", [
    ("Pia", "Notes on PII handling."), ("Ida", "The IDE crashed again."),
    # a name written in capitals inside lower-case prose reads as an acronym too (a residual)
    ("Ula", "kolacja u ULI w piatek."), ("Ula", "Kolacja u ULKI."),
])
def test_a_word_in_capitals_inside_prose_reads_as_an_acronym(alias, text):
    assert not boundary(alias).mentions_protected(text)


def test_a_long_text_is_read_in_one_pass():
    """Each word reads at most _OPENING_WINDOW characters before it, so a long text with many capitalised words costs
    one pass (reading the whole text before every word took 22 seconds for 100,000 characters); beyond the window a
    word reads as not opening, so the form withholds."""
    text = "Word " * 20000 + "and then Uli was here."
    started = time.monotonic()
    tokens = entity_boundary.proper_tokens(text)
    assert time.monotonic() - started < 5
    assert {"uli", "word"} <= tokens
    assert "uli" in entity_boundary.proper_tokens("Notes.\n" + " " * 100 + "Uli was here.")
    assert "uli" not in entity_boundary.proper_tokens("Notes.\n" + " " * 10 + "Uli was here.")


def test_endings_that_would_make_other_names_are_left_out():
    assert "lee" not in entity_boundary.inflected_forms("lea")                       # no -e on a vowel pair
    assert "kenya" not in entity_boundary.inflected_forms("ken")                     # no Russian -ya
    assert {"diego", "dim"}.isdisjoint(entity_boundary.inflected_forms("di"))        # adjectival: three letters only


def test_a_y_after_a_vowel_declines_like_a_consonant():
    assert {"raya", "rayem", "rayowi"} <= entity_boundary.inflected_forms("ray")
    assert "guya" in entity_boundary.inflected_forms("guy")
    assert {"ami", "amie"} <= entity_boundary.inflected_forms("amy")                 # after a consonant, a vowel


@pytest.mark.parametrize("alias, text", [
    ("Jan", "The meeting moved to January."), ("Mo", "See you on Monday."), ("Zan", "Flights to Zanzibar."),
    ("Ula", "A day trip to Ulm."), ("Ira", "News from Iran."), ("Ana", "A layover in Anaheim."),
    # a two-letter name and a vowel-vowel name do not decline on a stem
    ("Bo", "Coffee at the Be Kind cafe."), ("Leo", "Dinner with Mr Lee."),
    # endings left out because they make other names and ordinary words
    ("Lea", "Dinner with Mr Lee."), ("Rob", "We met Robin there."), ("Mo", "Open Mon to Fri."),
    ("Eve", "A walk with Even and Tor."), ("Ken", "A safari in Kenya."), ("Mel", "Lunch with Melissa."),
    ("Rob", "A call from Robert."), ("Ma", "A week in Malta."), ("Kit", "Kitchen is clean, finally."),
])
def test_a_proper_noun_that_is_not_an_inflected_form_still_releases(alias, text):
    assert not boundary(alias).mentions_protected(text)


@pytest.mark.parametrize("text", ["Uli", "Uli, Mara Example", "Mara Example, Zanka", "ULI"])
def test_in_a_name_list_or_a_field_value_every_capitalised_word_is_a_proper_noun(text):
    """A people column or a field value has no prose, so its first word is a name like any other."""
    gate = boundary("Ula", "Zan")
    assert gate.mentions_protected(text)


def test_a_journal_people_column_naming_an_inflected_form_withholds(node):
    _protect(node, "Ulrike Varnell", ["Ula"])
    _entry(node, "e-people", "Lunch after the run.", people="Uli")
    _entry(node, "e-people-many", "Coffee after the swim.", people="Mara Example, Uli")
    assert _floors_code(node, "e-people") == "entity_protected"
    assert _floors_code(node, "e-people-many") == "entity_protected"


def test_proper_tokens_read_the_case_the_text_was_written_in():
    assert entity_boundary.proper_tokens("Kolacja u Uli.") >= {"uli"}
    assert "uli" not in entity_boundary.proper_tokens("Uli przyszla.")              # opens the text
    assert "uli" not in entity_boundary.proper_tokens("Koniec. Uli przyszla.")      # opens a sentence
    assert "uli" not in entity_boundary.proper_tokens("Lista:\n- Uli przyszla")     # opens a list item
    assert "uli" not in entity_boundary.proper_tokens("kolacja u uli")              # lower case
    assert "uli" in entity_boundary.proper_tokens("Lista: Uli przyszla")            # after a colon, a proper noun
    assert "uli" in entity_boundary.proper_tokens("Uli")                            # no prose: a name
    assert "uli" not in entity_boundary.proper_tokens("uli, mara")                  # lower case is not


def test_inflected_forms_of_an_initial_a_vowel_less_pair_or_another_script_are_none():
    assert entity_boundary.inflected_forms("j") == frozenset()
    assert entity_boundary.inflected_forms("th") == frozenset()
    assert entity_boundary.inflected_forms("\u043b\u0438\u0434") == frozenset()
    assert entity_boundary.inflected_forms("\u00f8le") == frozenset()                # another script, with a vowel
    assert entity_boundary.inflected_forms("abel") == frozenset()
    assert "same" not in entity_boundary.inflected_forms("sam")                      # no "-e" after a consonant
    assert "time" not in entity_boundary.inflected_forms("tim")
    assert "zane" not in entity_boundary.inflected_forms("zan")


def test_v5_only_ever_adds_to_v4(monkeypatch):
    """Over every message and claim in the synthetic entailment cases and a set of inflected sentences: wherever v4
    (v5 without the inflected forms) matched, v5 matches, and v5 adds matches v4 missed."""
    texts = []
    for path in sorted(CASES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.extend(v for v in json.loads(line).values() if isinstance(v, str) and " " in v)
    texts += ["Kolacja u Uli.", "Urodziny u Reosia.", "We borrowed Iras car.", "Spacer z Zanem.", "Zebby and Abie.",
              "Obed u Iva.", "Kolacja u Joego.", "Kafe s Ulinkou.", "Spacer z Rayem.", "Notes on PII."]
    gates = [boundary(alias) for alias in ("Ula", "Reo", "Ira", "Zan", "Abe", "Sam", "Jo", "Ivo", "Joe", "Ray", "Pia")]
    def verdicts():
        return [gate.mentions_protected(text) for gate in gates for text in texts]
    without_v8(monkeypatch)
    v5 = verdicts()
    monkeypatch.setattr(entity_boundary, "_inflections", lambda short_terms: frozenset())
    monkeypatch.setattr(entity_boundary, "_named", lambda short_terms: frozenset())
    v4 = verdicts()
    assert all(new or not old for new, old in zip(v5, v4))
    assert sum(v4) > 0 and sum(new and not old for new, old in zip(v5, v4)) >= 8


def test_a_journal_entry_naming_a_protected_person_by_an_inflected_form_is_withheld(node):
    _protect(node, "Ulrike Varnell", ["Ula"])
    _entry(node, "e-alias", "Kolacja u Uli w piatek.")
    _entry(node, "e-lower", "kolacja u uli w piatek.")
    _entry(node, "e-clear", "Kolacja u Ulricha w piatek.")
    assert _floors_code(node, "e-alias") == "entity_protected"
    assert _floors_code(node, "e-lower") is None                       # lower case: not a proper noun (a residual)
    assert _floors_code(node, "e-clear") is None


def test_a_three_letter_name_word_takes_inflected_forms_and_a_two_letter_one_none(node):
    _protect(node, "Ula Varnell", [])                                  # the full name only, no alias
    _entry(node, "e-word", "Kolacja u Uli w piatek.")
    assert _floors_code(node, "e-word") == "entity_protected"
    gate = boundary(canonical="Ana de la Cruzado")
    assert gate.name_short_words == {"ana", "de", "la"}
    row = {"entry_id": "j1", "source_id": "s", "content": "Flew home via Las Vegas and Des Moines."}
    assert not gate.observe(table="journal_entries", record_id="j1", source_id="s", dataset_id=None, row=row)[0]
    row = {"entry_id": "j2", "source_id": "s", "content": "Lunch with Anita today."}
    assert gate.observe(table="journal_entries", record_id="j2", source_id="s", dataset_id=None, row=row)[0]
    assert gate.mentions_protected("Lunch with Anita today.")          # since v8 (N6) every kind reads name words


def test_a_message_naming_a_protected_person_by_an_inflected_form_is_withheld(protected_corpus):
    edit(protected_corpus, "UPDATE entities SET aliases_json='[\"Ula\"]' WHERE entity_id='protected-entity'")
    edit(protected_corpus, "UPDATE conversation_messages SET content='Kolacja u Uli w piatek.'")
    assert decision(protected_corpus).verdict == "withheld"


# --- v6: a name that is not an English word, other languages' endings, tag characters, a second Goal line ----------
# An independent blind set (set 3) released two journal entries naming a protected person only by the possessive s of
# a short alias ending in a vowel, written as a sentence's first word, where v5 reads every capitalised word as
# ordinary; it matched 43 of its 64 short-form cases. A name that is not itself an English word is a name wherever it
# is written, so v6 lets its forms withhold wherever capitalised, adds the endings a multilingual owner writes on a
# name, reads every default-ignorable code point through and withholds any text in Unicode tag characters.

def _tags(text):
    return "".join(chr(0xE0000 + ord(ch)) for ch in text)


@pytest.mark.parametrize("alias, text", [
    # the possessive or plural s, and other forms, opening a sentence or in capitals inside prose
    ("Oti", "Otis car is red."), ("Oti", "Notes:\n- Otis plan worked."), ("Oti", "We met OTIS sister today."),
    ("Uka", "Ukka came by."), ("Zub", "Zubek dzwonil rano."), ("Zan", "Notatki:\n- Zankiem sie zajelam."),
    # Finnish, Dutch, Basque, Yiddish and Korean endings, on the name or (a vowel-final three-letter name) its stem
    ("Oti", "Soitin eilen Otille."), ("Oti", "Koffie met Otitje."), ("Oti", "Bazkaria Otirekin."),
    ("Oti", "A letter from Otiko."), ("Uka", "Ukele is visiting."), ("Oti", "Coffee with Otissi."),
    ("Oti", "Otiya, come here."), ("Zub", "A gift for Zubtje."),
    # a doubled first syllable
    ("Uka", "Dinner with Ukuk tonight."),
    # in capitals too, so an acronym that spells such a name's form withholds (the cost of reading capitals)
    ("Ian", "Ask IANA for the list."),
])
def test_a_name_that_is_not_an_english_word_withholds_its_forms_wherever_capitalised(alias, text, monkeypatch):
    assert boundary(alias).mentions_protected(text)
    without_v8(monkeypatch)
    monkeypatch.setattr(entity_boundary, "_named", lambda short_terms: frozenset())
    assert not boundary(alias).mentions_protected(text)                # each was a miss under v5


@pytest.mark.parametrize("alias, text", [
    # an English word's forms keep the proper-noun place: its plural opens sentences ("Rays", "Days", "Kitchen")
    ("Ray", "Rays of light came in."), ("Day", "Days later we left."), ("Kit", "Kitchen is clean, finally."),
    ("Eve", "Eves are long in June."),
    # a form that is itself a short English word is never a name's
    ("Eko", "Eke out a living, they said."),
    # lower case stays ordinary, as in v5
    ("Oti", "the otis were late."),
])
def test_an_english_word_or_a_lower_case_form_is_not_a_named_form(alias, text):
    assert not boundary(alias).mentions_protected(text)


def test_named_forms_leave_english_words_out():
    assert entity_boundary.named_forms("ray") == frozenset()                 # "ray" is an English word
    assert "eke" in entity_boundary.inflected_forms("eko") and "eke" not in entity_boundary.named_forms("eko")
    assert {"otis", "otille", "otitje", "otiren", "otile", "otiya", "otot"} <= entity_boundary.named_forms("oti")
    assert entity_boundary.named_forms("th") == entity_boundary.named_forms("\u043b\u0438\u0434") == frozenset()
    assert {"ray", "day", "eve", "kit"} <= english_short_words.WORDS_2_3
    assert {"was", "days", "does"} <= english_short_words.WORDS_ENDING_S_3_4
    words = english_short_words.WORDS_2_3 | english_short_words.WORDS_ENDING_S_3_4
    assert all(word.isascii() and word.isalpha() and word.islower() for word in words)


@pytest.mark.parametrize("text", [
    "Notes for the trip: " + _tags("Oti") + " will drive.",                  # a name spelled in tags alone
    "Plain words" + chr(0xE0001) + " and nothing else.",                        # any tag character at all
])
def test_text_in_unicode_tag_characters_withholds_outright(text):
    assert boundary("Oti").mentions_protected(text)
    assert boundary("Mara Example").mentions_protected(text)                   # whatever the protected name


def test_tag_characters_withhold_only_where_someone_is_protected(protected_corpus):
    assert entity_boundary.text_hits("x" + chr(0xE0041), frozenset(), [])
    gate = boundary("Oti")
    assert not gate.mentions_protected("Plain words and nothing else.")


@pytest.mark.parametrize("char", [0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x17B4, 0x180E, 0x200B, 0x200D, 0x202E,
                                  0x2060, 0x2066, 0x3164, 0xFE0F, 0xFEFF, 0xFFA0, 0x1BCA0, 0x1D173, 0xE0100])
def test_every_default_ignorable_code_point_is_read_through(char):
    assert entity_boundary.normalized("O" + chr(char) + "ti") == "oti"
    assert boundary("Oti").mentions_protected("Lunch with O" + chr(char) + "ti today.")
    assert boundary("Ula").mentions_protected("Kolacja u U" + chr(char) + "li w piatek.")


@pytest.mark.parametrize("content", [
    "Goal: walk daily\n\nGoal: run a marathon",                                 # a second Goal paragraph
    "Goal: walk daily\n\nNotes.\n- goal : run a marathon",                      # a Goal line in a list
    "Goal: walk daily\n\nNotes.\n" + chr(0xFF27) + "oal" + chr(0xFF1A) + " run",  # fullwidth
    "Goal: walk daily\n\nG" + chr(0x200B) + "oal: run",                         # an invisible character inside
    "Goal: walk daily\n\n> GOAL: run",                                          # quoted, in capitals
])
def test_a_further_goal_line_is_a_mismatch(content):
    entry = {"content": content, "metadata_json": json.dumps({"template": "time-log", "goal": "walk daily"})}
    assert journal_goal_field.field_state(entry) == (None, "goal_field_mismatch")


@pytest.mark.parametrize("content", ["Goal: walk daily", "Goal: walk daily\n\nMy goal: stay with it.",
                                     "Goal: walk daily\n\nNotes on the goal."])
def test_one_goal_line_still_states_the_field(content):
    entry = {"content": content, "metadata_json": json.dumps({"template": "time-log", "goal": "walk daily"})}
    assert journal_goal_field.field_state(entry) == ("walk daily", None)


def test_v6_only_ever_adds_to_v5(monkeypatch):
    """Over every message and claim in the synthetic entailment cases and a set of v6 sentences: wherever v5 (v6
    without named forms, tag characters and the wider ignorable set) matched, v6 matches, and v6 adds matches."""
    texts = []
    for path in sorted(CASES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.extend(v for v in json.loads(line).values() if isinstance(v, str) and " " in v)
    texts += ["Otis car is red.", "Soitin eilen Otille.", "Kolacja u U" + chr(0x3164) + "li.", "Kolacja u Uli.",
              "Rays of light came in.", "Notes: " + _tags("Oti"), "We met OTIS sister today."]
    gates = [boundary(alias) for alias in ("Oti", "Ula", "Ray", "Zub", "Sam", "Jo")]
    def verdicts():
        return [gate.mentions_protected(text) for gate in gates for text in texts]
    v6 = verdicts()
    monkeypatch.setattr(entity_boundary, "_named", lambda short_terms: frozenset())
    monkeypatch.setattr(entity_boundary, "TAG_CHARACTERS", re.compile("(?!)"))
    monkeypatch.setattr(entity_boundary, "_IGNORABLE", {})
    v5 = verdicts()
    assert all(new or not old for new, old in zip(v6, v5))
    assert sum(v5) > 0 and sum(new and not old for new, old in zip(v6, v5)) >= 4


# --- v7: another script, look-alike letters, digits for letters, more case endings; Goal lines in any column; money --
# An independent blind set (set 5) released a journal entry whose Goal line stood in the people column, one naming a
# protected person with digits for letters, and a finance goal; and the boundary released 56 of its 139 Off-limits
# entries whole: names in Cyrillic or Greek, look-alike letters, Hungarian, Turkish and Baltic case endings.

@pytest.mark.parametrize("alias, text", [
    # transliterated from Cyrillic or Greek, and a stroked letter spelled out
    ("Zub", "Vchera " + "".join(map(chr, (0x0417, 0x0443, 0x0431, 0x0435, 0x043A))) + " zvonil."),
    ("Oti", "Kafes me ton " + "".join(map(chr, (0x039F, 0x03C4, 0x03B9, 0x03C2))) + " simera."),
    ("Ola", "Lunch with " + chr(0x00D8) + "la today."),
    # look-alike letters CONFUSABLES reads otherwise (a Greek nu reads as n there, but looks like v)
    ("Vok", "Coffee with " + chr(0x03BD) + "ok today."),
    # Hungarian, Turkish, Lithuanian, Greek-in-Latin, Romanian and Estonian endings on a name that is not English
    ("Oti", "Beszeltem Otival tegnap."), ("Oti", "Otinak adtam a konyvet."), ("Oti", "Elmentem Otihoz."),
    ("Oti", "Otinin evi buyuk."), ("Oti", "Otiden haber yok."), ("Oti", "Gyvenu pas Otioje."),
    ("Oti", "Kafes me ton Otiaki."), ("Oti", "Cartea Otiului e noua."), ("Oti", "Lahen Otisse homme."),
    ("Zub", "Talalkoztam Zubbal."),
    # a one- or two-letter ending, written as a proper noun
    ("Oti", "Lattam Otiat tegnap."), ("Oti", "Kirje Otiga kaasa."), ("Oti", "Gyvenu su Otiui."),
])
def test_v7_reads_another_script_look_alikes_and_more_endings(alias, text, monkeypatch):
    assert boundary(alias).mentions_protected(text)
    without_v8(monkeypatch)
    monkeypatch.setattr(entity_boundary, "_readings", lambda value: iter([value]))
    monkeypatch.setattr(entity_boundary, "_short_named", lambda short_terms: frozenset())
    monkeypatch.setattr(entity_boundary, "_named", lambda short_terms: frozenset())
    monkeypatch.setattr(entity_boundary, "_inflections", lambda short_terms: frozenset())
    assert not boundary(alias).mentions_protected(text)                # each was a miss before these forms


@pytest.mark.parametrize("text", ["M4rta K0walsk4 came by.", "Lunch with M@rta Kowal$ka.", "Dinner: Marta K0wa1ska"])
def test_digits_and_symbols_standing_for_letters_read_as_the_name(text):
    assert boundary("Marta Kowalska").mentions_protected(text)
    assert not boundary("Marta Kowalska").mentions_protected("Room 101 at 10am, then 4.5 km.")


@pytest.mark.parametrize("alias, text", [
    ("Di", "Version d1 shipped."), ("Mo", "Use the m0 bucket."), ("Al", "Run job a1b2."),    # too short, or an id
    ("Oti", "Otiat later, maybe."),                   # a short ending opening a sentence is ordinary
    ("Ira", "News from Iran."), ("Abe", "Abel called."),        # no single consonant: other names and places
    ("Tim", "Time to go home."), ("Hal", "Halt the build."),     # a short ending that makes an English word
])
def test_v7_leaves_short_words_ids_and_english_words_alone(alias, text):
    assert not boundary(alias).mentions_protected(text)


def test_v7_short_forms_leave_english_words_out():
    assert "time" not in entity_boundary.short_named_forms("tim")
    assert "ids" not in entity_boundary.short_named_forms("ida")
    assert {"otiat", "otiga", "otia"} <= entity_boundary.short_named_forms("oti")
    assert not {"n", "t", "l", "d", "s"} & set(entity_boundary._SHORT_FOREIGN_ENDINGS)      # no single consonant
    assert entity_boundary.short_named_forms("ray") == frozenset()            # an English word takes none
    assert len(english_short_words.WORDS_4) > 5000 and "time" in english_short_words.WORDS_4


@pytest.mark.parametrize("entry", [
    {"content": "Goal: walk daily\n\nNotes.", "people": "Goal: run a marathon"},
    {"content": "Goal: walk daily\n\nNotes.", "people": "Mara Example\nGoal: run"},
    {"content": "Goal: walk daily\n\nNotes.", "metadata_extra": {"note": "Goal: run a marathon"}},
    {"content": "Goal: walk daily\n\nNotes.", "metadata_raw": '{"template": "time-log", "goal": "run", "goal": "walk daily"}'},
])
def test_a_goal_line_in_any_column_or_a_repeated_goal_key_is_a_mismatch(entry):
    metadata = {"template": "time-log", "goal": "walk daily", **entry.pop("metadata_extra", {})}
    entry["metadata_json"] = entry.pop("metadata_raw", None) or json.dumps(metadata)
    assert journal_goal_field.field_state(entry) == (None, "goal_field_mismatch")


def test_a_people_column_or_metadata_without_a_goal_line_still_states_the_field():
    entry = {"content": "Goal: walk daily\n\nNotes.", "people": "Mara Example, goal-setting group",
             "metadata_json": json.dumps({"template": "time-log", "goal": "walk daily", "note": "my goal is fine"})}
    assert journal_goal_field.field_state(entry) == ("walk daily", None)


@pytest.mark.parametrize("goal", [
    "Pay off my credit card", "Put 200 into savings", "Stick to my budget this month", "Refinance the mortgage",
    "Build an emergency fund", "File my taxes", "Pay down the student loan", "Max out my 401k",
    "Start investing in index funds", "Ask for a higher salary",
])
def test_a_finance_goal_is_a_special_category(goal):
    assert _rule(goal, boundary=boundary()) == "goal_field_special_category"


def test_v7_only_ever_adds_to_v6(monkeypatch):
    """Wherever v7 with its readings and short endings off matched, v7 matches, and v7 adds matches."""
    texts = []
    for path in sorted(CASES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                texts.extend(v for v in json.loads(line).values() if isinstance(v, str) and " " in v)
    texts += ["Lattam Otiat tegnap.", "Otinak adtam a konyvet.", "M4rta K0walsk4 came by.", "Lunch with " + chr(0x00D8)
              + "la today.", "Kafes me ton " + "".join(map(chr, (0x039F, 0x03C4, 0x03B9, 0x03C2))) + " simera."]
    gates = [boundary(alias) for alias in ("Oti", "Ola", "Marta Kowalska", "Zub", "Sam", "Jo")]
    def verdicts():
        return [gate.mentions_protected(text) for gate in gates for text in texts]
    v7 = verdicts()
    monkeypatch.setattr(entity_boundary, "_readings", lambda value: iter([value]))
    monkeypatch.setattr(entity_boundary, "_short_named", lambda short_terms: frozenset())
    less = verdicts()
    assert all(new or not old for new, old in zip(v7, less))
    assert sum(less) > 0 and sum(new and not old for new, old in zip(v7, less)) >= 4
