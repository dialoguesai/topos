"""Third fix round, ruling P: an entry the upgrade carried is CARRIED AND WAITING until the owner acts on it.

protects:
  - what the mark is and who reads it: every reader of the store's EVERYONE view (the default) sees a carried entry
    exactly as it saw one before there were two views; the OWNER view (the owner's own tools) does not see it at all;
  - that the mark survives what the brief names: a restart, a second run of the step, a backup and its restore, and
    `add_aliases` on an entry the owner had already made (which stays a full entry: only what it gained waits);
  - that the owner's act ends it (from then every view sees an ordinary entry) and that removal ends all of it;
  - the contacts of this round's findings: ordinary short names ("al" as a username, "Ed" as a learned alias, "Sam"
    and "J" as saved names, "work" as a handle), not only invented surnames.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.topos.test_carry_step_review_r1 import PHONE, cid, conn, contact, entity  # noqa: F401 (conn: fixture)
from topos.features.lifecycle.blackhole import (EVERYONE, OWNER, WAITING_COLUMN, BlackholeStore, blackholed_entity_ids,
                                                blackholed_name_terms, has_waiting, off_limits_terms,
                                                pending_rebuild_names, start_waiting_clean_up)
from topos.features.lifecycle.blackhole_rebuild import rebuild_for_blackhole
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.permissions_v2.entity_boundary import EntityBoundary

pytestmark = pytest.mark.public

EXOTIC = "Quorra Vellaby"
#: One excluded contact per finding of the re-check (R2-H1's table), as (label, what `contact`/`entity` are given).
ORDINARY = {
    "username al": dict(display=EXOTIC, usernames=["al"], handles=[(PHONE, "phone")]),
    "alias Ed": dict(display=EXOTIC, entity=(EXOTIC, ["Ed"])),
    "saved as J": dict(display="J", handles=[(PHONE, "phone")]),
    "saved as Sam": dict(display="Sam", handles=[(PHONE, "phone")]),
    "handle work": dict(display=EXOTIC, handles=[("work", "username")]),
}


def excluded(c, spec, tail="0a"):
    contact(c, cid(tail), spec["display"], usernames=spec.get("usernames"), handles=spec.get("handles", ()))
    if spec.get("entity"):
        entity(c, f"ent-{tail}", spec["entity"][0], cid(tail), aliases=spec["entity"][1])


def owner_reads(c):
    """Everything a reader in the OWNER view can get from the store."""
    store = BlackholeStore(c)
    return {"list": store.list(view=OWNER), "ids": blackholed_entity_ids(c, view=OWNER),
            "terms": blackholed_name_terms(c, view=OWNER), "scan": set(off_limits_terms(c, view=OWNER)),
            "pending": pending_rebuild_names(c, view=OWNER), "has_pending": store.has_pending_rebuild(view=OWNER)}


NOTHING = {"list": [], "ids": set(), "terms": set(), "scan": set(), "pending": set(), "has_pending": False}


@pytest.mark.parametrize("label", list(ORDINARY))
def test_a_carried_entry_is_there_for_everyone_and_not_there_for_the_owners_own_tools(conn, label):
    """Rule: the step writes its entries carried and waiting (`blackhole_entity(carried=True)`), and the OWNER view
    leaves such an entry out. Write them as ordinary entries, or let the OWNER view read them, and the owner's own
    outside client and model calls are starved by "al", "Ed", "J", "Sam" and "work" again (review R2-H1)."""
    excluded(conn, ORDINARY[label])
    assert owner_reads(conn) == NOTHING                                   # before the step: nothing anywhere
    out = carry_contact_excludes(conn)
    conn.commit()
    assert (out["carried"], out["waiting"], out["clean_ups_waiting"]) == (1, 1, 1)
    store = BlackholeStore(conn)
    (entry,) = store.list()
    assert entry["carried_waiting"] and entry["rebuild_state"] == "pending" and has_waiting(entry)
    assert set(entry["carried_waiting_aliases"]) == {entry["normalized_name"], *entry["aliases"]}
    # everyone else: every name and identifier, at once
    assert blackholed_name_terms(conn) >= {entry["normalized_name"], *entry["aliases"]}
    assert pending_rebuild_names(conn) == {entry["normalized_name"]}
    assert store.is_blackholed(entry["normalized_name"]) and store.get(entry["blackhole_id"]) == entry
    assert EntityBoundary(conn).active
    # the owner's own tools: exactly what they read before the step
    assert owner_reads(conn) == NOTHING
    assert not store.is_blackholed(entry["normalized_name"], view=OWNER)
    assert store.get(entry["blackhole_id"], view=OWNER) is None and store.processing_tier(EXOTIC, view=OWNER) is None


def test_the_default_view_is_everyone_and_a_view_that_is_not_one_is_refused(conn):
    """A reader nobody classified keeps reading every entry; a mistyped view is never read as the narrower one."""
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    store = BlackholeStore(conn)
    assert store.list() == store.list(view=EVERYONE) and len(store.list()) == 1
    assert store.blackholed_name_terms() == store.blackholed_name_terms(view=EVERYONE) != set()
    for read in (store.list, store.blackholed_entity_ids, store.blackholed_name_terms, store.pending_rebuild_names,
                 store.terms):
        with pytest.raises(ValueError):
            read(view="owners")


def test_where_nothing_was_carried_the_owner_view_is_the_same_read(conn):
    """On a database the step never wrote to no entry carries a mark, and the OWNER view of an entry the owner
    made himself is the entry: his own entries behave on every path as they did (ruling P.4). The column itself
    is on every database since migration 81; what is asked is whether any row is marked."""
    store = BlackholeStore(conn)
    entity(conn, "ent-9", "Perrin Ashgrove", cid("09"), aliases=["Perry"])
    store.blackhole_entity(entity_ref="ent-9", processing_tier="local_only")
    assert conn.execute(f"SELECT COUNT(*) FROM entity_blackholes WHERE {WAITING_COLUMN} IS NOT NULL").fetchone()[0] == 0
    assert store._owner_view_differs(OWNER) is False
    excluded(conn, ORDINARY["username al"])
    carry_contact_excludes(conn)                                          # once anything is carried, it is asked
    assert store._owner_view_differs(OWNER) is True and store._owner_view_differs(EVERYONE) is False
    store.unblackhole_entity(entity_ref=next(e["blackhole_id"] for e in store.list() if e["carried_waiting"]))
    assert store._owner_view_differs(OWNER) is False
    assert store.list(view=OWNER) == store.list()
    assert store.blackholed_entity_ids(view=OWNER) == store.blackholed_entity_ids() == {"ent-9"}
    assert store.blackholed_name_terms(view=OWNER) == store.blackholed_name_terms() != set()
    assert store.pending_rebuild_names(view=OWNER) == store.pending_rebuild_names() == {"perrin ashgrove"}
    assert store.is_blackholed("ent-9", view=OWNER) and store.processing_tier("ent-9", view=OWNER) == "local_only"


def test_the_mark_survives_a_restart_a_second_run_and_a_backup_and_restore(conn, tmp_path):
    excluded(conn, ORDINARY["username al"])
    carry_contact_excludes(conn)
    conn.commit()
    mark = conn.execute(f"SELECT {WAITING_COLUMN} FROM entity_blackholes").fetchone()[0]
    assert json.loads(mark)["whole"] is True
    # a second run of the step (the next start): the contact is remembered, the entry untouched
    again = carry_contact_excludes(conn)
    conn.commit()
    assert (again["carried"], again["carried_before"]) == (0, 1)
    assert conn.execute(f"SELECT {WAITING_COLUMN} FROM entity_blackholes").fetchone()[0] == mark
    # a restart: another connection to the same file
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    restarted = sqlite3.connect(path)
    try:
        assert owner_reads(restarted) == NOTHING and len(BlackholeStore(restarted).list()) == 1
        # a backup, and its restore into a new home
        copy = sqlite3.connect(str(tmp_path / "restored.db"))
        try:
            restarted.backup(copy)
            (entry,) = BlackholeStore(copy).list()
            assert entry["carried_waiting"] and owner_reads(copy) == NOTHING
            assert carry_contact_excludes(copy)["carried_before"] == 1          # and the step still knows it ran
        finally:
            copy.close()
    finally:
        restarted.close()


def test_a_mark_that_cannot_be_read_marks_nothing(conn):
    """The protecting direction: an entry whose mark is damaged is a full entry for every reader."""
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    for damaged in ("{not json", "[]", '"whole"', '{"whole": "yes", "terms": "sam"}'):
        conn.execute(f"UPDATE entity_blackholes SET {WAITING_COLUMN}=?", (damaged,))
        (entry,) = BlackholeStore(conn).list(view=OWNER)
        assert not entry["carried_waiting"] and "sam" in BlackholeStore(conn).blackholed_name_terms(view=OWNER)


def test_the_owners_act_makes_it_an_ordinary_entry_for_every_view(conn):
    """Ruling P.3. Rule: the owner's mark on an entry that has anything waiting clears the mark (`make_full`). Leave
    it and a carried entry the owner made fully Off-limits still hides nothing from his outside client."""
    excluded(conn, ORDINARY["alias Ed"])
    carry_contact_excludes(conn)
    store = BlackholeStore(conn)
    (entry,) = store.list()
    started = start_waiting_clean_up(store, entry["blackhole_id"], processing_tier="secure", note=None)
    assert started is not None and started["rebuild_state"] == "pending" and not started["carried_waiting"]
    (full,) = store.list(view=OWNER)                                      # there now, for his own tools too
    assert not has_waiting(full) and full["blackhole_id"] == entry["blackhole_id"]
    assert store.blackholed_entity_ids(view=OWNER) == {"ent-0a"}
    assert {"ed", "quorra vellaby"} <= store.blackholed_name_terms(view=OWNER)
    assert store.pending_rebuild_names(view=OWNER) == {full["normalized_name"]}   # D4: his summaries are held now
    kinds = [n["kind"] for n in store.notifications(state="open")]
    assert kinds == ["rebuild_needed"]                                    # told first; the step's notice is settled
    report = rebuild_for_blackhole(conn, entry["blackhole_id"])
    assert report.details["status"] == "complete" and store.get(entry["blackhole_id"])["rebuild_state"] == "complete"
    assert EntityBoundary(conn).mentions_protected("Lunch with Ed at noon.")       # and every share, as before


def test_a_mark_by_name_or_by_entity_is_the_owners_act_too(conn):
    """The older switch sends an entity id, and an owner can type the name: each is his act on that entry."""
    excluded(conn, ORDINARY["alias Ed"])
    excluded(conn, ORDINARY["saved as Sam"], tail="0b")
    carry_contact_excludes(conn)
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-0a")                           # by the linked entity
    store.blackhole_entity(entity_ref="Sam", processing_tier="local_only")   # by the name
    assert [has_waiting(entry) for entry in store.list()] == [False, False]
    assert {entry["rebuild_state"] for entry in store.list()} == {"pending"}
    assert len(store.list(view=OWNER)) == 2


def test_removal_ends_all_of_it_and_the_step_does_not_put_it_back(conn):
    excluded(conn, ORDINARY["handle work"])
    carry_contact_excludes(conn)
    store = BlackholeStore(conn)
    (entry,) = store.list()
    removed = store.unblackhole_entity(entity_ref=entry["blackhole_id"])
    assert removed["removed"] and store.list() == [] and owner_reads(conn) == NOTHING
    assert not EntityBoundary(conn).active
    assert carry_contact_excludes(conn)["carried_before"] == 1 and store.list() == []
    open_kinds = [n["kind"] for n in store.notifications(state="open")]
    assert "carried_over" not in open_kinds                               # nobody waits: the step's notice is gone
    (told,) = [n["message"] for n in store.notifications(state="open") if n["kind"] == "reinclude_needed"]
    assert "can be shared again" in told and "rebuild" not in told       # it only ever waited: no summary changed


def test_an_entry_the_owner_made_stays_full_and_only_what_it_gains_waits(conn):
    """R-L4 as ruled: the owner's own entry keeps full behaviour for the names it had, on every path; the contact's
    further names, its identifiers and the entity the step links it to wait."""
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=EXOTIC, processing_tier="local_only", note="the owner's own")   # by name
    store.mark_rebuild_complete(EXOTIC)
    before = owner_reads(conn)
    excluded(conn, ORDINARY["alias Ed"])                                  # the same person: a linked entity, "Ed"
    out = carry_contact_excludes(conn)
    assert (out["carried"], out["added_to_existing"], out["waiting"]) == (0, 1, 1)
    (entry,) = store.list()
    assert not entry["carried_waiting"] and entry["rebuild_state"] == "complete"
    assert "ed" in entry["carried_waiting_aliases"] and entry["carried_waiting_entity_id"] == "ent-0a"
    assert (entry["processing_tier"], entry["note"]) == ("local_only", "the owner's own")
    # everyone else gains the new name and the id join at once
    assert "ed" in blackholed_name_terms(conn) and blackholed_entity_ids(conn) == {"ent-0a"}
    # the owner's own tools read the entry he made, as he made it
    after = owner_reads(conn)
    assert after["ids"] == before["ids"] == set() and after["terms"] == before["terms"]
    assert after["pending"] == set() and "ed" not in after["scan"]
    # his act: the names and the link are full
    store.blackhole_entity(entity_ref=EXOTIC, processing_tier="local_only")
    assert not has_waiting(store.get(EXOTIC))
    assert blackholed_entity_ids(conn, view=OWNER) == {"ent-0a"} and "ed" in blackholed_name_terms(conn, view=OWNER)


def test_a_clean_up_nobody_asked_for_leaves_a_carried_entry_alone(conn):
    """R2-N4, which ruling P settles: a later upgrade step that re-runs every clean-up, or anything else that calls
    the rebuild, does nothing to an entry that is carried and waiting."""
    from topos.features.lifecycle.blackhole_rebuild import rerun_all_rebuilds, run_pending_rebuilds

    excluded(conn, ORDINARY["saved as Sam"])
    conn.execute("INSERT INTO user_goals (goal_id, goal_text, payload_json, created_at) VALUES ('g1',"
                 "'Ring Sam about the keys','{}','2026-10-01')")
    carry_contact_excludes(conn)
    conn.commit()
    assert run_pending_rebuilds(conn) == [] and rerun_all_rebuilds(conn, home_chat=False) == []
    assert rebuild_for_blackhole(conn, "Sam").details["status"] == "carried_waiting"
    assert conn.execute("SELECT COUNT(*) FROM user_goals").fetchone()[0] == 1
    assert BlackholeStore(conn).get("Sam")["rebuild_state"] == "pending"
