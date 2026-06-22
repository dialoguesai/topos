"""Tests for typed signal object store."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.signal_object_store import SignalObjectStore
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_signal_objects_up(conn)
    return conn


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
