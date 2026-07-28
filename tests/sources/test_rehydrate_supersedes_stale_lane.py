"""Stale runtime installs whose lane contradicts a bundled triple must retire.

Regression: a `gcal_events` runtime install predating the bundled source kept
`canonical_group_id="conversations"` while `gcal.events.v1` maps to `schedule`.
Rehydrate re-raised the mismatch on every sources-list call, so the connectors
UI polled a warning into the log forever. The bundled definition is
authoritative, so the row can never install — retire it instead of retrying.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager

import pytest

from topos.sources import install_service
from topos.sources.bundled_canonical_triples import bundled_lane_conflict

STALE_GCAL_DEFINITION = {
    "source_id": "gcal_events",
    "display_name": "Google Calendar Events",
    "source_type": "ui_stream",
    "delivery": "client_push",
    "schema_id": "gcal.events.v1",
    "parser_id": "gcal.events.v1",
    "canonical_mapper_id": "gcal.events.v1",
    "canonical_group_id": "conversations",
    "ingestion_trigger": "automatic",
    "enrichment_trigger": "manual",
    "default_scope_id": "schedule",
    "allowed_scope_ids": ["schedule:read"],
}

SCOPE = {
    "user_id": "user-a",
    "device_id": "*",
    "topos_id": "topos-a",
    "dataset_id": "user-a:topos:topos-a",
}


@pytest.fixture
def install_db(tmp_path, monkeypatch):
    """Point install_service at a throwaway sqlite file."""
    conn = sqlite3.connect(tmp_path / "installs.db")

    @contextmanager
    def _fake_db_conn():
        yield conn

    monkeypatch.setattr(install_service, "_db_conn", _fake_db_conn)
    monkeypatch.setattr(install_service.settings, "topos_database_mode", "local")
    monkeypatch.setattr(install_service, "_ACTIVE_HANDLES", {})
    install_service.ensure_install_schema()
    yield conn
    conn.close()


def _insert_active(conn, definition: dict) -> str:
    install_id = str(uuid.uuid4())
    now = install_service._utc_now_iso()
    conn.execute(
        f"""
        INSERT INTO {install_service.INSTALL_TABLE} (
            install_id, scope_key, source_id, version_id, status, is_active,
            source_definition_json, source_version_row_json, failure_reason,
            created_at, updated_at
        ) VALUES (?, ?, ?, NULL, 'active', 1, ?, NULL, NULL, ?, ?)
        """,
        (
            install_id,
            install_service._scope_key(SCOPE),
            definition["source_id"],
            json.dumps(definition),
            now,
            now,
        ),
    )
    conn.commit()
    return install_id


def _row(conn, install_id: str):
    return conn.execute(
        f"SELECT status, is_active, failure_reason FROM {install_service.INSTALL_TABLE} WHERE install_id = ?",
        (install_id,),
    ).fetchone()


def test_bundled_lane_conflict_flags_stale_gcal_lane() -> None:
    reason = bundled_lane_conflict(STALE_GCAL_DEFINITION)
    assert reason is not None
    assert "'conversations'" in reason
    assert "'schedule'" in reason


def test_bundled_lane_conflict_allows_matching_and_absent_lanes() -> None:
    assert bundled_lane_conflict({**STALE_GCAL_DEFINITION, "canonical_group_id": "schedule"}) is None
    # No declared lane: normalization fills it from the triple, nothing to conflict with.
    assert bundled_lane_conflict({**STALE_GCAL_DEFINITION, "canonical_group_id": ""}) is None
    # Unknown schema: custom sources own their lane.
    assert bundled_lane_conflict({"schema_id": "custom.thing.v1", "canonical_group_id": "conversations"}) is None


def test_rehydrate_supersedes_stale_lane_install(install_db) -> None:
    install_id = _insert_active(install_db, STALE_GCAL_DEFINITION)

    summary = install_service.rehydrate_active_installs_runtime(source_id="gcal_events")

    assert summary["superseded"] == 1
    assert summary["failed"] == 0
    status, is_active, failure_reason = _row(install_db, install_id)
    assert status == "superseded"
    assert is_active == 0
    assert "does not match bundled lane" in failure_reason


def test_rehydrate_is_quiet_on_second_pass(install_db, caplog) -> None:
    _insert_active(install_db, STALE_GCAL_DEFINITION)
    install_service.rehydrate_active_installs_runtime(source_id="gcal_events")

    caplog.clear()  # drop the one-time supersede warning from the first pass
    with caplog.at_level("WARNING", logger="topos.sources.install_service"):
        summary = install_service.rehydrate_active_installs_runtime(source_id="gcal_events")

    assert summary["superseded"] == 0
    assert summary["active_installs"] == 0
    assert caplog.records == []
