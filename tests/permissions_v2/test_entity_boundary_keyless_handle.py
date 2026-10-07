"""Third fix round, review R2-H2: one contact handle with no letter or digit no longer turns every share off.

The boundary every share read builds refused to be built when a contact it reaches had a handle that holds no letter
and no digit ("._.", "__", a dash, "+", an emoji, spaces). The upgrade step makes every excluded contact a reached
contact at the first start, so one such handle on one excluded contact, and from then on every share on the node
refused every read, while the step reported that it had succeeded. The re-check reproduced it with six handles.

protects:
  - such a handle is passed over: it can match no text and reach nobody, so nothing is lost; the contact is still
    reached by everything else it has, and its conversations are still withheld;
  - NOTHING ELSE is loosened. Every other value the closure cannot read still withholds everything, one case per
    fault below. Before this change no test held any of those refusals for a reached contact: the re-check's own
    proposed fix left all 995 boundary and carry tests green.
  - the step builds the boundary once after it has written, and a boundary that still refuses fails the step by name,
    with a notice the owner sees.
Every person, handle and id here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA, withheld
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import UNAVAILABLE, EntityBoundary

pytestmark = pytest.mark.public

NAME = "Quorra Vellaby"
CONTACT = "dataset-1:contact:7471fce8530d7bd0"
KEYLESS = ["._.", "__", "—", "+", "\U0001F338", "   "]
KEYLESS_IDS = ["dots_and_underscore", "underscores", "a_dash", "a_plus", "an_emoji", "spaces"]


def home(*, handles=(), usernames=None, entity_identifiers=None, aliases=None, identifiers="unset", name=NAME,
         in_thread=True, contact_row=True):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE entity_blackholes ADD COLUMN identifier_aliases_json TEXT")
    conn.execute("ALTER TABLE contacts ADD COLUMN known_usernames_json TEXT")
    conn.execute("INSERT INTO entity_blackholes VALUES('b1',?,?,?,?,?)",
                 ("ent-1" if entity_identifiers is not None else "", name.lower(), name,
                  aliases if aliases is not None else json.dumps([NAME.lower()]),
                  None if identifiers == "unset" else identifiers))
    if contact_row:
        conn.execute("INSERT INTO contacts VALUES(?,?,?)", (CONTACT, NAME, usernames))
    conn.executemany("INSERT INTO contact_identifiers VALUES(?,?,?)", [(CONTACT, handle, "username") for handle in handles])
    if entity_identifiers is not None:
        conn.execute("INSERT INTO entities VALUES('ent-1',?,?,'[]',?,?)", (NAME, NAME.lower(), entity_identifiers, CONTACT))
    if in_thread:
        conn.execute("INSERT INTO conversation_participants VALUES('thread-1','source-1','dataset-1',?)", (CONTACT,))
    return conn


@pytest.mark.parametrize("handle", KEYLESS, ids=KEYLESS_IDS)
def test_a_reached_contacts_handle_with_no_letter_or_digit_is_passed_over(handle):
    """Rule: `_close_identities` skips a reached contact's handle whose skeleton is empty. Raise for it again and
    this boundary cannot be built, which is every share on the node refusing every read."""
    boundary = EntityBoundary(home(handles=[handle, "q.vellaby@fernmail.example"]))
    assert boundary.active and CONTACT in boundary.contacts
    assert "qvellabyfernmailexample" in boundary.terms and "" not in boundary.terms
    assert withheld("message", boundary, "Nothing in this text names anyone.")      # the contact is in the thread
    assert withheld("journal_entry", boundary, "Wrote to q.vellaby@fernmail.example today.")
    assert withheld("journal_entry", boundary, "Dinner with Quorra Vellaby.")
    assert not withheld("journal_entry", boundary, "A quiet week: " + handle)        # and the handle matches nothing


FAULTS = {
    # one per value the closure cannot read, each on a contact or entity the entry REACHES
    "a_handle_that_is_not_text": dict(handles=[b"\x00\x01"]),             # bytes: a text column keeps a number as text
    "an_entity_identifier_with_no_letter_or_digit": dict(entity_identifiers=json.dumps(["._."])),
    "entity_identifiers_that_are_not_a_list": dict(entity_identifiers=json.dumps({"a": 1})),
    "an_entity_identifier_that_is_not_text": dict(entity_identifiers=json.dumps(["ok", 7])),
    "entity_identifiers_that_are_not_json": dict(entity_identifiers="{not json"),
    "usernames_that_are_not_a_list": dict(usernames=json.dumps({"a": 1})),
    "a_username_that_is_not_text": dict(usernames=json.dumps(["ok", 7])),
    "usernames_that_are_not_json": dict(usernames="[not json"),
    "aliases_that_are_not_a_list": dict(aliases=json.dumps({"a": 1})),
    "an_alias_that_is_not_text": dict(aliases=json.dumps(["ok", 7])),
    "aliases_that_are_not_json": dict(aliases="[not json"),
    "an_identifier_list_that_is_not_a_list": dict(identifiers=json.dumps({"a": 1})),
    "an_identifier_list_that_is_not_json": dict(identifiers="[not json"),
    "an_entry_named_by_no_letter_or_digit": dict(name="._."),
}


@pytest.mark.parametrize("fault", list(FAULTS))
def test_every_other_lineage_fault_still_withholds_everything(fault):
    """The pass is for a reached contact's keyless handle and for nothing else. Widen it (let `_handle` answer
    nothing for any value it cannot key, or read past a list it cannot read) and one of these builds."""
    with pytest.raises(PolicyError) as refused:
        EntityBoundary(home(**FAULTS[fault]))
    assert refused.value.code == UNAVAILABLE


def test_a_missing_table_still_withholds_everything():
    conn = home()
    conn.execute("DROP TABLE contact_identifiers")
    with pytest.raises(PolicyError) as refused:
        EntityBoundary(conn)
    assert refused.value.code == UNAVAILABLE
