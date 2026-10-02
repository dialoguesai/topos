"""The disclosure backlog's owner door: the owner socket only, counts back, nothing written. Invented rows."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos.api.nsfw_maintenance import router
from topos.auth import resolve_request_principal
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.uds import UDSChannelApp

from tests.disclosure.test_disclosure_sweep import ROWS, build, dump

PATH = "/v1/privacy/disclosure-check"


@pytest.fixture
def node(tmp_path, monkeypatch):
    import topos.config.settings as config
    import topos.core.state as state

    conn = sqlite3.connect(tmp_path / "canonical.db", check_same_thread=False)
    build(conn)
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(config.settings, "platform_privacy_via_engine", True)
    app = FastAPI()
    app.include_router(router)
    yield app, conn
    conn.close()


def test_the_owner_socket_gets_the_backlog_in_counts_and_nothing_is_written(node):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH)
        pending = client.post(PATH, json={"mode": "pending", "tables": ["conversation_messages"]}).json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["dry_run"] is True and body["mode"] == "verify"
    assert (body["totals"]["missing"], body["totals"]["stale"], body["totals"]["calls"]) == (7, 1, 0)
    assert set(pending["tables"]) == {"conversation_messages"} and pending["totals"]["stale"] == 0
    assert pending["totals"]["missing"] == 2
    assert dump(conn) == before
    for _table, rid, values in ROWS:
        assert rid not in response.text
        assert not any(isinstance(text, str) and text.strip() and text in response.text for text in values.values())


@pytest.mark.parametrize("payload", [
    {"dry_run": False}, {"mode": "all"}, {"tables": []}, {"tables": "journal_entries"}, {"tables": ["contacts"]},
    {"tables": ["journal_entries", "journal_entries"]}, ["journal_entries"],
])
def test_an_unexpected_body_is_refused_before_anything_is_read(node, payload):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json=payload)
    assert response.status_code in (400, 422)
    if response.status_code == 400:
        assert response.json()["detail"] == "disclosure_check_payload_invalid"
    assert dump(conn) == before


@pytest.mark.parametrize("principal", [
    None,
    Principal(THIRD_PARTY, "local_http"),
    Principal(THIRD_PARTY, "uds"),
    Principal(OWNER_APP, "cp_relay", acting_user="owner-1"),
    Principal(OWNER_APP, "local_http", acting_user="owner-1"),
])
def test_no_other_principal_reads_the_counts(node, principal):
    app, _conn = node
    app.dependency_overrides[resolve_request_principal] = lambda: principal
    with TestClient(app) as client:
        response = client.post(PATH)
    assert response.status_code == 403 and response.json()["detail"] == "owner_socket_required"


def test_a_node_without_a_database_answers_a_code_only(node, monkeypatch):
    app, _conn = node
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: None)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH)
    assert response.status_code == 503 and response.json() == {"detail": "disclosure_check_unavailable"}
