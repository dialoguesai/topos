"""W4 engine handlers: pack registry list/toggle + conflicts queue."""
import asyncio
import json
import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers.derivation import (
    handle_get_derivation_packs, handle_put_derivation_pack,
    handle_get_fact_conflicts, handle_put_fact_conflict)
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(tmp_path / "h.db")
    apply_all_migrations(c)
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
              " aliases_json, is_self) VALUES ('ent_o','person','O','o','[]',1)")
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    return c


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_get_packs_seeds_and_lists(conn):
    out = _run(handle_get_derivation_packs({"id": "1"}))
    assert out["status"] == "ok"
    packs = {p["pack_id"]: p for p in out["payload"]["packs"]}
    assert len(packs) == 25
    assert packs["work.career"]["enabled"] is True          # Wave A
    assert packs["beliefs.civic"]["enabled"] is False       # opt-in, gated
    assert packs["work.career"]["predicates"] > 5


def test_toggle_pack(conn):
    _run(handle_get_derivation_packs({"id": "1"}))          # seed
    out = _run(handle_put_derivation_pack({"id": "2", "payload":
                                           {"pack_id": "interests.taste", "enabled": True}}))
    assert out["status"] == "ok"
    assert conn.execute("SELECT enabled FROM pack_registry WHERE pack_id='interests.taste'").fetchone()[0] == 1


def test_conflicts_roundtrip(conn):
    conn.execute("INSERT INTO fact_conflicts (conflict_id, subject_entity_id, predicate,"
                 " incumbent_object_id, challenger_value, challenger_confidence)"
                 " VALUES ('cfl_1','ent_o','rel.relationship','quarantine:about_unclear',"
                 " ?, 0.9)", (json.dumps({"person": "Luc", "role": "partner"}),))
    conn.commit()
    out = _run(handle_get_fact_conflicts({"id": "3"}))
    rows = out["payload"]["conflicts"]
    assert rows and rows[0]["kind"] == "quarantine" and rows[0]["reason"] == "about_unclear"
    out2 = _run(handle_put_fact_conflict({"id": "4", "payload":
                                          {"conflict_id": "cfl_1", "status": "dismissed"}}))
    assert out2["status"] == "ok"
    assert _run(handle_get_fact_conflicts({"id": "5"}))["payload"]["conflicts"] == []
