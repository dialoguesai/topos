"""Review R1 (node), R-B1, the owner's decision: the owner starts the clean-up, per person.

protects: the carry step leaves each entry waiting, and before this change nothing could start a waiting entry's
clean-up: marking an entry Off-limits again answered "already" and ran nothing (review R1, N3: a clean-up that failed
stayed failed until the owner removed the entry and made it again). The owner's own door now does it, with no new
message type:
  - marking an entry that waits (pending, failed) Off-limits again runs its clean-up, on the relay handler
    (`signal_blackhole_entity`) and on the local route (`POST /entities/{ref}/blackhole`);
  - that changes nothing else about the entry: its tier and its note stay (the plain re-mark reset the tier to the
    default);
  - an entry whose clean-up is complete is left alone, as before;
  - an entry with no entity behind it (a carried contact with no linked entity) is started by its name.
Every person here is invented.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import topos.core.handlers as hub
import topos.core.state as state_mod
from topos.core.handlers.signal_features import handle_signal_blackhole_entity
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.storage.db import write_gate
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

NAME = "Dana Reyes"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "canonical.db"), check_same_thread=False)
    apply_all_migrations(c)
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
              "VALUES ('ent-bh', 'person', ?, ?)", (NAME, NAME.lower()))
    c.execute("INSERT INTO signal_dimension_briefs (brief_id, signal_dimension, head_revision_id, structured_json, "
              "markdown_body, revision_number, updated_at, updated_by) VALUES ('b1','social','r1','{}',"
              "'Dinner with Dana Reyes went late.',1,'2026-10-01','test')")
    c.execute("INSERT INTO signal_dimension_briefs (brief_id, signal_dimension, head_revision_id, structured_json, "
              "markdown_body, revision_number, updated_at, updated_by) VALUES ('b2','work','r1','{}',"
              "'Dinner with Perrin Ashgrove went late.',1,'2026-10-01','test')")
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    monkeypatch.setattr(state_mod, "get_db_connection", lambda: c)
    monkeypatch.setattr(state_mod, "close_thread_db_connection", lambda: None)
    write_gate.reset_loop_warning_state()
    yield c
    c.close()


def _briefs(c):
    return dict(c.execute("SELECT brief_id, markdown_body FROM signal_dimension_briefs"))


def _mark(ref, **payload):
    return handle_signal_blackhole_entity({"id": "req-1", "type": "signal_blackhole_entity",
                                           "payload": {"entity_id": ref, **payload}})


@pytest.mark.asyncio
async def test_marking_a_waiting_entry_again_runs_its_clean_up_and_changes_nothing_else(conn):
    """Rule: the handler runs the clean-up of an entry that exists and is not complete. Remove it (run only for a
    new entry, as before) and the brief that names the person stays and the entry waits for ever."""
    store = BlackholeStore(conn)
    await asyncio.to_thread(store.blackhole_entity, entity_ref="ent-bh", processing_tier="local_only", note="carried over")
    assert store.get("ent-bh")["rebuild_state"] == "pending" and _briefs(conn)["b1"]
    result = await _mark("ent-bh")
    assert result["status"] == "ok" and result["payload"]["already_blackholed"] is True
    assert result["payload"]["rebuild"]["status"] == "complete" and result["payload"]["rebuild"]["briefs_invalidated"] == 1
    entry = store.get("ent-bh")
    assert (entry["rebuild_state"], entry["processing_tier"], entry["note"]) == ("complete", "local_only", "carried over")
    assert _briefs(conn) == {"b1": "", "b2": "Dinner with Perrin Ashgrove went late."}


@pytest.mark.asyncio
async def test_a_clean_up_that_failed_is_started_again_the_same_way(conn):
    store = BlackholeStore(conn)
    await asyncio.to_thread(store.blackhole_entity, entity_ref="ent-bh")
    await asyncio.to_thread(store.mark_rebuild_failed, "ent-bh", reason="disk")
    result = await _mark("ent-bh")
    assert result["payload"]["rebuild"]["status"] == "complete" and store.get("ent-bh")["rebuild_state"] == "complete"
    assert _briefs(conn)["b1"] == ""


@pytest.mark.asyncio
async def test_marking_a_finished_entry_again_runs_nothing(conn, monkeypatch):
    store = BlackholeStore(conn)
    await asyncio.to_thread(store.blackhole_entity, entity_ref="ent-bh")
    await asyncio.to_thread(store.mark_rebuild_complete, "ent-bh")

    def never(*_a, **_k):
        raise AssertionError("a finished entry's clean-up was run again")

    monkeypatch.setattr("topos.features.lifecycle.blackhole_rebuild.rebuild_for_blackhole", never)
    result = await _mark("ent-bh")
    assert result["status"] == "ok" and result["payload"]["already_blackholed"] is True
    assert "rebuild" not in result["payload"] and _briefs(conn)["b1"]


@pytest.mark.asyncio
async def test_a_re_mark_that_names_a_tier_still_changes_the_tier(conn):
    """The owner changing the tier of an entry is the older use of a re-mark; it keeps working, and on a waiting
    entry the clean-up runs as well."""
    store = BlackholeStore(conn)
    await asyncio.to_thread(store.blackhole_entity, entity_ref="ent-bh")
    result = await _mark("ent-bh", processing_tier="local_only")
    assert result["status"] == "ok" and store.get("ent-bh")["processing_tier"] == "local_only"
    assert store.get("ent-bh")["rebuild_state"] == "complete"


@pytest.mark.asyncio
async def test_an_entry_with_no_entity_behind_it_is_started_by_its_name(conn):
    """A carried contact with no linked entity has no entity id; the entry's name is its reference."""
    store = BlackholeStore(conn)
    await asyncio.to_thread(store.blackhole_entity, entity_ref="Perrin Ashgrove", note="carried over")
    assert store.get("Perrin Ashgrove")["entity_id"] == ""
    result = await _mark("perrin ashgrove")
    assert result["payload"]["rebuild"]["status"] == "complete"
    assert _briefs(conn) == {"b1": "Dinner with Dana Reyes went late.", "b2": ""}
    assert store.get("Perrin Ashgrove")["note"] == "carried over"


@pytest.mark.asyncio
async def test_the_local_route_starts_a_waiting_clean_up_too(conn):
    from topos.api.signal import EntityBlackholeBody, blackhole_entity

    store = BlackholeStore(conn)
    await asyncio.to_thread(store.blackhole_entity, entity_ref="ent-bh", processing_tier="local_only", note="carried over")
    result = await blackhole_entity("ent-bh", EntityBlackholeBody(), _api_key="test")
    assert result["already_blackholed"] is True and result["rebuild"]["status"] == "complete"
    entry = store.get("ent-bh")
    assert (entry["rebuild_state"], entry["processing_tier"], entry["note"]) == ("complete", "local_only", "carried over")
    assert _briefs(conn)["b1"] == ""
    # complete now: a further mark runs nothing
    again = await blackhole_entity("ent-bh", EntityBlackholeBody(), _api_key="test")
    assert again["already_blackholed"] is True and "rebuild" not in again
