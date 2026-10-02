"""Tests for typed signal object store."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.facts.store import FactStore
from topos.features.signal.signal_object_store import SignalObjectStore
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_signal_objects_up(conn)
    return conn


@pytest.fixture()
def migrated(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "signal.db"))
    apply_all_migrations(conn)
    yield conn
    conn.close()


def test_migration_creates_table() -> None:
    conn = _conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_objects'"
    ).fetchone()
    assert row is not None


def test_upsert_idempotent() -> None:
    conn = _conn()
    store = SignalObjectStore(conn)
    obj = store.upsert_object(
        "time",
        "AvailabilityWindow",
        "mar16-morning",
        {"start": "2026-03-16T11:00:00Z", "end": "2026-03-16T13:00:00Z", "availability_kind": "free"},
        source_refs=[{"table": "calendar_events", "id": "evt-1"}],
        confidence=0.9,
    )
    again = store.upsert_object(
        "time",
        "AvailabilityWindow",
        "mar16-morning",
        {"start": "2026-03-16T11:00:00Z", "end": "2026-03-16T13:00:00Z", "availability_kind": "free"},
        source_refs=[{"table": "calendar_events", "id": "evt-1"}],
        confidence=0.9,
    )
    assert obj["object_id"] == again["object_id"]
    items = store.list_objects("time", object_type="AvailabilityWindow")
    assert len(items) == 1


def test_supersede_chain() -> None:
    conn = _conn()
    store = SignalObjectStore(conn)
    first = store.upsert_object(
        "profile",
        "SkillNode",
        "python",
        {"label": "Python", "proficiency_band": "high"},
        confidence=0.7,
    )
    second = store.supersede_object(
        first["object_id"],
        {"label": "Python", "proficiency_band": "expert"},
        confidence=0.95,
    )
    assert second["object_id"] != first["object_id"]
    archived = store.get_object(first["object_id"])
    assert archived["valid_to"] is not None
    active = store.list_objects("profile")
    assert len(active) == 1
    assert active[0]["payload"]["proficiency_band"] == "expert"


def test_owner_override_supersedes_system_object() -> None:
    conn = _conn()
    store = SignalObjectStore(conn)
    created = store.upsert_object(
        "intentions",
        "Goal",
        "edtech-collab",
        {"goal_text": "Seek edtech intros", "horizon": "quarter"},
        confidence=0.6,
    )
    overridden = store.owner_override(
        created["object_id"],
        {"goal_text": "Seek edtech intros (owner clarified)"},
    )
    assert overridden["created_by"] == "owner"
    assert overridden["payload"]["_meta"]["explicitness"] == "user_authored"


def test_refused_owner_override_leaves_the_fact_current(migrated) -> None:
    # FactStore writes object_type 'fact', which no dimension declares, so
    # owner_override refuses it. The refusal used to come after the close had
    # run, and the next commit on the connection (anyone's) kept the close.
    conn = migrated
    fact = FactStore(conn).assert_fact(
        subject_entity_id="ent_self", predicate="works_at", object_value="Lumon Industries", confidence=0.9
    )
    before = list(conn.iterdump())

    with pytest.raises(ValueError, match="'fact' not declared for dimension 'profile'"):
        SignalObjectStore(conn).owner_override(fact["object_id"], {"object_value": "Lumon (owner)"})

    assert not conn.in_transaction  # no close left pending for the next commit
    conn.commit()  # what the next writer's commit on this connection persists
    assert list(conn.iterdump()) == before
    current = FactStore(conn).facts_for_subject("ent_self")
    assert [f["object_id"] for f in current] == [fact["object_id"]]


def test_supersede_rolls_back_the_close_when_the_successor_insert_fails(migrated) -> None:
    conn = migrated
    store = SignalObjectStore(conn)
    created = store.upsert_object("profile", "SkillNode", "python", {"label": "Python"}, confidence=0.7)
    conn.execute(
        "CREATE TEMP TRIGGER refuse_successor BEFORE INSERT ON signal_objects"
        " BEGIN SELECT RAISE(ABORT, 'successor refused'); END"
    )
    before = list(conn.iterdump())

    with pytest.raises(sqlite3.IntegrityError, match="successor refused"):
        store.supersede_object(created["object_id"], {"label": "Python", "proficiency_band": "expert"})

    assert not conn.in_transaction
    conn.commit()
    assert list(conn.iterdump()) == before
    assert store.get_object(created["object_id"])["valid_to"] is None


def test_unknown_dimension_rejected() -> None:
    conn = _conn()
    store = SignalObjectStore(conn)
    with pytest.raises(ValueError):
        store.upsert_object("bogus", "Goal", "k1", {})


def test_undeclared_object_type_rejected() -> None:
    conn = _conn()
    store = SignalObjectStore(conn)
    with pytest.raises(ValueError):
        store.upsert_object("time", "NotDeclaredType", "k1", {})
