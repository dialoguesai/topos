"""A canonical-scope duplicate left by an id convergence retires once its
successor is live.

Regression setup: the Drive connector's directory card converged back on the
bundled `gdrive_files` id (CP registry republish, 2026-09-02), while a node
still holds the July `drive_files` install under the same canonical scope. A
reconnect then leaves BOTH ids active — one connector, two supply rows.
`_CANONICAL_SOURCE_SUCCESSORS` declares the convergence, and rehydrate retires
the predecessor's canonical-scope row when a HEALTHY canonical-scope row of the
successor exists for the same user+topos.

The healthy requirement is load-bearing: a successor row this same pass is
retiring (lane conflict / schema drift) must never count as proof, or one pass
could unwire the connector entirely — old drifted gdrive_files retiring via the
drift guard while simultaneously "proving" drive_files dead.
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

CANONICAL_SCOPE = {
    "user_id": USER,
    "device_id": "*",
    "topos_id": TOPOS,
    "dataset_id": f"{USER}:topos:{TOPOS}",
}

OTHER_TOPOS_SCOPE = {
    "user_id": USER,
    "device_id": "*",
    "topos_id": "topos-b",
    "dataset_id": f"{USER}:topos:topos-b",
}

# The July template shape both live Drive rows carry (drifted schema + lane).
DRIFTED_DEFINITION = {
    "source_type": "ui_stream",
    "delivery": "client_push",
    "schema_id": "gdrive.files.v1",
    "parser_id": "gdrive.files.v1",
    "canonical_mapper_id": "gdrive.files.v1",
    "canonical_group_id": "conversations",
    "source_kind": "ingestion",
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


def test_predecessor_retires_when_healthy_successor_is_live(install_db) -> None:
    old_id = _insert_active(install_db, "drive_files", CANONICAL_SCOPE, DRIFTED_DEFINITION)
    new_id = _insert_active(install_db, "gdrive_files", CANONICAL_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 1
    status, is_active, failure_reason = _row(install_db, old_id)
    assert status == "superseded"
    assert is_active == 0
    assert "successor source_id=gdrive_files" in failure_reason
    status, is_active, _ = _row(install_db, new_id)
    assert (status, is_active) == ("active", 1)


def test_predecessor_stays_without_a_successor(install_db) -> None:
    old_id = _insert_active(install_db, "drive_files", CANONICAL_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 0
    status, is_active, failure_reason = _row(install_db, old_id)
    assert (status, is_active, failure_reason) == ("active", 1, None)


def test_successor_in_a_different_topos_proves_nothing(install_db) -> None:
    old_id = _insert_active(install_db, "drive_files", CANONICAL_SCOPE)
    _insert_active(install_db, "gdrive_files", OTHER_TOPOS_SCOPE)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 0
    _, is_active, _ = _row(install_db, old_id)
    assert is_active == 1


def test_a_drifted_successor_is_not_proof_and_one_pass_never_unwires_both(install_db) -> None:
    # Old drifted gdrive_files retires via the drift guard (bundled replacement
    # exists), so it must not simultaneously prove drive_files dead.
    old_id = _insert_active(install_db, "drive_files", CANONICAL_SCOPE)
    drifted_successor = _insert_active(install_db, "gdrive_files", CANONICAL_SCOPE, DRIFTED_DEFINITION)

    summary = install_service.rehydrate_active_installs_runtime()

    assert summary["superseded"] == 1
    status, is_active, failure_reason = _row(install_db, drifted_successor)
    assert (status, is_active) == ("superseded", 0)
    assert "drift variant" in failure_reason
    status, is_active, _ = _row(install_db, old_id)
    assert (status, is_active) == ("active", 1)
