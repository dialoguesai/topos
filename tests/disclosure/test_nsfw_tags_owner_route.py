"""The sweep's owner door: the owner socket only, a dry run unless asked otherwise, counts back. Invented rows."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos.api.nsfw_maintenance import router
from topos.auth import resolve_request_principal
from topos.disclosure.nsfw_tags import RULE_ID
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.uds import UDSChannelApp

from tests.disclosure.test_nsfw_tags import CLEARS, ROWS, build, dump, tags

PATH = "/v1/privacy/nsfw-recheck"


@pytest.fixture
def node(tmp_path, monkeypatch):
    import topos.config.settings as config
    import topos.core.state as state

    conn = sqlite3.connect(tmp_path / "canonical.db", check_same_thread=False)
    build(conn)
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(config.settings, "nsfw_classifier_enabled", True)
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
    assert body["dry_run"] is True and body["rule"] == RULE_ID and body["walked"] == "all"
    assert body["totals"]["cleared"] == CLEARS and body["totals"]["evaluated"] == len(ROWS)
    assert dump(conn) == before
    # Counts only: no id and no text in the answer.
    assert not any(rid in response.text for _table, rid, *_rest in ROWS)
    assert not any(content and content[:20] in response.text for _table, _rid, content, *_rest in ROWS)


def test_the_owner_socket_can_write_with_dry_run_false(node):
    app, conn = node
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json={"dry_run": False})
    assert response.status_code == 200
    body = response.json()
    assert body["finished"] is True and body["totals"]["cleared"] == CLEARS and body["generation"] == 1
    for table, rid, *_stored, after, _outcome in ROWS:
        assert tags(conn, table, rid) == after, rid
    # Asked again, the owner's write run walks every row again and changes nothing.
    with TestClient(UDSChannelApp(app)) as client:
        again = client.post(PATH, json={"dry_run": False}).json()
    assert again["walked"] == "all" and again["totals"]["cleared"] == 0 and again["generation"] == 1


def test_the_owner_can_take_the_journal_first(node):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json={"dry_run": False, "tables": ["journal_entries"]})
    assert response.status_code == 200
    assert set(response.json()["tables"]) == {"journal_entries"}
    after = dump(conn)
    assert {key[0] for key in before if before[key] != after[key]} == {"journal_entries"}


@pytest.mark.parametrize(
    "payload",
    [
        {"threshold": 0.5},                        # the retired cutoff is not a parameter any more
        {"dry_run": True, "threshold": 0.91},
        {"dry_run": "false"},
        {"dry_run": 0},
        {"dryRun": False},
        {"dry_run": False, "only": ["journal_entries"]},
        {"tables": []},
        {"tables": "journal_entries"},
        {"tables": ["signal_objects"]},
        {"tables": ["journal_entries", "journal_entries"]},
        ["journal_entries"],
    ],
)
def test_an_unexpected_body_is_refused_before_anything_is_read(node, payload):
    app, conn = node
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json=payload)
    assert response.status_code in (400, 422)
    if response.status_code == 400:
        assert response.json()["detail"] == "nsfw_recheck_payload_invalid"
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


def test_switched_off_a_write_run_writes_nothing_and_says_so(node, monkeypatch):
    import topos.config.settings as config

    app, conn = node
    monkeypatch.setattr(config.settings, "nsfw_classifier_enabled", False)
    before = dump(conn)
    with TestClient(UDSChannelApp(app)) as client:
        body = client.post(PATH, json={"dry_run": False}).json()
    assert body["disabled"] is True and body["totals"]["cleared"] == 0
    assert dump(conn) == before


def test_the_node_app_serves_the_route():
    from topos.app import app

    assert any(getattr(route, "path", None) == PATH and "POST" in getattr(route, "methods", set())
               for route in app.routes)
