"""Active install rows under the legacy dataset scope must retire once duplicated.

Regression: rows keyed under the legacy `{owner}:default:{key-hash}` dataset
scope survived the move to the canonical `{owner}:topos:{topos_id}` scope.
Readers query the canonical scope only, so the rows are dead weight — but any
reader that ever matches the legacy scope gets a PARTIAL install list, and a
partial list makes routing_supply_states mark live scopes no_source_connected.
Rehydrate now demotes an active legacy-scope row when an active canonical-scope
row exists for the same user+topos and the same source_id (or its declared
successor, e.g. gdrive_files -> drive_files, a republish under a new id).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager

import pytest

from topos.sources import install_service

USER = "user-a"
TOPOS = "topos-a"

LEGACY_SCOPE = {
    "user_id": USER,
    "device_id": "*",
    "topos_id": TOPOS,
    "dataset_id": f"{USER}:default:0123456789abcdef",
}

CANONICAL_SCOPE = {
    "user_id": USER,
    "device_id": "*",
    "topos_id": TOPOS,
    "dataset_id": f"{USER}:topos:{TOPOS}",
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


def _insert_active(conn, source_id: str, scope: dict, definition: dict | None = None) -> str:
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
            install_service._scope_key(scope),
            source_id,
            json.dumps(definition or {}),
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


def test_legacy_duplicate_of_canonical_row_is_superseded(install_db) -> None:
    legacy_id = _insert_active(install_db, "gmail_messages", LEGACY_SCOPE)
    canonical_id = _insert_active(install_db, "gmail_messages", CANONICAL_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 1
    assert summary["failed"] == 0
    status, is_active, failure_reason = _row(install_db, legacy_id)
    assert status == "superseded"
    assert is_active == 0
    assert "legacy dataset scope" in failure_reason
    assert "source_id=gmail_messages" in failure_reason
    status, is_active, _ = _row(install_db, canonical_id)
    assert (status, is_active) == ("active", 1)


def test_legacy_row_is_superseded_by_declared_successor(install_db) -> None:
    legacy_id = _insert_active(install_db, "gdrive_files", LEGACY_SCOPE)
    _insert_active(install_db, "drive_files", CANONICAL_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 1
    status, is_active, failure_reason = _row(install_db, legacy_id)
    assert status == "superseded"
    assert is_active == 0
    assert "source_id=drive_files" in failure_reason


def test_filtered_rehydrate_still_sees_successor_peer(install_db) -> None:
    # A source_id-filtered listing excludes the drive_files row; the sweep must
    # look at all active installs to find the successor.
    legacy_id = _insert_active(install_db, "gdrive_files", LEGACY_SCOPE)
    _insert_active(install_db, "drive_files", CANONICAL_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime(source_id="gdrive_files")

    assert summary["superseded"] == 1
    _, is_active, _ = _row(install_db, legacy_id)
    assert is_active == 0


def test_legacy_row_without_canonical_counterpart_stays_active(install_db) -> None:
    # No canonical-scope proof: leave the row alone rather than orphan an install.
    legacy_id = _insert_active(install_db, "gmail_messages", LEGACY_SCOPE)
    _insert_active(install_db, "notion_pages", CANONICAL_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 0
    status, is_active, failure_reason = _row(install_db, legacy_id)
    assert (status, is_active, failure_reason) == ("active", 1, None)


def test_canonical_rows_are_never_demoted_by_the_sweep(install_db) -> None:
    first = _insert_active(install_db, "gmail_messages", CANONICAL_SCOPE)
    second = _insert_active(install_db, "drive_files", CANONICAL_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 0
    for install_id in (first, second):
        status, is_active, _ = _row(install_db, install_id)
        assert (status, is_active) == ("active", 1)


def test_second_pass_is_quiet(install_db, caplog) -> None:
    _insert_active(install_db, "gmail_messages", LEGACY_SCOPE)
    _insert_active(install_db, "gmail_messages", CANONICAL_SCOPE)
    install_service.rehydrate_active_installs_runtime()

    caplog.clear()  # drop the one-time supersede warning from the first pass
    with caplog.at_level("WARNING", logger="topos.sources.install_service"):
        summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 0
    assert caplog.records == []
