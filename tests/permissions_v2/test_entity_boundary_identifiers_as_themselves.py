"""Third fix round, ruling M at the share boundary (re-check R2-M3): an identifier matches only as itself.

The owner decided it on 7 Oct ("handles and ids match only as themselves"); the last round applied it in the clean-up
only. At the boundary a contact's handle or username was one more term: four characters or more matched anywhere in
the text with its separators removed, so the handle "work" withheld "network" and "slow or king", and three
characters matched as a whole token AND through a name's pet forms, so the handle "the" withheld "they". On the
re-check's four invented homes one such handle withheld from every share 83 of 820 of the owner's messages and 42 of
150 journal entries ("work"), 45 and 6 ("king"), 534 and 134 ("the").

The rule (`entity_boundary.matches_only_as_itself`, `EntityBoundary._term_groups`):
  - an identifier with no digit and no "@" withholds only where it stands as a whole token, and takes no form;
  - an identifier with a digit or an "@" is read exactly as before (a number written with spaces, an address in a
    link);
  - no identifier is looked for in the KEYS of a JSON column, only in values;
  - a NAME is read exactly as before, and a term that is anybody's name is a name whoever lists it as an identifier.
What an identifier reaches is unchanged: its contact, and so every conversation that contact is in.
Every person, handle and id here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_entity_boundary_identifier_aliases import CONTACT_ALIAS, EMAIL, NAME, PHONE, gate
from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA, withheld
from topos.permissions_v2.entity_boundary import EntityBoundary, matches_only_as_itself

pytestmark = pytest.mark.public

KINDS = ("message", "journal_entry", "goal", "fact")
CONTACT = "dataset-1:contact:7471fce8530d7bd0"


def carried(*identifiers, name=NAME):
    """The boundary over one entry the upgrade step would write: a name, and these identifiers listed as such."""
    return gate(canonical=name, aliases=(name.lower(), *identifiers), identifiers=list(identifiers))


@pytest.mark.parametrize("handle, as_itself, inside_other_words", [
    ("work", ["Back to work on Monday.", "Work: the release.", "@work said so", "ask work/home"],
     ["The network was down.", "Homework is done and the paperwork too.", "A slow or kingly pace.", "Working late."]),
    ("king", ["The king of the hill.", "king, again"],
     ["Working, talking and making plans.", "A booking for two.", "Kings and kingdoms."]),
    ("hopewell", ["hopewell wrote back", "Ask @hopewell."], ["The Hopewellian mounds.", "hopewells"]),
])
def test_a_bare_word_handle_withholds_only_where_it_stands_as_a_whole_token(handle, as_itself, inside_other_words):
    """Rule: `_term_groups` takes an identifier with no digit and no "@" out of the anywhere-in-the-text reading.
    Put it back and every sentence of the second list withholds, as the re-check measured."""
    boundary = carried(handle)
    assert handle in boundary._groups[2] and handle not in boundary._groups[0]
    for kind in KINDS:
        for text in as_itself:
            assert withheld(kind, boundary, text), (kind, text)
        for text in inside_other_words:
            assert not withheld(kind, boundary, text), (kind, text)


@pytest.mark.parametrize("handle, itself, a_names_form", [
    ("the", "Not the one I meant.", "They came on Friday."),              # short_variants("the") holds "they"
    ("al", "Al came by.", "Ally and Allie were there."),                  # a two-letter name's doubled forms
    ("sam", "Lunch with Sam.", "Sammy, Sams and Samsie rang."),           # a three-letter name's pet forms
])
def test_a_short_handle_takes_none_of_a_names_forms(handle, itself, a_names_form):
    """A short NAME also withholds through its pet-name and inflected forms; a handle is not a name."""
    boundary = carried(handle)
    for kind in KINDS:
        assert withheld(kind, boundary, itself), kind
        assert not withheld(kind, boundary, a_names_form), kind
    named = gate(canonical=handle.title(), aliases=(handle,), identifiers=[])     # the same letters as a NAME: unchanged
    assert withheld("journal_entry", named, a_names_form) or handle == "the"      # "the" as a name has the form "they"
    assert withheld("journal_entry", named, itself)


@pytest.mark.parametrize("identifier, written", [
    (PHONE, ["Call +1 555 0142 0137 after six.", "call 555-0142-0137", "tel:+15550142" + "0137"]),
    (EMAIL, ["Write to q.vellaby@mail.example today.", "mailto:q.vellaby@mail.example?subject=hi",
             "Q.Vellaby@Mail.Example"]),
    ("quorra7", ["ping quorra7 about it", "see quorra7's page", "user/quorra7/posts"]),
    (CONTACT_ALIAS, [CONTACT_ALIAS]),
])
def test_an_identifier_with_a_digit_or_an_at_sign_is_read_exactly_as_before(identifier, written):
    """Not narrowed: a number written with spaces or dashes, an address inside a link, a username with a digit."""
    boundary = carried(identifier)
    assert not matches_only_as_itself(identifier) and boundary._groups[2] == frozenset()
    for kind in KINDS:
        for text in written:
            assert withheld(kind, boundary, text), (kind, text)


def test_which_identifiers_match_only_as_themselves():
    assert all(matches_only_as_itself(value) for value in ("work", "al", "the", "j.smith", "hope_well", "Zoë", "王伟"))
    assert not any(matches_only_as_itself(value) for value in (PHONE, EMAIL, "quorra7", CONTACT_ALIAS, "٣٣٣", None, 7))


def test_no_identifier_is_looked_for_in_a_key_only_in_values():
    """A journal entry's metadata is a JSON column, read keys and all. Rule: an identifier is matched against the
    row's values (`keyed_surfaces(row, keys=False)`). Match keys again and every entry whose template has a field
    called "work", or a key that holds a carried number, is withheld from every share."""
    boundary = carried("work", "quorra7")

    def entry(metadata):
        row = {"entry_id": "entry-1", "source_id": "journal", "content": "Notes for the week.", "people": None,
               "metadata_json": json.dumps(metadata)}
        return boundary.observe(table="journal_entries", record_id="entry-1", source_id="journal", dataset_id=None,
                                row=row)[0]

    assert not entry({"work": "the release", "home": "the boiler"})       # a key that is the handle
    assert not entry({"fields": {"quorra7": 3, "work": []}})              # nested keys, and one with a digit
    assert entry({"area": "work"}) and entry({"notes": ["called quorra7 twice"]})   # the same words as VALUES
    # a NAME in a key withholds, as it always did: this round changes nothing for names
    assert entry({"Quorra Vellaby": "birthday"}) and entry({"vellaby": 1})


def test_a_term_that_is_somebodys_name_is_read_as_a_name_whoever_lists_it_as_a_handle():
    """A contact saved as "Work" (a name) whose handle is also "work": the name's reading stands."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE entity_blackholes ADD COLUMN identifier_aliases_json TEXT")
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','','work','Work',?,?)",
                 (json.dumps(["work", CONTACT]), json.dumps([CONTACT])))
    conn.execute("INSERT INTO contacts VALUES(?,?)", (CONTACT, "Work"))
    conn.execute("INSERT INTO contact_identifiers VALUES(?,?,?)", (CONTACT, "work", "username"))
    boundary = EntityBoundary(conn)
    assert "work" in boundary._groups[0] and "work" not in boundary._groups[2]
    assert withheld("journal_entry", boundary, "The network was down.")   # a name of four letters, anywhere: as before


@pytest.mark.parametrize("where", ["listed_by_the_entry", "a_handle_of_the_reached_contact",
                                   "a_username_of_the_reached_contact", "an_identifier_of_the_linked_entity"])
def test_every_way_an_identifier_reaches_the_boundary_follows_the_rule(where):
    """The carried contact's handle is a term whether or not the entry lists it: the closure reads a reached
    contact's own handles and usernames, and a linked entity's identifiers. Each is an identifier."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE entity_blackholes ADD COLUMN identifier_aliases_json TEXT")
    conn.execute("ALTER TABLE contacts ADD COLUMN known_usernames_json TEXT")
    listed = ["work"] if where == "listed_by_the_entry" else []
    conn.execute("INSERT INTO entity_blackholes VALUES('b1',?,?,?,?,?)",
                 ("ent-1" if where == "an_identifier_of_the_linked_entity" else "", NAME.lower(), NAME,
                  json.dumps([NAME.lower(), CONTACT, *listed]), json.dumps([CONTACT, *listed])))
    conn.execute("INSERT INTO contacts VALUES(?,?,?)",
                 (CONTACT, NAME, json.dumps(["work"]) if where == "a_username_of_the_reached_contact" else None))
    if where == "a_handle_of_the_reached_contact":
        conn.execute("INSERT INTO contact_identifiers VALUES(?,?,?)", (CONTACT, "work", "username"))
    if where == "an_identifier_of_the_linked_entity":
        conn.execute("INSERT INTO entities VALUES('ent-1',?,?,'[]',?,NULL)", (NAME, NAME.lower(), json.dumps(["work"])))
    boundary = EntityBoundary(conn)
    assert "work" in boundary.terms and "work" in boundary._groups[2], where
    assert withheld("message", boundary, "Back to work on Monday.")
    assert not withheld("message", boundary, "The network was down.")
    assert not withheld("journal_entry", boundary, "Homework, then paperwork.")


def test_a_handle_still_reaches_its_contact_and_every_conversation_the_contact_is_in():
    """What an identifier REACHES is not narrowed: a handle-only contact in the thread withholds the thread's
    messages whatever they say."""
    boundary = gate(canonical="work", aliases=("work", CONTACT_ALIAS), identifiers=["work", CONTACT_ALIAS],
                    contacts=[(CONTACT, None)], handles=[(CONTACT, "work", "username")], participants=[CONTACT])
    assert CONTACT in boundary.contacts
    assert withheld("message", boundary, "Nothing in this text names anyone.")


def test_the_revision_moves_only_where_some_term_is_an_identifier():
    """So an index built at the last revision is re-qualified exactly where the reading changed."""
    plain = gate(canonical=NAME, aliases=(NAME.lower(),), identifiers=[])
    same = gate(canonical=NAME, aliases=(NAME.lower(),), identifiers=None)
    assert plain.revision == same.revision
    assert carried("work").revision != plain.revision and carried("work").revision != carried("king").revision
