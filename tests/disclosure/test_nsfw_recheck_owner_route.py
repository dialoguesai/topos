"""The re-check's door: the owner socket only, a dry run unless asked otherwise, counts back."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos.api.nsfw_maintenance import router
from topos.auth import resolve_request_principal
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.uds import UDSChannelApp

from tests.disclosure.nsfw_recheck_fixture import CLEARED_AT_091, MODEL, ROWS, build, dump, tags

PATH = "/v1/privacy/nsfw-recheck"


@pytest.fixture
def node(tmp_path, monkeypatch):
    import topos.config.settings as config
    import topos.core.state as state

    conn = sqlite3.connect(tmp_path / "canonical.db", check_same_thread=False)
    build(conn)
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(config.settings, "nsfw_classifier_threshold", 0.91)
    monkeypatch.setattr(config.settings, "nsfw_classifier_model", MODEL)
    app = FastAPI()
    app.include_router(router)
    yield app, conn
    conn.close()


def test_a_bare_post_on_the_owner_socket_is_a_dry_run(node):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["dry_run"] is True and body["threshold"] == 0.91 and body["comparison"] == "score > threshold"
    assert body["totals"]["below_threshold"] == len(CLEARED_AT_091) and body["totals"]["cleared"] == 0
    assert dump(conn) == before
    assert not any(rid in response.text for _table, rid, *_rest in ROWS)


def test_the_owner_socket_can_write_with_dry_run_false(node):
    app, conn = node
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json={"dry_run": False})
    assert response.status_code == 200
    assert response.json()["totals"]["cleared"] == len(CLEARED_AT_091)
    still_flagged = {(table, rid) for table, rid, *_rest in ROWS if tags(conn, table, rid)[0] == 1}
    assert still_flagged == {(table, rid) for table, rid, flag, *_rest in ROWS if flag == 1} - CLEARED_AT_091


def test_the_owner_can_take_the_journal_first(node):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json={"dry_run": False, "tables": ["journal_entries"]})
    assert response.status_code == 200
    body = response.json()
    assert set(body["tables"]) == {"journal_entries"}
    journal = {(table, rid) for table, rid in CLEARED_AT_091 if table == "journal_entries"}
    assert body["totals"]["cleared"] == len(journal)
    after = dump(conn)
    assert {key for key in before if before[key] != after[key]} == journal


def test_a_dry_run_may_ask_what_if_at_another_cutoff(node):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json={"dry_run": True, "threshold": 0.5})
    assert response.status_code == 200 and response.json()["threshold"] == 0.5
    assert dump(conn) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"dry_run": False, "threshold": 0.5},     # a write always uses the configured cutoff
        {"threshold": 1.0},
        {"threshold": -0.1},
        {"threshold": "0.9"},
        {"threshold": True},
        {"dry_run": "false"},
        {"dry_run": 0},
        {"dryRun": False},
        {"dry_run": False, "only": ["journal_entries"]},
        {"tables": []},
        {"tables": "journal_entries"},
        {"tables": ["signal_objects"]},
        {"tables": ["journal_entries", "journal_entries"]},
    ],
)
def test_an_unexpected_body_is_refused_before_anything_is_read(node, payload):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json=payload)
    assert response.status_code == 400 and response.json()["detail"] == "nsfw_recheck_payload_invalid"
    assert dump(conn) == before


def test_tcp_cannot_claim_the_owner_socket(node):
    app, conn = node
    before = dump(conn)
    with TestClient(app) as client:
        response = client.post(PATH, json={"dry_run": False},
                               headers={"X-Topos-Client": "topos_home_chat", "X-Transport": "uds"})
    assert response.status_code == 401
    assert dump(conn) == before


@pytest.mark.parametrize("principal", [
    None,
    Principal(THIRD_PARTY, "local_http"),
    Principal(THIRD_PARTY, "uds"),
    Principal(OWNER_APP, "cp_relay", acting_user="owner-1"),
    Principal(OWNER_APP, "local_http", acting_user="owner-1"),
])
def test_no_other_principal_reaches_the_rows(node, principal):
    app, conn = node
    before = dump(conn)
    app.dependency_overrides[resolve_request_principal] = lambda: principal
    with TestClient(app) as client:
        response = client.post(PATH, json={"dry_run": False})
    assert response.status_code == 403 and response.json()["detail"] == "owner_socket_required"
    assert dump(conn) == before


def test_a_node_without_a_database_answers_a_code_only(node, monkeypatch):
    app, _conn = node
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: None)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH)
    assert response.status_code == 503 and response.json() == {"detail": "nsfw_recheck_unavailable"}
    assert response.headers["cache-control"] == "no-store"


def test_the_node_app_serves_the_route():
    from topos.app import app

    assert any(getattr(route, "path", None) == PATH and "POST" in getattr(route, "methods", set())
               for route in app.routes)
