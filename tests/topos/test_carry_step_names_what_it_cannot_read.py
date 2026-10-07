"""Fifth round, item 3 (second re-check, R3-M3): a contact value the boundary cannot read, and what the owner is told.

The share boundary refuses to be built over a value it cannot read (a username list that is not a list of strings,
a saved name that is not text): unknown is never "nobody". Before the upgrade step such a contact is reached by no
Off-limits entry, the boundary builds and shares work. The step carries the contact, the boundary then refuses, and
every share on the node is off from that start on. That is the ruling as built and it stays. What this round adds:

  - the step says WHICH entry: the ledger row holds the entry's own id and the kind of value, and when the boundary
    builds without that entry the owner's notice names it by the label the app shows, so he can remove it and
    sharing comes back at the next start;
  - nothing is promised where removing one entry would not be enough (a value the boundary reads for every contact):
    the notice keeps its general words and the ledger says so;
  - when the step ended failed the hold answers `off_limits_carry_failed` although every contact was dealt with, so
    a new bind is refused too (it answered nothing there).
Every person, handle and id here is invented.
"""

from __future__ import annotations

import json

import pytest

from tests.topos.test_carry_step_review_r1 import DATASET, EXCLUDE, cid, conn  # noqa: F401 (conn: fixture)
from tests.topos.test_exclude_carry_runs_first_and_sharing_waits import (RELEASE, an_upgraded_home,  # noqa: F401
                                                                          the_hold_alone, the_release_as_cut)
from topos.features.lifecycle import contact_excludes
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.contact_excludes import FAILED, NOTICE_FAILED, STEP_ID, hold, owed
from topos.features.lifecycle.off_limits_list import listing
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.upgrades import runner

pytestmark = pytest.mark.public

POLICY = json.dumps(EXCLUDE)
SAVED = "Brisa Vantongeren"
ODD = cid("0b")

#: The second re-check's unreadable values (its `test_s5n_r3_hold.py`): what is stored, the kind the diagnosis must
#: give, and whether removing that one entry is enough while another entry (the home's "Sam") is left.
UNREADABLE = {
    "usernames: not JSON": (dict(usernames="not json"), "contact_usernames", True),
    "usernames: an object": (dict(usernames='{"a": 1}'), "contact_usernames", True),
    "usernames: [null]": (dict(usernames="[null]"), "contact_usernames", True),
    "usernames: [123]": (dict(usernames="[123]"), "contact_usernames", True),
    "usernames: a bare string": (dict(usernames='"brisavt"'), "contact_usernames", True),
    "saved name: bytes": (dict(display=b"\xff\xfe"), "contact_name", False),
    "handle: bytes": (dict(handle=b"\xff\xfe"), "contact_handle", True),
    "linked entity: aliases not JSON": (dict(entity=("not json", "[]")), "entity_names", False),
    "linked entity: aliases an object": (dict(entity=('{"a": 1}', "[]")), "entity_names", False),
    "linked entity: aliases [null]": (dict(entity=("[null]", "[]")), "entity_names", False),
    "linked entity: identifiers not JSON": (dict(entity=("[]", "not json")), "entity_identifiers", True),
    "linked entity: identifiers [null]": (dict(entity=("[]", "[null]")), "entity_identifiers", True),
}


def odd(c, *, display=SAVED, usernames=None, handle=None, entity=None):
    """One more excluded contact, with one value written as no app would write it."""
    c.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self, known_usernames_json, "
              "sharing_policy_json) VALUES (?,?,?,?,0,?,?)", (ODD, DATASET, "src", display, usernames, POLICY))
    if handle is not None:
        c.execute("INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id) "
                  "VALUES (?,?,?,?,?)", (DATASET, "src", handle, "username", ODD))
    if entity is not None:
        aliases, identifiers = entity
        c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
                  "identifiers_json, contact_id, mention_count, metadata_json) VALUES ('ent-odd','person',?,?,?,?,?,3,"
                  "'{}')", (display, str(display).lower(), aliases, identifiers, ODD))
    c.commit()


def builds(c) -> bool:
    try:
        EntityBoundary(c)
    except PolicyError:
        return False
    return True


def the_steps_row(c):
    rows = [row for row in runner.ledger_rows(c) if row["step_id"] == STEP_ID]
    assert len(rows) == 1
    return rows[0]


def notices(c):
    return {n["kind"]: n["message"] for n in BlackholeStore(c).notifications(state="open")}


def path_of(c):
    return c.execute("PRAGMA database_list").fetchone()[2]


@pytest.mark.parametrize("label", list(UNREADABLE))
def test_the_step_says_which_entry_and_what_kind_of_value(conn, label):  # noqa: F811
    """Rule: when the boundary refuses after the carry, the step asks `carry_diagnosis.unreadable` and the ledger
    row holds the entry's own id and the kind; the notice names the entry only when the boundary builds without
    it. Before this round the row held the boundary's code and counts, and the notice said nothing of where."""
    shape, kind, enough = UNREADABLE[label]
    an_upgraded_home(conn, "1.4.4")                                       # one ordinary excluded contact, "Sam"
    odd(conn, **shape)
    assert builds(conn)                                                   # before the step: nobody reaches it
    for _start in range(2):
        runner.run_pending_upgrades(conn)
    assert not builds(conn)
    row = the_steps_row(conn)
    carried = conn.execute(f"SELECT blackhole_id FROM {contact_excludes.CARRIES_TABLE} WHERE contact_id=?",
                           (ODD,)).fetchone()[0]
    position = [entry["blackhole_id"] for entry in BlackholeStore(conn).list()].index(carried) + 1
    assert row["status"] == "failed" and carried in row["detail"]["error"]
    assert row["detail"]["unreadable"] == {"entries": [{"blackhole_id": carried, "position": position,
                                                         "kinds": [kind]}],
                                           "of": 2, "kinds": [kind], "enough": enough}
    shown = {entry["blackhole_id"]: entry["display_label"] for entry in listing(conn)["blackholes"]}[carried]
    said = notices(conn)["carry_failed"]
    if enough:
        assert said == contact_excludes.NOTICE_FAILED_ENTRY.format(who=shown) and shown in said
    else:
        assert said == NOTICE_FAILED
    assert len([n for n in BlackholeStore(conn).notifications(state="open") if n["kind"] == "carry_failed"]) == 1


def test_removing_the_entry_the_notice_names_brings_sharing_back_at_the_next_start(conn):  # noqa: F811
    """What the notice promises, shown: the owner removes that one entry in the app, and the next start finishes
    the step. The other excluded person stays carried; the removed one is no longer excluded, as the notice says."""
    an_upgraded_home(conn, "1.4.4")
    odd(conn, usernames="[null]")
    conn.commit()
    path = path_of(conn)
    runner.run_pending_upgrades(conn)
    contact_excludes.forget_hold()
    assert not builds(conn) and owed(conn) == FAILED and hold(path) == FAILED
    named = the_steps_row(conn)["detail"]["unreadable"]["entries"][0]["blackhole_id"]
    assert SAVED in notices(conn)["carry_failed"]
    removed = BlackholeStore(conn).unblackhole_entity(entity_ref=named)   # the app's "remove", by the entry's own id
    assert removed["removed"] is True
    out = runner.run_pending_upgrades(conn)                               # the next start
    contact_excludes.forget_hold()
    assert (out["steps_run"], out["steps_failed"]) == (1, 0)
    row = the_steps_row(conn)
    assert row["status"] == "done" and row["detail"]["boundary"] == "built" and "unreadable" not in row["detail"]
    assert builds(conn) and owed(conn) is None and hold(path) is None
    assert "carry_failed" not in notices(conn)
    assert [(e["canonical_name"], e["carried_waiting"]) for e in BlackholeStore(conn).list()] == [("Sam", True)]
    assert runner.read_baseline(conn) == RELEASE


def test_where_removing_one_entry_is_not_enough_nothing_is_promised(conn):  # noqa: F811
    """A saved name that is not text is read by the boundary for every contact, reached or not. With another entry
    left the boundary still refuses, so the notice does not name an entry; with none left it builds."""
    an_upgraded_home(conn, "1.4.4")
    odd(conn, display=b"\xff\xfe")
    runner.run_pending_upgrades(conn)
    found = the_steps_row(conn)["detail"]["unreadable"]
    assert found["enough"] is False and notices(conn)["carry_failed"] == NOTICE_FAILED
    BlackholeStore(conn).unblackhole_entity(entity_ref=found["entries"][0]["blackhole_id"])
    runner.run_pending_upgrades(conn)
    assert the_steps_row(conn)["status"] == "failed" and not builds(conn)
    for entry in BlackholeStore(conn).list():
        BlackholeStore(conn).unblackhole_entity(entity_ref=entry["blackhole_id"])
    runner.run_pending_upgrades(conn)
    assert the_steps_row(conn)["status"] == "done" and builds(conn)


def test_when_the_step_ended_failed_the_hold_answers_for_a_new_bind_too(conn):  # noqa: F811
    """Rule: `owed` answers FAILED while the step's row is `failed`, whether or not a contact is still to carry.
    Here every contact was dealt with and the boundary refuses: the hold answered nothing, so a new bind was not
    refused by it."""
    an_upgraded_home(conn, "1.4.4")
    odd(conn, usernames="[123]")
    conn.commit()
    runner.run_pending_upgrades(conn)
    contact_excludes.forget_hold()
    assert contact_excludes.uncarried(conn) == 0                          # nobody is left to carry
    assert owed(conn) == FAILED and hold(path_of(conn)) == FAILED


def test_the_diagnosis_reads_and_decides_nothing_where_the_boundary_builds(conn):  # noqa: F811
    """Its answer over a list the boundary builds over: no entry, and nothing is written by asking."""
    from topos.features.lifecycle import carry_diagnosis

    an_upgraded_home(conn, "1.4.4")
    runner.run_pending_upgrades(conn)
    before = conn.total_changes
    found = carry_diagnosis.unreadable(conn)
    assert found["entries"] == [] and found["enough"] is False and conn.total_changes == before
    assert carry_diagnosis.builds(conn) and carry_diagnosis.builds(conn, only="bh_000000000000")
