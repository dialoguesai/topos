"""N6 (decision D24): the upgrade step that carries the older model's explicit per-person "exclude" choices into Off-limits.

protects: the older sharing model kept a per-contact choice (contacts.sharing_policy_json, the app's Include / Exclude
toggle); a grant that inherited contact defaults dropped every message in a thread with an excluded participant. The
any-to-any model has no per-person setting and removes that lane, so an explicit exclude the owner made would silently
stop protecting anyone. These tests pin, on an invented database written through the real writers:
  - which stored choices are carried (an explicit exclude) and which are not (no stored choice, an include, a hidden
    name with rows included, an object without a row choice, an unreadable value), each counted;
  - how a carried contact is named (linked entity, display name, handle, contact id) and that the Off-limits boundary
    then reaches the contact itself, so a message in a thread it takes part in is withheld, as the older exclude did;
  - idempotence (a second run writes nothing new), a dry run that writes nothing, the owner's notification, and the
    upgrade runner's dispatch and the manifest entry.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle.contact_excludes import ENDPOINT, NOTE, STEP_ID, carry_contact_excludes
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations
from topos.upgrades import load_unreleased
from topos.upgrades.runner import _exec_engine_endpoint

pytestmark = pytest.mark.public

EXCLUDE = {"name_visibility": "normal", "row_visibility": "exclude_from_grants"}
INCLUDE = {"name_visibility": "normal", "row_visibility": "normal"}
# A phone handle written with separators, so no ten-digit run stands in this file.
PHONE = "+1 555 " + "0142 " + "0137"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "canonical.db"), check_same_thread=False)
    apply_all_migrations(c)
    c.execute(TOMBSTONES_SQL)                  # the merge feature and the protection clock create it on a node
    manager = ConversationsTablesManager(c)
    manager.ensure_tables()
    contacts = [
        # contact id, display name, policy written through the older model's own writer (None: never set)
        ("contact-named", "Quorra Vellaby", EXCLUDE),
        ("contact-phone", None, EXCLUDE),
        ("contact-entity", "Bree V.", EXCLUDE),
        ("contact-bare", None, EXCLUDE),
        ("contact-include", "Tamsin Holloway-Reed", INCLUDE),
        ("contact-default", "Ottilie Brandmoor", None),
        ("contact-hidden", "Perrin Ashgrove", {"name_visibility": "hidden", "row_visibility": "normal"}),
        ("contact-norow", "Lysander Coombe", {"name_visibility": "hidden"}),
    ]
    for contact_id, display, _policy in contacts:
        c.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name) VALUES (?,?,?,?)",
                  (contact_id, "dataset-1", "messages-source", display))
    c.commit()
    for contact_id, _display, policy in contacts:
        if policy is not None:
            manager.update_contact_sharing_policy(dataset_id="dataset-1", contact_id=contact_id, sharing_policy=policy)
    c.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, sharing_policy_json) "
              "VALUES ('contact-broken', 'dataset-1', 'messages-source', 'Wilmer Fentiman', '{not json')")
    c.execute("INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id) "
              "VALUES ('dataset-1', 'messages-source', ?, 'phone', 'contact-phone')", (PHONE,))
    c.execute("INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id) "
              "VALUES ('dataset-1', 'messages-source', 'brisa.vantongeren@example.org', 'email', 'contact-entity')")
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, contact_id, "
              "mention_count) VALUES ('entity-1', 'person', 'Brisa Vantongeren', 'brisa vantongeren', '[\"Bri\"]', "
              "'contact-entity', 3)")
    c.commit()
    yield c
    c.close()


def offlimits(c):
    return {row[0]: {"entity_id": row[1], "aliases": json.loads(row[2] or "[]"), "note": row[3]}
            for row in c.execute("SELECT canonical_name, entity_id, aliases_json, note FROM entity_blackholes")}


def thread_with(c, contact_id, text):
    """A message the owner wrote in a thread `contact_id` takes part in; whether the boundary withholds it."""
    c.execute("DELETE FROM conversations")
    c.execute("DELETE FROM conversation_participants")
    c.execute("DELETE FROM conversation_messages")
    c.execute("INSERT INTO conversations (conversation_id, dataset_id, source_id) VALUES ('thread-9', 'dataset-1', "
              "'messages-source')")
    c.execute("INSERT INTO conversation_participants (conversation_id, dataset_id, source_id, contact_id, role) "
              "VALUES ('thread-9', 'dataset-1', 'messages-source', ?, 'member')", (contact_id,))
    c.execute("INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, content, "
              "event_at, source_id, is_from_self) VALUES ('message-9', 'thread-9', 'dataset-1', 'owner-handle', ?, "
              "'2026-09-30T10:00:00Z', 'messages-source', 1)", (text,))
    c.commit()
    row = dict(zip(["message_id", "conversation_id", "dataset_id", "sender_id", "content", "source_id"], c.execute(
        "SELECT message_id, conversation_id, dataset_id, sender_id, content, source_id FROM conversation_messages")
        .fetchone()))
    row["reply_to_message_id"] = None
    return EntityBoundary(c).observe(table="conversation_messages", record_id="message-9", source_id="messages-source",
                                     dataset_id="dataset-1", row=row)[0]


def test_every_explicit_exclude_and_nothing_else_is_carried(conn):
    out = carry_contact_excludes(conn)
    assert out["step"] == STEP_ID
    assert out["counts"] == {"contacts": 9, "no_stored_choice": 1, "unreadable": 1, "stored_without_row_choice": 1,
                             "explicit_excludes": 4, "explicit_includes": 2, "hidden_names": 2}
    assert out["carried"] == 4 and out["already_off_limits"] == 0
    assert out["clean_ups_waiting"] == 4 and "rebuilds_failed" not in out      # the step runs no clean-up (R-B1)
    assert {row[0] for row in conn.execute("SELECT rebuild_state FROM entity_blackholes")} == {"pending"}
    assert out["named_by"] == {"name": 1, "handle": 1, "linked_entity": 1, "contact_id_only": 1}
    entries = offlimits(conn)
    assert set(entries) == {"Quorra Vellaby", PHONE, "Brisa Vantongeren", "contact-bare"}
    assert entries["Brisa Vantongeren"]["entity_id"] == "entity-1"       # named by the linked entity it points at
    assert {"bree v.", "bri", "contact-entity"} <= set(entries["Brisa Vantongeren"]["aliases"])   # display name kept
    assert all(entry["note"] == NOTE for entry in entries.values())
    for name in ("Tamsin Holloway-Reed", "Ottilie Brandmoor", "Perrin Ashgrove", "Lysander Coombe", "Wilmer Fentiman"):
        assert name not in entries                                  # include, default, hidden name, no row choice


def test_each_carried_contact_is_protected_where_the_older_exclude_reached(conn):
    for contact_id in ("contact-named", "contact-phone", "contact-entity", "contact-bare", "contact-include"):
        assert not thread_with(conn, contact_id, "Plans for the weekend.")
    carry_contact_excludes(conn)
    boundary = EntityBoundary(conn)
    assert {"contact-named", "contact-phone", "contact-entity", "contact-bare"} <= boundary.contacts
    assert "contact-include" not in boundary.contacts
    for contact_id in ("contact-named", "contact-phone", "contact-entity", "contact-bare"):
        assert thread_with(conn, contact_id, "Plans for the weekend."), contact_id
    assert not thread_with(conn, "contact-include", "Plans for the weekend.")
    assert thread_with(conn, "contact-include", "Lunch with Quorra on Friday.")     # the name itself, anywhere


def test_a_second_run_writes_nothing_new_and_a_dry_run_writes_nothing(conn):
    preview = carry_contact_excludes(conn, dry_run=True)
    assert preview["dry_run"] and preview["counts"]["explicit_excludes"] == 4 and preview["carried"] == 0
    assert offlimits(conn) == {}
    first = carry_contact_excludes(conn)
    before = offlimits(conn)
    notifications = conn.execute("SELECT COUNT(*) FROM blackhole_notifications").fetchone()[0]
    second = carry_contact_excludes(conn)
    # Each contact the step has dealt with is remembered and skipped (review R1 node, R-L5): the second run does
    # not even look at the entries, so it cannot put back one the owner removed.
    assert first["carried"] == 4 and second["carried"] == 0 and second["carried_before"] == 4
    assert second["already_off_limits"] == 0 and second["clean_ups_waiting"] == 0
    assert offlimits(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM blackhole_notifications").fetchone()[0] == notifications


def test_the_owner_is_told_and_an_existing_entry_only_gains_aliases(conn):
    from topos.features.lifecycle.blackhole import BlackholeStore
    BlackholeStore(conn).blackhole_entity(entity_ref="Quorra Vellaby", note="the owner's own")
    out = carry_contact_excludes(conn)
    assert out["carried"] == 3 and out["already_off_limits"] == 1
    kinds = [row[0] for row in conn.execute("SELECT kind FROM blackhole_notifications")]
    assert kinds.count("rebuild_needed") == 4                     # the owner's own flag, then one per carried contact
    assert "contact-named" in offlimits(conn)["Quorra Vellaby"]["aliases"]


def test_a_node_without_the_older_choices_has_nothing_to_carry(tmp_path):
    c = sqlite3.connect(str(tmp_path / "fresh.db"))
    apply_all_migrations(c)
    out = carry_contact_excludes(c)
    assert out["carried"] == 0 and out["counts"]["explicit_excludes"] == 0


def test_the_upgrade_runner_dispatches_the_step_declared_for_the_release(conn):
    staged = {step["id"]: step for step in (load_unreleased() or {}).get("steps", [])}
    assert staged[STEP_ID]["kind"] == "engine_endpoint" and staged[STEP_ID]["params"]["path"] == ENDPOINT
    out = _exec_engine_endpoint(staged[STEP_ID], conn)
    assert out["carried"] == 4
    assert _exec_engine_endpoint(staged[STEP_ID], conn)["carried"] == 0
