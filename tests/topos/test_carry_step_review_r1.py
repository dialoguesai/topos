"""Review R1 (node): what the carry step writes, whom it skips, and what a second run does.

protects (each finding of REVIEW_R1_NODE the step answers, with the reviewer's probe turned into a test):
  - R-M5: a contact's handles, usernames and id are written as identifiers, so the boundary reads no name part from
    them ("contact", "default", the words of an address);
  - R-M6: the owner's own contact card is never carried (it would withhold every message the owner wrote), and is
    counted;
  - R-H2: a contact saved under an emoji is named by the next usable thing, and one contact that cannot be carried
    does not stop the others: it is counted, the rest are carried, and the runner's entry fails the step so that the
    next start tries again;
  - R-L5: a second run never puts back an entry the owner removed, and never alters the stored older choice;
  - R-L4: an entry the owner had already made gains the contact's names and identifiers and nothing else; if its
    clean-up had finished it waits again, with a notice; no derived text is touched;
  - the notice: who was carried, by the name the owner saved, that they are withheld now, and what to do.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle import contact_excludes
from topos.features.lifecycle.blackhole import OWNER, BlackholeStore, start_waiting_clean_up
from topos.features.lifecycle.blackhole_rebuild import rebuild_for_blackhole
from topos.features.lifecycle.contact_excludes import (CARRIES_TABLE, NOTICE, NOTICE_ONE, CarryIncomplete,
                                                         carry_contact_excludes, dispatch)
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

EXCLUDE = {"name_visibility": "normal", "row_visibility": "exclude_from_grants"}
DATASET = "3f9c2e71-6b0d-4a58-9e13-7c5d2b8e4f60:default"       # made up; the shape a node's dataset id has
PHONE = "+1 555 " + "0142 " + "0137"                            # with separators: no ten-digit run in this file
HEART = "❤️"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "canonical.db"), check_same_thread=False)
    apply_all_migrations(c)
    c.execute(TOMBSTONES_SQL)
    ConversationsTablesManager(c).ensure_tables()
    yield c
    c.close()


def cid(tail: str) -> str:
    return f"{DATASET}:contact:{tail}"


def contact(c, contact_id, display, policy=EXCLUDE, *, is_self=0, usernames=None, handles=()):
    c.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self, known_usernames_json) "
              "VALUES (?,?,?,?,?,?)", (contact_id, DATASET, "src", display, is_self,
                                      json.dumps(usernames) if usernames else None))
    for identifier, kind in handles:
        c.execute("INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id) "
                  "VALUES (?,?,?,?,?)", (DATASET, "src", identifier, kind, contact_id))
    c.commit()
    if policy is not None:
        ConversationsTablesManager(c).update_contact_sharing_policy(dataset_id=DATASET, contact_id=contact_id,
                                                                    sharing_policy=policy)


def entity(c, entity_id, name, contact_id, aliases=(), *, is_self=0):
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
              "identifiers_json, contact_id, mention_count, metadata_json, is_self) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (entity_id, "person", name, name.lower(), json.dumps(list(aliases)), "[]", contact_id, 3, "{}", is_self))
    c.commit()


def names(c):
    return sorted(row[0] for row in c.execute("SELECT canonical_name FROM entity_blackholes"))


def journal(boundary, text):
    return boundary.observe(table="journal_entries", record_id="e1", source_id="s", dataset_id=None,
                            row={"entry_id": "e1", "content": text})[0]


# ------------------------------------------------------------------------------------------------------ R-M5

def test_handles_usernames_and_the_id_are_written_as_identifiers_and_give_no_name_part(conn):
    """The reviewer's probe `test_every_carried_contact_makes_the_words_of_its_id_names`, with the fix's outcome.
    Rule: `_entry` gives the store the handles, usernames and id as `identifiers`, not as names."""
    contact(conn, cid("7471fce8530d7bd0"), "Quorra Vellaby", usernames=["brisavt"],
            handles=[(PHONE, "phone"), ("q.vellaby@mail.example", "email")])
    assert carry_contact_excludes(conn)["carried"] == 1
    record = BlackholeStore(conn).get("Quorra Vellaby")
    listed = set(record["identifier_aliases"])
    assert listed == {"brisavt", PHONE, "q.vellaby@mail.example",
                      "3f9c2e71-6b0d-4a58-9e13-7c5d2b8e4f60 default contact 7471fce8530d7bd0"}
    assert listed < set(record["aliases"])                              # still aliases, for every older reader
    boundary = EntityBoundary(conn)
    assert boundary.name_parts == {"quorra", "vellaby"}
    assert cid("7471fce8530d7bd0") in boundary.contacts                 # the id still reaches the contact
    for text in ("Lost a contact lens at the pool again.", "Dark mode is the default on the new phone.",
                 "The mail was late; an example follows."):
        assert not journal(boundary, text), text
    assert not boundary.mentions_protected("Contact the landlord about the boiler.")
    assert not boundary.mentions_protected("keep in contact with the landlord")
    assert journal(boundary, "Tea with Quorra after work.")             # the name, as before
    assert journal(boundary, "Wrote to q.vellaby@mail.example.")        # the address, as itself


# ------------------------------------------------------------------------------------------------------ R-M6

def test_the_owners_own_card_is_skipped_and_counted(conn):
    """The reviewer's probe `test_the_owners_own_contact_card_is_carried`. Rule: a contact with `is_self` is never
    carried. Remove the skip and the owner's own entity is Off-limits and their contact id protected."""
    contact(conn, cid("self"), "Tavisha Orrumbel", is_self=1)
    entity(conn, "ent-self", "Tavisha Orrumbel", cid("self"), aliases=["me"], is_self=1)
    contact(conn, cid("0a"), "Quorra Vellaby")
    out = carry_contact_excludes(conn)
    assert out["own_card_skipped"] == 1 and out["carried"] == 1 and out["failed"] == 0
    assert out["counts"]["explicit_excludes"] == 2                      # the stored choice is still counted as one
    assert names(conn) == ["Quorra Vellaby"]
    boundary = EntityBoundary(conn)
    assert cid("self") not in boundary.contacts and "ent-self" not in boundary.ids
    assert carry_contact_excludes(conn, dry_run=True)["own_card_skipped"] == 1


# ------------------------------------------------------------------------------------------------------ R-H2

def test_a_contact_saved_under_an_emoji_does_not_stop_the_step(conn):
    """The reviewer's probe `test_one_contact_whose_saved_name_has_no_letter_stops_the_whole_step`: four explicit
    excludes, the second saved as a heart with no handle. All four are carried; that one is named by its id."""
    contact(conn, cid("0a"), "Quorra Vellaby")
    contact(conn, cid("0b"), HEART)
    contact(conn, cid("0c"), "Brisa Vantongeren")
    contact(conn, cid("0d"), "Perrin Ashgrove")
    out = carry_contact_excludes(conn)
    assert out["carried"] == 4 and out["failed"] == 0
    assert out["named_by"] == {"name": 3, "contact_id_only": 1}
    assert names(conn) == sorted(["Quorra Vellaby", "Brisa Vantongeren", "Perrin Ashgrove", cid("0b")])
    boundary = EntityBoundary(conn)                                     # and the boundary can still be read
    assert {cid("0a"), cid("0b"), cid("0c"), cid("0d")} <= boundary.contacts
    assert "contact" not in boundary.name_parts and "default" not in boundary.name_parts


def test_a_nameless_contact_is_named_by_its_first_usable_handle(conn):
    """A handle that normalises to nothing (here a bare possessive) is passed over like an emoji name."""
    contact(conn, cid("0b"), "\U0001F338", handles=[("'s", "username"), (PHONE, "phone")])
    contact(conn, cid("0c"), "  -  ")
    out = carry_contact_excludes(conn)
    assert out["named_by"] == {"handle": 1, "contact_id_only": 1} and out["failed"] == 0
    assert names(conn) == sorted([PHONE, cid("0c")])
    assert {cid("0b"), cid("0c")} <= EntityBoundary(conn).contacts      # and the boundary can still be read


def test_one_contact_that_fails_does_not_stop_the_others_and_the_runner_tries_again(conn, monkeypatch):
    """Rule: `carry_contact_excludes` catches per contact and `dispatch` raises afterwards. Remove the catch and the
    contacts after the failing one are never carried (the review's run: three of four excludes lost)."""
    for tail, display in (("0a", "Quorra Vellaby"), ("0b", "Brisa Vantongeren"), ("0c", "Perrin Ashgrove")):
        contact(conn, cid(tail), display)
    real, broken = contact_excludes._carry_one, {"on": True}

    def carry_one(c, store, contact_id, entry):
        if broken["on"] and contact_id == cid("0b"):
            raise sqlite3.OperationalError("database is locked")
        return real(c, store, contact_id, entry)

    monkeypatch.setattr(contact_excludes, "_carry_one", carry_one)
    with pytest.raises(CarryIncomplete) as failed:
        dispatch(conn, {})
    conn.commit()
    assert names(conn) == ["Perrin Ashgrove", "Quorra Vellaby"]         # the ones after it were carried
    message = str(failed.value)
    assert message.startswith("1 of 3 explicit excludes could not be carried (carried 2,")
    assert "Brisa" not in message and "Vantongeren" not in message      # counts only, never a name
    broken["on"] = False
    out = dispatch(conn, {})                                            # the next start
    assert (out["carried"], out["carried_before"], out["failed"]) == (1, 2, 0)
    assert names(conn) == ["Brisa Vantongeren", "Perrin Ashgrove", "Quorra Vellaby"]


def test_what_the_runners_ledger_row_records(conn, monkeypatch):
    """Through the upgrade runner itself: a run in which a contact failed is ledgered `failed`, its error the counts,
    and the step runs again at the next start; a clean run is `done` and its row keeps the step's counts."""
    from topos.upgrades import runner

    contact(conn, cid("0a"), "Quorra Vellaby")
    contact(conn, cid("0b"), "Brisa Vantongeren")
    step = {"id": contact_excludes.STEP_ID, "kind": "engine_endpoint",
            "params": {"method": "POST", "path": contact_excludes.ENDPOINT}}
    monkeypatch.setenv("TOPOS_UPGRADE_RUNNER", "on")
    monkeypatch.setattr(runner, "plan_upgrade",
                        lambda c, shipped=None: {"shipped": "1.5.0", "fresh_install": False, "steps": [step]})
    real, broken = contact_excludes._carry_one, {"on": True}

    def carry_one(c, store, contact_id, entry):
        if broken["on"] and contact_id == cid("0a"):
            raise RuntimeError("no")
        return real(c, store, contact_id, entry)

    monkeypatch.setattr(contact_excludes, "_carry_one", carry_one)

    def row():
        return [r for r in runner.ledger_rows(conn) if r["step_id"] == contact_excludes.STEP_ID][-1]

    first = runner.run_pending_upgrades(conn, shipped="1.5.0")
    assert (first["steps_run"], first["steps_failed"], first["baseline_advanced"]) == (0, 1, False)
    assert row()["status"] == "failed"
    assert row()["detail"]["error"].startswith("1 of 2 explicit excludes could not be carried (carried 1,")
    assert names(conn) == ["Brisa Vantongeren"]                          # the other one was carried all the same
    broken["on"] = False
    second = runner.run_pending_upgrades(conn, shipped="1.5.0")          # the next start
    assert (second["steps_run"], second["steps_failed"], second["baseline_advanced"]) == (1, 0, True)
    assert row()["status"] == "done"
    detail = row()["detail"]
    assert (detail["carried"], detail["carried_before"], detail["failed"], detail["clean_ups_waiting"]) == (1, 1, 0, 1)
    assert set(detail) == {"step", "dry_run", "counts", "carried", "already_off_limits", "added_to_existing",
                           "carried_before", "own_card_skipped", "failed", "named_by", "clean_ups_waiting", "ran_under",
                           "waiting", "boundary"}
    assert (detail["waiting"], detail["boundary"]) == (2, "built")
    assert "Quorra" not in json.dumps(detail) and "Brisa" not in json.dumps(detail)    # counts, never a name


# ------------------------------------------------------------------------------------------------------ R-L5

def test_a_second_run_never_puts_back_an_entry_the_owner_removed(conn):
    """The reviewer's run: the owner took the carried entry out between two starts and the next start put it back.
    Rule: every contact the step dealt with is remembered (CARRIES_TABLE) and skipped. Forget them and it returns."""
    contact(conn, cid("0a"), "Quorra Vellaby")
    contact(conn, cid("0b"), "Brisa Vantongeren")
    stored = conn.execute("SELECT contact_id, sharing_policy_json FROM contacts ORDER BY contact_id").fetchall()
    assert carry_contact_excludes(conn)["carried"] == 2
    BlackholeStore(conn).unblackhole_entity(entity_ref="Quorra Vellaby")
    conn.commit()
    assert names(conn) == ["Brisa Vantongeren"]
    again = carry_contact_excludes(conn)
    assert (again["carried"], again["carried_before"]) == (0, 2)
    assert names(conn) == ["Brisa Vantongeren"]                          # not put back
    assert conn.execute("SELECT contact_id, sharing_policy_json FROM contacts ORDER BY contact_id").fetchall() == stored
    remembered = conn.execute(f"SELECT contact_id, outcome FROM {CARRIES_TABLE} ORDER BY contact_id").fetchall()
    assert remembered == [(cid("0a"), "carried"), (cid("0b"), "carried")]
    assert not EntityBoundary(conn).mentions_protected("Lunch with Quorra Vellaby.")   # the owner's removal stands


def test_an_entry_of_the_owners_own_that_the_step_found_is_not_put_back_either(conn):
    contact(conn, cid("0a"), "Quorra Vellaby")
    BlackholeStore(conn).blackhole_entity(entity_ref="Quorra Vellaby", note="the owner's own")
    assert carry_contact_excludes(conn)["already_off_limits"] == 1
    BlackholeStore(conn).unblackhole_entity(entity_ref="Quorra Vellaby")
    assert carry_contact_excludes(conn)["carried_before"] == 1 and names(conn) == []


def test_a_dry_run_writes_nothing_not_even_the_memory(conn):
    contact(conn, cid("0a"), "Quorra Vellaby")
    out = carry_contact_excludes(conn, dry_run=True)
    assert out["named_by"] == {"name": 1} and out["carried"] == 0
    assert names(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name=?", (CARRIES_TABLE,)).fetchone()[0] == 0


# ------------------------------------------------------------------------------------------------------ R-L4

def test_an_entry_the_owner_already_made_gains_the_names_as_waiting_ones_with_nothing_withdrawn(conn):
    """The reviewer's probe `test_an_entry_the_owner_already_made_gets_the_aliases_but_no_rebuild`: the linked entity
    is already Off-limits, its clean-up complete, on the stricter tier, with the owner's note. The step adds the
    contact's username; a brief and a goal name only that username.

    Third fix round (ruling P; R-L4 as ruled there): the entry STAYS a full entry in the state it had. What it gains
    waits: the share boundary reads the username at once, and no reader that serves the owner himself does. Until
    then the step put the entry back to `pending`, which withheld every summary from the owner's own outside client
    for as long as he did nothing."""
    contact(conn, cid("0e"), "Brisa Vantongeren", usernames=["brisavt"])
    entity(conn, "ent-1", "Brisa Vantongeren", cid("0e"))
    conn.execute("INSERT INTO signal_dimension_briefs (brief_id, signal_dimension, head_revision_id, structured_json, "
                 "markdown_body, revision_number, updated_at, updated_by) VALUES ('b1','social','r1','{}',"
                 "'Weekend plans were settled with brisavt over chat.',1,'2026-10-01','test')")
    conn.execute("INSERT INTO user_goals (goal_id, goal_text, payload_json, created_at) VALUES ('g1',"
                 "'Send brisavt the photos from the trip','{}','2026-10-01')")
    conn.commit()
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-1", processing_tier="local_only", note="the owner's own")
    rebuild_for_blackhole(conn, "ent-1")
    conn.commit()
    assert store.get("ent-1")["rebuild_state"] == "complete"

    out = carry_contact_excludes(conn)
    conn.commit()
    assert (out["carried"], out["already_off_limits"], out["added_to_existing"], out["clean_ups_waiting"]) == (0, 1, 1, 1)
    entry = store.get("ent-1")
    assert "brisavt" in entry["aliases"] and "brisavt" in entry["identifier_aliases"]
    assert (entry["rebuild_state"], entry["processing_tier"], entry["note"]) == ("complete", "local_only", "the owner's own")
    assert not entry["carried_waiting"] and "brisavt" in entry["carried_waiting_aliases"]
    # for the owner's own tools nothing changed: the entry is the one he made, with the names it had
    mine = store.get("ent-1", view=OWNER)
    assert "brisavt" not in mine["aliases"] and "brisavt" not in store.blackholed_name_terms(view=OWNER)
    assert store.pending_rebuild_names(view=OWNER) == set()
    # nothing was cleaned up unattended: the text is still there, and withheld at the boundary
    assert conn.execute("SELECT markdown_body FROM signal_dimension_briefs").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM user_goals").fetchone()[0] == 1
    assert EntityBoundary(conn).mentions_protected("Send brisavt the photos")
    opened = [(row[0], row[1]) for row in conn.execute(
        "SELECT kind, message FROM blackhole_notifications WHERE state='open' AND kind != 'rebuild_complete'")]
    assert opened == [("carried_over", NOTICE_ONE)]
    # a clean-up nobody asked for looks for the names the entry had, never the waiting one
    rebuild_for_blackhole(conn, "ent-1")
    conn.commit()
    assert conn.execute("SELECT markdown_body FROM signal_dimension_briefs").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM user_goals").fetchone()[0] == 1
    # the owner acts (his mark, as both doors make it): now the text naming only the username goes
    start_waiting_clean_up(store, "ent-1", processing_tier="secure", note=None)
    entry = store.get("ent-1")
    assert (entry["rebuild_state"], entry["processing_tier"], entry["carried_waiting_aliases"]) == ("pending", "local_only", [])
    rebuild_for_blackhole(conn, "ent-1")
    conn.commit()
    assert conn.execute("SELECT markdown_body FROM signal_dimension_briefs").fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM user_goals").fetchone()[0] == 0
    assert "brisavt" in store.blackholed_name_terms(view=OWNER)


def test_an_existing_entry_that_gains_nothing_is_left_exactly_as_it_was(conn):
    contact(conn, cid("0a"), "Quorra Vellaby")
    carry_contact_excludes(conn)
    conn.execute(f"DELETE FROM {CARRIES_TABLE}")                         # as if the memory were lost
    conn.commit()
    before = conn.execute("SELECT * FROM entity_blackholes").fetchall()
    notes = conn.execute("SELECT COUNT(*) FROM blackhole_notifications").fetchone()[0]
    out = carry_contact_excludes(conn)
    assert (out["carried"], out["already_off_limits"], out["added_to_existing"]) == (0, 1, 0)
    assert conn.execute("SELECT * FROM entity_blackholes").fetchall() == before
    assert conn.execute("SELECT COUNT(*) FROM blackhole_notifications").fetchone()[0] == notes


# ------------------------------------------------------------------------------------------------ the notice

def test_the_owner_is_told_once_and_each_entry_is_shown_by_the_name_they_saved(conn):
    """ONE open notice for the step (third fix round, R2-H3; it was one per carried contact, each naming a control the
    app does not have). It says how many, that they are never shared, that nothing else changed, and names only the
    two controls the app has for every entry. Each entry is listed under the name the owner saved the contact by,
    also where the entry is named by the linked entity or, for a contact saved under an emoji, by its id; never
    under a contact id."""
    from topos.features.lifecycle.off_limits_list import listing

    contact(conn, cid("0a"), "Bree V.")
    entity(conn, "ent-1", "Brisa Vantongeren", cid("0a"))
    contact(conn, cid("0b"), HEART)
    contact(conn, cid("0c"), None, handles=[(PHONE, "phone")])
    contact(conn, cid("0d"), None)
    out = carry_contact_excludes(conn)
    assert (out["carried"], out["waiting"]) == (4, 4)
    rows = conn.execute("SELECT kind, state, blackhole_id, message FROM blackhole_notifications").fetchall()
    assert rows == [("carried_over", "open", "carry-contact-excludes-to-off-limits", NOTICE.format(count=4))]
    # The words as ruled in the fourth round: a routine is the one thing of the owner's own that the step changes,
    # so "Nothing else changed" comes after it.
    assert NOTICE == (
        "{count} people you had excluded from sharing in an earlier version of Topos are now never shared, and "
        "your routines leave out anything that names them. Nothing else changed. In Settings, under Off-limits, "
        "you can make any of them fully Off-limits or remove them.")
    assert NOTICE_ONE == (
        "1 person you had excluded from sharing in an earlier version of Topos is now never shared, and your "
        "routines leave out anything that names them. Nothing else changed. In Settings, under Off-limits, "
        "you can make them fully Off-limits or remove them.")
    shown = {row["canonical_name"]: row for row in listing(conn)["blackholes"]}
    assert shown["Brisa Vantongeren"]["display_label"] == "Bree V."
    assert shown[cid("0b")]["display_label"] == HEART
    assert shown[PHONE]["display_label"] == PHONE
    assert shown[cid("0d")]["display_label"] == "A contact with no saved name"
    assert all(row["carried_waiting"] and row["clean_up"]["state"] == "not_started" for row in shown.values())
    assert not any(":contact:" in row["display_label"] or "contact 0" in " ".join(row["names"] + row["identifiers"])
                   for row in shown.values())
    assert {row[0] for row in conn.execute("SELECT rebuild_state FROM entity_blackholes")} == {"pending"}


def test_a_second_run_does_not_raise_the_notice_again_once_the_owner_has_dismissed_it(conn):
    contact(conn, cid("0a"), "Quorra Vellaby")
    carry_contact_excludes(conn)
    store = BlackholeStore(conn)
    (notice,) = store.notifications(state="open")
    assert (notice["kind"], notice["message"]) == ("carried_over", NOTICE_ONE)
    assert store.dismiss_notification(notice["notification_id"])
    carry_contact_excludes(conn)                                         # the next start: everyone carried before
    assert store.notifications(state="open") == []


def test_the_notice_goes_by_itself_when_nobody_waits_any_more(conn):
    """The owner acts on one and removes the other: the step's notice is resolved by the node."""
    contact(conn, cid("0a"), "Quorra Vellaby")
    contact(conn, cid("0b"), "Brisa Vantongeren")
    carry_contact_excludes(conn)
    store = BlackholeStore(conn)
    start_waiting_clean_up(store, "Quorra Vellaby", processing_tier="secure", note=None)
    assert [n["kind"] for n in store.notifications(state="open")].count("carried_over") == 1    # one still waits
    store.unblackhole_entity(entity_ref="Brisa Vantongeren")
    assert "carried_over" not in [n["kind"] for n in store.notifications(state="open")]
