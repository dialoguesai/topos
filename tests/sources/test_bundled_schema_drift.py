"""Drift variants of bundled schema ids must be seen — but never orphan a connector.

Regression: a 2026-07 operator install template invented `gdrive.files.v1`
(the bundled id is `gdrive.file.v1`, lane `documents`) and declared the
`conversations` lane. Because the guard keys BUNDLED_CANONICAL_TRIPLES on
exact ids, the lane guard never fired — the rows installed and rehydrated
silently for six weeks.

The fix is deliberately split, because the live `drive_files` install carries
this exact drift and is the node's only supply row for Google Drive:

- `bundled_schema_drift` (advisory) recognizes alias variants.
- Rehydrate retires a drifted row only when BUNDLED_REGISTRY ships a
  replacement for its source_id (the gcal precedent); otherwise it keeps the
  row and logs the drift once per boot.
- `bundled_lane_conflict` and `normalize_canonical_source_payload` ignore
  aliases, so a reinstall of the published payload keeps working and the
  strict demote path can never claim the live row.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager

import pytest

from topos.sources import install_service
from topos.sources.bundled_canonical_triples import (
    bundled_lane_conflict,
    bundled_schema_drift,
    normalize_canonical_source_payload,
)

DRIFTED_DRIVE_DEFINITION = {
    "source_id": "drive_files",
    "display_name": "Drive Files",
    "source_type": "ui_stream",
    "delivery": "client_push",
    "schema_id": "gdrive.files.v1",
    "parser_id": "gdrive.files.v1",
    "canonical_mapper_id": "gdrive.files.v1",
    "canonical_group_id": "conversations",
    "source_kind": "ingestion",
}

SCOPE = {
    "user_id": "user-a",
    "device_id": "*",
    "topos_id": "topos-a",
    "dataset_id": "user-a:topos:topos-a",
}


def test_drift_flags_plural_gdrive_schema_with_wrong_lane() -> None:
    reason = bundled_schema_drift(DRIFTED_DRIVE_DEFINITION)
    assert reason is not None
    assert "'gdrive.files.v1'" in reason
    assert "'gdrive.file.v1'" in reason
    assert "'documents'" in reason


def test_drift_is_none_when_lane_matches_bundled() -> None:
    assert bundled_schema_drift({**DRIFTED_DRIVE_DEFINITION, "canonical_group_id": "documents"}) is None


def test_drift_is_none_for_unknown_and_exact_bundled_schemas() -> None:
    # Custom sources own their lane.
    assert bundled_schema_drift({"schema_id": "custom.thing.v1", "canonical_group_id": "conversations"}) is None
    # An exact bundled id is the strict guard's job, not drift's.
    exact = {**DRIFTED_DRIVE_DEFINITION, "schema_id": "gdrive.file.v1", "parser_id": "gdrive.file.v1"}
    assert bundled_schema_drift(exact) is None
    assert bundled_lane_conflict(exact) is not None


def test_strict_guard_and_normalize_ignore_aliases() -> None:
    # These two pins protect the live drive_files install: the strict demote
    # path never claims it, and a reinstall of the published payload never raises.
    assert bundled_lane_conflict(DRIFTED_DRIVE_DEFINITION) is None
    normalized = normalize_canonical_source_payload(dict(DRIFTED_DRIVE_DEFINITION))
    assert normalized["canonical_group_id"] == "conversations"


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


def test_rehydrate_tolerates_drift_without_bundled_replacement(install_db, monkeypatch, caplog) -> None:
    # drive_files has no BUNDLED_REGISTRY entry: the row must survive, install,
    # and leave one warning naming the drift.
    install_id = _insert_active(install_db, DRIFTED_DRIVE_DEFINITION)
    installed: list = []
    monkeypatch.setattr(
        install_service, "install_source_definition", lambda d: installed.append(d) or object()
    )
    monkeypatch.setitem(install_service.PARSER_REGISTRY, "gdrive.files.v1", object())

    with caplog.at_level("WARNING", logger="topos.sources.install_service"):
        summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 0
    assert summary["rehydrated"] == 1
    assert len(installed) == 1
    status, is_active, _ = _row(install_db, install_id)
    assert (status, is_active) == ("active", 1)
    assert any("drift" in rec.message for rec in caplog.records)

    # Second pass: the handle exists, so no reinstall and no repeat warning.
    caplog.clear()
    with caplog.at_level("WARNING", logger="topos.sources.install_service"):
        summary = install_service.rehydrate_active_installs_runtime()
    assert summary["rehydrated"] == 0
    assert caplog.records == []


def test_rehydrate_supersedes_drift_with_bundled_replacement(install_db) -> None:
    # gdrive_files IS bundled, so a drifted row retires like a lane conflict.
    definition = {**DRIFTED_DRIVE_DEFINITION, "source_id": "gdrive_files"}
    install_id = _insert_active(install_db, definition)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 1
    status, is_active, failure_reason = _row(install_db, install_id)
    assert status == "superseded"
    assert is_active == 0
    assert "drift variant" in failure_reason
