"""Fourth round: two faults of the third round's own, found on its last reading (its backlog B9 and B10).

B9   The owner's mark with a processing tier the node does not know made a carried, waiting entry FULL before the
     tier was refused (400). The entry was left full, with its clean-up never run, until the next valid mark: his
     outside AI client and his routines closed, his model calls moved, on a mark the node had refused.
     Rule: `start_waiting_clean_up` checks the tier before it makes the entry full.

B10  An entry the upgrade step made under the name the owner had saved the contact by was shown as "A contact with no
     saved name" once the contact's own row was gone (a contact removed at its source), although the entry still
     holds that name. Rule: `display_label` falls back to the entry's own name before the fixed words; never to a
     contact id.

Every person, handle and id here is invented.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.topos.test_carried_entry_waits import ORDINARY, excluded
from tests.topos.test_carry_step_review_r1 import HEART, PHONE, cid, contact, entity
from tests.topos.test_off_limits_doors_for_carried_entries import conn, relay_list, relay_mark  # noqa: F401 (conn: fixture)
from topos.features.lifecycle.blackhole import OWNER, BlackholeStore, has_waiting, start_waiting_clean_up
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.features.lifecycle.off_limits_list import NAMELESS, display_label, listing

pytestmark = pytest.mark.public


def carried(c, spec=None):
    excluded(c, spec or ORDINARY["saved as Sam"])
    carry_contact_excludes(c)
    c.commit()
    (entry,) = BlackholeStore(c).list()
    return entry


# ------------------------------------------------------------------------------------------------------------- B9

@pytest.mark.parametrize("tier", ["bogus", "", "SECURE", "local-only"])
def test_a_tier_the_node_does_not_know_is_refused_before_a_waiting_entry_is_made_full(conn, tier):  # noqa: F811
    entry = carried(conn)
    store = BlackholeStore(conn)
    with pytest.raises(ValueError):
        start_waiting_clean_up(store, entry["blackhole_id"], processing_tier=tier, note=None)
    after = store.get(entry["blackhole_id"])
    assert after == entry and after["carried_waiting"] and has_waiting(after)       # not rewritten at all
    assert store.get(entry["blackhole_id"], view=OWNER) is None                     # still not there for his own tools
    assert store.waiting_count() == 1
    # and a tier it knows still is the owner's act
    start_waiting_clean_up(store, entry["blackhole_id"], processing_tier="secure", note=None)
    assert not store.get(entry["blackhole_id"])["carried_waiting"]


@pytest.mark.asyncio
async def test_the_door_answers_400_and_the_entry_still_waits(conn):  # noqa: F811
    """Through the relay door the app's switch uses: the refusal is the 400 it always was, and what the list says
    of the entry afterwards is what it said before."""
    entry = await asyncio.to_thread(carried, conn)
    before = (await relay_list())["blackholes"]
    answer = await relay_mark(entry["blackhole_id"], processing_tier="bogus")
    assert (answer["status"], answer.get("code")) == ("error", 400), answer
    after = (await relay_list())["blackholes"]
    assert after == before and after[0]["carried_waiting"] is True and after[0]["clean_up"]["state"] == "not_started"
    assert (await relay_list())["carried"] == {"waiting": 1}


# ------------------------------------------------------------------------------------------------------------ B10

def test_an_entry_made_by_a_saved_name_keeps_that_name_when_the_contact_row_is_gone(conn):  # noqa: F811
    contact(conn, cid("0b"), "Wilhelmina Okonjo-Reyes")
    carry_contact_excludes(conn)
    conn.commit()
    (row,) = listing(conn)["blackholes"]
    assert row["display_label"] == "Wilhelmina Okonjo-Reyes"
    conn.execute("DELETE FROM contacts WHERE contact_id=?", (cid("0b"),))    # removed at its source after the upgrade
    conn.commit()
    (row,) = listing(conn)["blackholes"]
    assert row["display_label"] == "Wilhelmina Okonjo-Reyes" and row["carried_waiting"] is True


def test_the_order_of_the_label_is_unchanged_while_the_contact_is_there(conn):  # noqa: F811
    """The saved name first, else the first handle, else the linked entity's name: as before."""
    contact(conn, cid("0a"), "Bree V.", handles=[(PHONE, "phone")])
    entity(conn, "ent-1", "Brisa Vantongeren", cid("0a"))
    contact(conn, cid("0c"), HEART, handles=[("wilf@fernmail.example", "email")])
    carry_contact_excludes(conn)
    conn.commit()
    assert sorted(row["display_label"] for row in listing(conn)["blackholes"]) == ["Bree V.", HEART]


def test_a_contact_id_is_never_the_label_even_as_the_last_resort(conn):  # noqa: F811
    """An entry the step named by the contact's id (nothing else to name it by) has no name of its own to fall back
    to: with the contact row there or gone it is shown under the fixed words."""
    contact(conn, cid("0d"), None)
    carry_contact_excludes(conn)
    conn.commit()
    (record,) = BlackholeStore(conn).list()
    assert display_label(conn, record) == NAMELESS
    conn.execute("DELETE FROM contacts WHERE contact_id=?", (cid("0d"),))
    conn.commit()
    assert display_label(conn, record) == NAMELESS and cid("0d") not in display_label(conn, record)


def test_an_entry_named_by_a_handle_keeps_the_handle_when_the_contact_row_is_gone(conn):  # noqa: F811
    contact(conn, cid("0c"), HEART, handles=[("wilf@fernmail.example", "email")])
    carry_contact_excludes(conn)
    conn.commit()
    conn.execute("DELETE FROM contact_identifiers WHERE contact_id=?", (cid("0c"),))
    conn.execute("DELETE FROM contacts WHERE contact_id=?", (cid("0c"),))
    conn.commit()
    (row,) = listing(conn)["blackholes"]
    assert row["display_label"] != NAMELESS and "wilf" in row["display_label"].lower()
