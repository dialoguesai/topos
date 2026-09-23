"""The floor outside the canonical database, and what it refuses.

The protection clock is monotone inside its own file. These tests pin what
happens when that file is replaced by an older copy of itself, and what a read
is allowed to adopt on its own.
"""
import json
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import corpus, edit
from tests.permissions_v2.test_owner_identity_binding import OWNER, add_entity, db, do_attest, generation
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.canonical_floor import (CanonicalFloor, CanonicalFloorStore, event_chain,
    ledger_state, observe, registry_state)
from topos.permissions_v2.protection_clock import EVENTS, LEDGER, REGISTRY, TABLE


def store(corpus, tmp_path, name="canonical-floor.json"):
    return CanonicalFloorStore(tmp_path / name, owner_id="owner-1", node_id="node-1", resource_id="resource-1")


def installed(corpus, tmp_path):
    floor = store(corpus, tmp_path)
    with db(corpus) as conn:
        floor.install(conn)
    return floor


def protect(corpus, record_id="message-1"):
    """A native write that advances the clock and appends an event."""
    from topos.features.lifecycle.record_protection import RecordProtectionStore
    with db(corpus) as conn:
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id=record_id)


def test_install_records_the_state_and_refuses_to_overwrite(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    with db(corpus) as conn:
        assert floor.check(conn).clock_id == floor._current.clock_id
        with pytest.raises(PolicyError, match="canonical_floor_unavailable"):
            floor.install(conn)


def test_a_read_adopts_native_growth_and_republishes(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    before = json.loads((tmp_path / "canonical-floor.json").read_text())
    protect(corpus)
    with db(corpus) as conn:
        after = floor.check(conn)
    assert after.generation > before["generation"]
    assert after.event_sequence > before["event_sequence"]
    assert after.revision == before["revision"] + 1
    # The republished file, not just the in-memory copy.
    assert json.loads((tmp_path / "canonical-floor.json").read_text())["event_chain"] == after.event_chain


def test_a_read_refuses_a_canonical_database_that_went_backwards(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    protect(corpus)
    with db(corpus) as conn:
        floor.check(conn)
    # Simulate the restore of an older copy: the clock and log move back.
    with db(corpus) as conn:
        conn.execute(f"DROP TRIGGER permissions_v2_protection_events_no_delete")
        conn.execute(f"DELETE FROM {EVENTS}")
        conn.execute(f"UPDATE {TABLE} SET generation=0 WHERE singleton=1")
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_rollback"):
        floor.check(conn)


def test_a_read_refuses_an_event_log_whose_history_was_rewritten(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    protect(corpus)
    with db(corpus) as conn:
        floor.check(conn)
    with db(corpus) as conn:
        conn.execute("DROP TRIGGER permissions_v2_protection_events_no_update")
        conn.execute(f"UPDATE {EVENTS} SET artifact_key='conversation_messages|other'")
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_rollback"):
        floor.check(conn)


def test_a_read_never_adopts_a_consent_row(corpus, tmp_path):
    """The one thing only the attestation handler may move."""
    floor = installed(corpus, tmp_path)
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    with db(corpus) as conn, pytest.raises(PolicyError, match="identity_ledger_unpinned"):
        floor.check(conn)


def test_the_attestation_publish_sequence_adopts_it_exactly_once(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    with db(corpus) as conn:
        pending = floor.publish_pending(conn)
    assert pending.state == "pending"
    # A crash between the two publishes leaves the floor closed, not ahead.
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        floor.check(conn)
    with db(corpus) as conn:
        do_attest(conn, OWNER)
    with db(corpus) as conn:
        active = floor.publish_active(conn)
    assert active.state == "active" and active.ledger_sequence == 1
    with db(corpus) as conn:
        assert floor.check(conn).ledger_digest == active.ledger_digest
        # A second consent row still needs its own publish.
        add_entity(conn, "second-self")
    with db(corpus) as conn:
        do_attest(conn, "second-self")
    with db(corpus) as conn, pytest.raises(PolicyError, match="identity_ledger_unpinned"):
        floor.check(conn)


def test_a_floor_file_that_goes_backwards_under_a_running_process_is_refused(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    old = (tmp_path / "canonical-floor.json").read_bytes()
    protect(corpus)
    with db(corpus) as conn:
        floor.check(conn)
    (tmp_path / "canonical-floor.json").write_bytes(old)
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_rollback"):
        floor.check(conn)


def test_a_floor_for_another_resource_is_not_this_nodes_floor(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    other = CanonicalFloorStore(tmp_path / "canonical-floor.json", owner_id="owner-1", node_id="node-1",
                                resource_id="resource-2")
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        other.check(conn)


def test_a_missing_or_oversized_floor_is_never_treated_as_no_floor(corpus, tmp_path):
    floor = store(corpus, tmp_path)
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        floor.check(conn)
    installed(corpus, tmp_path)
    (tmp_path / "canonical-floor.json").write_bytes(b"{" + b" " * 9000 + b"}")
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        floor.check(conn)


def test_resuming_the_fold_gives_the_same_chain_as_folding_it_whole(corpus, tmp_path):
    from topos.features.lifecycle.record_protection import RecordProtectionStore
    with db(corpus) as conn:
        for index in range(6):
            store = RecordProtectionStore(conn)
            store.protect(canonical_table="conversation_messages", record_id="message-1")
            store.unprotect(canonical_table="conversation_messages", record_id="message-1")
    with db(corpus) as conn:
        whole = event_chain(conn)
        half = event_chain(conn, through=3)
        resumed = event_chain(conn, start=half)
    assert whole == resumed and half[0] == 3


def test_the_registry_is_a_prefix_and_the_ledger_is_not(corpus, tmp_path):
    floor = installed(corpus, tmp_path)
    with db(corpus) as conn:
        before_registry, before_ledger = registry_state(conn), ledger_state(conn)
        add_entity(conn, "second-self")
    with db(corpus) as conn:
        # A new self row grows the registry through the clock's own trigger.
        assert registry_state(conn)[0] > before_registry[0]
        assert ledger_state(conn) == before_ledger
        assert floor.check(conn).registry_count == registry_state(conn)[0]


def test_a_clock_that_went_backwards_is_caught_even_with_the_log_intact(corpus, tmp_path):
    """A restore need not lose events to be a restore. The generation alone says so."""
    floor = installed(corpus, tmp_path)
    protect(corpus)
    with db(corpus) as conn:
        seen = floor.check(conn)
    with db(corpus) as conn:
        conn.execute(f"UPDATE {TABLE} SET generation=generation-1 WHERE singleton=1")
        assert conn.execute(f"SELECT count(*) FROM {EVENTS}").fetchone()[0] > 0
    with db(corpus) as conn, pytest.raises(PolicyError, match="canonical_floor_rollback"):
        floor.check(conn)
    # A registry that shrinks in place cannot be simulated: the delete guard
    # refuses it, and removing that guard breaks the clock before the floor
    # is consulted at all.
    with db(corpus) as conn:
        conn.execute(f"UPDATE {TABLE} SET generation=? WHERE singleton=1", (seen.generation,))
        conn.execute("DROP TRIGGER permissions_v2_identity_subjects_no_delete")
    with db(corpus) as conn, pytest.raises(PolicyError, match="protection_clock_unavailable"):
        floor.check(conn)
