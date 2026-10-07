"""Third fix round, review R2-H2, the step's half: the upgrade step is not done until the share boundary can be built
over what it wrote, and when it cannot, the step says so by name where the owner sees it.

protects: every share read builds the boundary over the Off-limits list first, so a list the boundary cannot read
turns every share on the node off. The step writes to that list unasked. Before this change it could leave the node
in that state and report `carried 1, failed 0`: the re-check did it with one contact handle that holds no letter or
digit. That handle is now passed over by the boundary (tests/permissions_v2/test_entity_boundary_keyless_handle.py);
this file holds that the step checks, for whatever other fault a real home may have:
  - after a run that found explicit excludes the step builds the boundary once and records the outcome;
  - a boundary that refuses fails the step under its own name (`BoundaryUnavailable`), with the boundary's code,
    through the runner's ledger; the owner gets one notice; the next start tries again and a run that succeeds
    takes the notice away;
  - a node with nothing to carry is not failed for a fault the step did not touch.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import pytest

from tests.topos.test_carry_step_review_r1 import cid, conn, contact, entity, names  # noqa: F401 (conn: fixture)
from topos.features.lifecycle import contact_excludes
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.contact_excludes import (NOTICE_FAILED, BoundaryUnavailable, CarryIncomplete,
                                                         carry_contact_excludes, dispatch)
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary

pytestmark = pytest.mark.public

KEYLESS = ["._.", "__", "—", "+", "\U0001F338", "   "]


@pytest.mark.parametrize("handle", KEYLESS, ids=["dots_and_underscore", "underscores", "a_dash", "a_plus", "an_emoji",
                                                 "spaces"])
def test_the_reviewers_six_handles_no_longer_turn_every_share_off(conn, handle):
    """The re-check's run (`test_point_6_a_contact_handle_with_no_letter_or_digit`), with the outcome it asked for."""
    contact(conn, cid("0a"), "Quorra Vellaby", handles=[("quorra@fernmail.example", "email"), (handle, "username")])
    contact(conn, cid("0b"), "Perrin Ashgrove", policy=None)
    EntityBoundary(conn)                                                  # builds before the step (nothing is listed)
    out = dispatch(conn, {})
    conn.commit()
    assert (out["carried"], out["failed"], out["boundary"]) == (1, 0, "built")
    after = EntityBoundary(conn)                                          # and after it: shares can be read
    assert after.active and cid("0a") in after.contacts
    assert [n["kind"] for n in BlackholeStore(conn).notifications(state="open")] == ["carried_over"]


def a_fault_the_boundary_still_refuses(c):
    """An entity linked to the excluded contact whose own identifier list holds a value with no letter or digit:
    a lineage fault of the entity spine, older than this step, that the boundary refuses (and must)."""
    contact(c, cid("0a"), "Quorra Vellaby")
    entity(c, "ent-1", "Quorra Vellaby", cid("0a"))
    c.execute("UPDATE entities SET identifiers_json=? WHERE entity_id='ent-1'", ('["._."]',))
    c.commit()


def test_a_boundary_that_cannot_be_built_fails_the_step_by_name_and_tells_the_owner(conn):
    """Rule: `dispatch` raises `BoundaryUnavailable` when the boundary refuses after the run. Return the counts
    instead and the step is ledgered done on a node where every share refuses, with nothing saying so."""
    a_fault_the_boundary_still_refuses(conn)
    out = carry_contact_excludes(conn)
    assert (out["carried"], out["failed"], out["boundary"]) == (1, 0, "entity_protection_lineage_unavailable")
    with pytest.raises(PolicyError):
        EntityBoundary(conn)
    failures = [n for n in BlackholeStore(conn).notifications(state="open") if n["kind"] == "carry_failed"]
    # The fifth round (R3-M3): the boundary builds without this one entry, so the notice names it, by the label
    # the app shows, and says what the owner can do. The general words are for a failure no one entry explains.
    assert [n["message"] for n in failures] == [contact_excludes.NOTICE_FAILED_ENTRY.format(who="Quorra Vellaby")]
    assert out["unreadable"]["kinds"] == ["entity_identifiers"] and out["unreadable"]["enough"] is True
    assert contact_excludes.NOTICE_FAILED_ENTRY == (
        "Topos could not finish carrying over the people you had excluded from sharing in an earlier version: it "
        "cannot read what it has saved about {who}. Nothing of yours is shared until that is put right. In "
        "Settings, under Off-limits, you can remove {who}; they are then no longer excluded, and your sharing comes "
        "back the next time Topos starts.")
    assert NOTICE_FAILED == ("Topos could not finish carrying over the people you had excluded from sharing in an "
                             "earlier version. Nothing of yours is shared until it has. It tries again each time "
                             "Topos starts.")
    with pytest.raises(BoundaryUnavailable) as failed:
        dispatch(conn, {})                                                # the next start: the same fault, the same name
    assert isinstance(failed.value, CarryIncomplete)
    assert "entity_protection_lineage_unavailable" in str(failed.value) and "Quorra" not in str(failed.value)
    assert len([n for n in BlackholeStore(conn).notifications(state="open") if n["kind"] == "carry_failed"]) == 1


def test_the_runner_ledgers_it_failed_and_the_run_that_succeeds_takes_the_notice_away(conn, monkeypatch):
    from topos.upgrades import runner

    a_fault_the_boundary_still_refuses(conn)
    step = {"id": contact_excludes.STEP_ID, "kind": "engine_endpoint",
            "params": {"method": "POST", "path": contact_excludes.ENDPOINT}}
    monkeypatch.setenv("TOPOS_UPGRADE_RUNNER", "on")
    monkeypatch.setattr(runner, "plan_upgrade",
                        lambda c, shipped=None: {"shipped": "1.5.0", "fresh_install": False, "steps": [step]})

    def row():
        return [r for r in runner.ledger_rows(conn) if r["step_id"] == contact_excludes.STEP_ID][-1]

    first = runner.run_pending_upgrades(conn, shipped="1.5.0")
    assert (first["steps_run"], first["steps_failed"], first["baseline_advanced"]) == (0, 1, False)
    assert row()["status"] == "failed"
    assert row()["detail"]["error"].startswith("the Off-limits boundary cannot be built after the carry "
                                               "(entity_protection_lineage_unavailable)")
    assert names(conn) == ["Quorra Vellaby"]                              # the person was carried all the same
    conn.execute("UPDATE entities SET identifiers_json='[]' WHERE entity_id='ent-1'")     # the fault is repaired
    conn.commit()
    second = runner.run_pending_upgrades(conn, shipped="1.5.0")           # the next start
    assert (second["steps_run"], second["steps_failed"], second["baseline_advanced"]) == (1, 0, True)
    assert (row()["status"], row()["detail"]["boundary"], row()["detail"]["carried_before"]) == ("done", "built", 1)
    assert [n["kind"] for n in BlackholeStore(conn).notifications(state="open")] == ["carried_over"]


def test_a_node_with_nothing_to_carry_is_not_failed_for_a_fault_the_step_did_not_touch(conn):
    """No explicit exclude: the step writes nothing, builds nothing and raises no notice, whatever state the entity
    spine is in. (Such a fault refuses that node's shares today, as it did before the upgrade.)"""
    contact(conn, cid("0a"), "Quorra Vellaby", policy=None)
    entity(conn, "ent-1", "Quorra Vellaby", cid("0a"))
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-1")
    conn.execute("UPDATE entities SET identifiers_json=? WHERE entity_id='ent-1'", ('["._."]',))
    conn.commit()
    with pytest.raises(PolicyError):
        EntityBoundary(conn)
    before = conn.execute("SELECT COUNT(*) FROM blackhole_notifications").fetchone()[0]
    out = dispatch(conn, {})
    assert (out["carried"], out["boundary"], out["counts"]["explicit_excludes"]) == (0, "not_built", 0)
    assert conn.execute("SELECT COUNT(*) FROM blackhole_notifications").fetchone()[0] == before


# ------------------------------------------------- a node that never turned sharing on (found in the fifth round)

@pytest.fixture()
def never_shared(tmp_path):
    """A node that has messages and contacts, never turned sharing on and never merged two entities. The table
    of entity merges is made by the first merge or by the sharing clock's install, so this node has none."""
    import sqlite3

    from topos.storage.canonical import ConversationsTablesManager
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "canonical.db"), check_same_thread=False)
    apply_all_migrations(c)
    ConversationsTablesManager(c).ensure_tables()
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='entity_merge_tombstones'").fetchone()[0] == 0
    yield c
    c.close()


def test_a_node_that_never_turned_sharing_on_finishes_the_step(never_shared):
    """Rule: a real run of the step makes the table of entity merges, empty, as the sharing clock and the merge
    feature each make it, before it builds the boundary. Without it the boundary refuses for the missing table,
    the step fails at every start, and (since the hold answers whenever the step ended failed) the node could never
    turn sharing on, which is the one thing that would have made the table."""
    conn = never_shared
    contact(conn, cid("0a"), "Quorra Vellaby")
    out = dispatch(conn, {})
    conn.commit()
    assert (out["carried"], out["failed"], out["boundary"]) == (1, 0, "built")
    boundary = EntityBoundary(conn)
    assert boundary.active and cid("0a") in boundary.contacts
    assert [n["kind"] for n in BlackholeStore(conn).notifications(state="open")] == ["carried_over"]
    assert conn.execute("SELECT COUNT(*) FROM entity_merge_tombstones").fetchone()[0] == 0     # made, and empty


def test_a_dry_run_and_a_node_with_nobody_excluded_make_no_table(never_shared):
    conn = never_shared
    contact(conn, cid("0a"), "Quorra Vellaby", policy=None)
    carry_contact_excludes(conn)
    contact(conn, cid("0b"), "Perrin Ashgrove")
    carry_contact_excludes(conn, dry_run=True)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='entity_merge_tombstones'").fetchone()[0] == 0


def test_the_sharing_clock_installs_over_the_table_the_step_made(never_shared):
    """Turning sharing on afterwards: the clock's own install finds the table there, as it does after a merge."""
    from pathlib import Path

    from topos.permissions_v2.protection_clock import clock_state, ensure_protection_clock

    conn = never_shared
    contact(conn, cid("0a"), "Quorra Vellaby")
    dispatch(conn, {})
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('user_id', 'owner-invented-heron')")
    conn.commit()
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    ensure_protection_clock(path, owner_id="owner-invented-heron")       # as the bind does
    assert clock_state(conn)[1] >= 0
    assert EntityBoundary(conn).active
    triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND "
                                               "tbl_name='entity_merge_tombstones'")}
    assert triggers, "the clock watches the table of merges it found there"
