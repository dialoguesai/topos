"""Review R1 (node), R-M5: a handle, a username and an id of an Off-limits entry are read as handles, never as names.

protects: the step that carries the older per-person excludes wrote a contact's handles, usernames and the contact id
as aliases, and the boundary read every alias as a name: each word of three letters or more became a name part. Every
node that carried one contact then withheld every journal entry holding "contact" or "default" (the words of the
contact id, `<owner>:default:contact:<digest>`), and an address added the words of its domain.

An entry now lists which of its aliases are identifiers (`entity_blackholes.identifier_aliases_json`), and the boundary
reads those where it reads a contact's own handles: whole (`_handle_keys`), no name part, no short name word. What
does not change, and these tests hold it too:
  - each identifier still withholds as itself, and still reaches the contact that carries it (and so its threads);
  - every NAME of the same entry is read exactly as before: its parts, its forms;
  - an entry without the list (every entry made before, every hand-made one) reads every alias as a name;
  - a list that cannot be read withholds everything.
The N6 name-form cases (test_entity_boundary_v8.py) are untouched. Every person, handle and id here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA, withheld
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary

NAME = "Quorra Vellaby"
# A node's contact id is "<owner uuid>:default:contact:<digest>"; the store keeps aliases normalised, so the colons
# are spaces by the time the boundary reads it. The uuid and the digest are made up.
CONTACT_ID = "3f9c2e71-6b0d-4a58-9e13-7c5d2b8e4f60:default:contact:7471fce8530d7bd0"
CONTACT_ALIAS = "3f9c2e71-6b0d-4a58-9e13-7c5d2b8e4f60 default contact 7471fce8530d7bd0"
EMAIL = "q.vellaby@mail.example"
PHONE = "+1 555 " + "0142 " + "0137"            # written with separators: no ten-digit run in this file
USERNAME = "hopewell"
IDENTIFIERS = [CONTACT_ALIAS, EMAIL, PHONE, USERNAME]
UNSET = object()


def gate(canonical=NAME, aliases=(NAME.lower(), *IDENTIFIERS), identifiers=IDENTIFIERS, *, contacts=(),
         handles=(), participants=()):
    """The real boundary over one entry; `identifiers` UNSET builds the table without the column at all."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    if identifiers is UNSET:
        conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?)",
                     (canonical.lower(), canonical, json.dumps(list(aliases))))
    else:
        conn.execute("ALTER TABLE entity_blackholes ADD COLUMN identifier_aliases_json TEXT")
        conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?,?)",
                     (canonical.lower(), canonical, json.dumps(list(aliases)),
                      identifiers if identifiers is None or isinstance(identifiers, str) else json.dumps(identifiers)))
    conn.executemany("INSERT INTO contacts VALUES(?,?)", contacts)
    conn.executemany("INSERT INTO contact_identifiers VALUES(?,?,?)", handles)
    conn.executemany("INSERT INTO conversation_participants VALUES('thread-1','source-1','dataset-1',?)",
                     [(contact,) for contact in participants])
    return EntityBoundary(conn)


ORDINARY = ("Lost a contact lens at the pool again.", "Dark mode is the default on the new phone.",
            "Contact the landlord about the boiler.", "The mail was late; an example follows.",
            "Keep in contact, by default I answer on Mondays.")


def test_the_words_of_an_id_or_an_address_are_nobodys_name():
    """Rule: `EntityBoundary._names` reads a value the entry lists as an identifier through `_handle`. Read it as a
    name again and "contact", "default", "mail" and "example" are name parts, and every sentence below withholds."""
    boundary = gate()
    assert boundary.name_parts == {"quorra", "vellaby"}
    assert boundary.name_short_words == set()
    for text in ORDINARY:
        for kind in ("journal_entry", "message", "goal", "fact"):
            assert not withheld(kind, boundary, text), (kind, text)


def test_each_identifier_still_withholds_as_itself():
    boundary = gate()
    for text in (f"Write to {EMAIL} today.", f"Call {PHONE} after six.",
                 "Call (555) 0142-0137 after six.",                   # a phone: its last ten digits, however written
                 f"Ping {USERNAME} about it.", f"See {CONTACT_ID} in the export."):
        for kind in ("journal_entry", "message"):
            assert withheld(kind, boundary, text), (kind, text)
    assert {"qvellabymailexample", "hopewell"} <= boundary.terms and {"qvellabymailexample", "hopewell"} <= boundary.handles


def test_an_identifier_still_reaches_the_contact_that_carries_it():
    """The older exclude's reach: the contact itself, and with it every thread it takes part in."""
    by_id = gate(contacts=[(CONTACT_ID, None)], participants=[CONTACT_ID])
    assert CONTACT_ID in by_id.contacts and withheld("message", by_id, "Plans for the weekend.")
    by_handle = gate(contacts=[("contact-77", None)], handles=[("contact-77", EMAIL, "email")],
                     participants=["contact-77"])
    assert "contact-77" in by_handle.contacts and withheld("message", by_handle, "Plans for the weekend.")
    stranger = gate(contacts=[("contact-78", None)], participants=["contact-78"])
    assert "contact-78" not in stranger.contacts and not withheld("message", stranger, "Plans for the weekend.")


def test_the_names_of_the_same_entry_are_read_exactly_as_before():
    """Nothing about a real name is loosened: the same verdicts with the list and without it."""
    marked, unmarked = gate(), gate(aliases=(NAME.lower(),), identifiers=UNSET)
    for kind, text in (("message", "Lunch with Quorra on Friday."), ("journal_entry", "saw vellaby by the canal"),
                       ("message", "Borrowed Vellabys ladder."), ("goal", "Send Quorra Vellaby the photos."),
                       ("message", "the vellaby question"), ("journal_entry", "nothing of note today")):
        assert withheld(kind, marked, text) == withheld(kind, unmarked, text), (kind, text)
    assert withheld("message", marked, "Lunch with Quorra on Friday.")
    assert withheld("journal_entry", marked, "saw vellaby by the canal")


def test_a_value_that_is_not_listed_stays_a_name_even_when_it_looks_like_a_handle():
    """Only the entry's own list decides. An alias the owner gave as a name is a name, whatever its shape."""
    boundary = gate(aliases=(NAME.lower(), "sam", "hopewell"), identifiers=[])
    assert {"sam", "hopewell"} <= boundary.name_parts and "sam" in boundary.name_short_words


@pytest.mark.parametrize("identifiers", [UNSET, None], ids=["no_column", "null"])
def test_an_entry_without_the_list_reads_every_alias_as_a_name_as_before(identifiers):
    boundary = gate(identifiers=identifiers)
    assert {"contact", "default", "mail", "example", "hopewell"} <= boundary.name_parts
    assert withheld("journal_entry", boundary, "Lost a contact lens at the pool again.")


@pytest.mark.parametrize("stored", ["{not json", json.dumps({"a": 1}), json.dumps([1, 2]), json.dumps("x")])
def test_a_list_that_cannot_be_read_withholds_everything(stored):
    with pytest.raises(PolicyError) as refused:
        gate(identifiers=stored)
    assert refused.value.code == "entity_protection_lineage_unavailable"


def test_an_entry_named_by_its_own_handle_gives_no_name_part():
    """A contact with no usable name is carried under a handle or its id: that name is an identifier too."""
    boundary = gate(canonical=EMAIL, aliases=(EMAIL, CONTACT_ALIAS), identifiers=[EMAIL, CONTACT_ALIAS],
                    contacts=[("contact-77", None)], handles=[("contact-77", EMAIL, "email")],
                    participants=["contact-77"])
    assert boundary.name_parts == set()
    assert "contact-77" in boundary.contacts and withheld("message", boundary, "Plans for the weekend.")
    assert not withheld("journal_entry", boundary, "The mail was late; an example follows.")
    assert withheld("journal_entry", boundary, f"Write to {EMAIL} today.")
