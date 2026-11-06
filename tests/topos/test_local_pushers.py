"""Local pushers (topos/local_pushers.py, auth.resolve_local_pusher, api/local_pushers.py, ingestion/local_push.py).

protects: an app pushes rows into a local-first node only with a key the owner minted on a terminal; the
key resolves to the owner's capture app (``owner_app`` on the ``local_pusher`` channel, ``writer_app_id`` =
the app) on the pusher doors ALONE — ``require_api_key`` and ``resolve_request_principal`` 401 it, a peer
that is not this machine is refused, and a ``tpk_`` token, a ``tpa_`` label and the shared key are refused
on the pusher door; the key writes exactly the sources the owner named (403 ``source_not_allowed`` for any
other); a second batch under the same ``write_id`` writes nothing (``deduplicated``); rows land under the
node's own dataset with the writer stamped and ``rescope_to_account`` moves them like any other; CORS is
answered for the exact enrolled extension origins and never for a wildcard; the three key namespaces
(``tpk_``, ``tpa_``, ``tpw_``) never cross and Connected apps neither lists nor revokes a pusher.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

pytestmark = pytest.mark.public

import topos.core.handlers as hub  # noqa: E402
import topos.core.state as state  # noqa: E402
from topos import local_identity as li  # noqa: E402
from topos import local_pushers as lp  # noqa: E402
from topos.api.local_pushers import LocalPusherCORS, router  # noqa: E402
from topos.auth import require_api_key, resolve_local_pusher, resolve_request_principal  # noqa: E402
from topos.config.settings import settings  # noqa: E402
from topos.mcp_clients import (  # noqa: E402
    list_clients,
    mint_agent_label,
    mint_client_token,
    revoke_client,
    verify_agent_label,
    verify_client_token,
)
from topos.principal import OWNER_APP, Principal  # noqa: E402
from topos.uds import _transport  # noqa: E402

APP = "browser-history-plugin"
ORIGIN = "chrome-extension://" + "a" * 32
OTHER_ORIGIN = "chrome-extension://" + "b" * 32
ACCOUNT = "user-kc-1234"
SOURCES = ("browser_visits", "browser_events")


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """A migrated node database with a local identity, served to every thread."""
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    li.ensure_local_identity(c)
    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    # The dedupe store binds the accessor at import; point it at this database too.
    import topos.ingestion.usage_inbox_dedupe as dedupe

    monkeypatch.setattr(dedupe, "get_db_connection", lambda: c)
    monkeypatch.setenv("TOPOS_PIPELINE_WORKER", "off")
    monkeypatch.setenv("TOPOS_INGESTION_BASE_PATH", str(tmp_path / "raw"))
    monkeypatch.setattr(settings, "topos_key", "legacy-key", raising=False)
    monkeypatch.setattr(settings, "topos_owner_key", "owner-key", raising=False)
    yield c
    c.close()


def _grant(sources=SOURCES, origins=(ORIGIN,)):
    return lp.PusherGrant(tuple(sources), tuple(origins))


def _enrol(conn, app=APP, **kw):
    return lp.enrol_pusher(conn, app=app, grant=kw.pop("grant", _grant()), **kw)


def _request(headers=None, *, client=("127.0.0.1", 40000), method="POST", path="/sources/browser_visits/ingest/batch"):
    return Request({
        "type": "http", "method": method, "path": path, "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    })


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None


@contextmanager
def socket():
    token = _transport.set("uds")
    try:
        yield
    finally:
        _transport.reset(token)


def _door(origins=None):
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(LocalPusherCORS, origins=origins)
    return app


def _client(app, peer=("127.0.0.1", 40000)):
    return AsyncClient(transport=ASGITransport(app=app, client=peer), base_url="http://127.0.0.1:8676")


def _visit(i: int) -> dict:
    return {"url": f"https://example.test/page-{i}", "visited_at": f"2026-11-05T10:0{i}:00.000Z",
            "title": f"Page {i}", "hostname": "example.test", "device_name": "Test computer",
            "capture_id": f"cap-{i}"}


def _activity_rows(conn):
    """``(writer_class, writer_app_id, writer_dataset_id)`` per activity row: the writer stamp and the dataset the
    door recorded (``activity_events`` has no dataset column of its own; ``writer_dataset_id`` is it)."""
    return conn.execute(
        "SELECT writer_class, writer_app_id, writer_dataset_id FROM activity_events ORDER BY rowid"
    ).fetchall()


async def _in_thread(fn, *args, **kwargs):
    """Registry writes take the write gate (a blocking lock): off the test's event loop, as the node does."""
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


# ------------------------------------------------------------------ registry
def test_enrolment_mints_a_tpw_key_whose_hash_is_the_row(conn):
    row = _enrol(conn)
    assert row["key"].startswith("tpw_browser-history-plugin.") and len(row["key"].split(".", 1)[1]) == 40
    assert row["client_id"] == "pusher-browser-history-plugin" and row["principal_class"] == "local_pusher"
    assert "token_hash" not in row
    verified = lp.verify_pusher_key(conn, row["key"])
    assert verified["app"] == APP and verified["grant"] == _grant()
    assert lp.verify_pusher_key(conn, row["key"][:-1] + ("0" if row["key"][-1] != "0" else "1")) is None


def test_the_display_name_is_the_capture_apps_and_sources_and_origins_are_stored(conn):
    row = _enrol(conn)
    assert row["display_name"] == "Dialogues Browser History"
    listed = lp.list_pushers(conn)
    assert [(r["app"], r["sources"], r["origins"], r["revoked_at"]) for r in listed] == [
        (APP, list(SOURCES), [ORIGIN], None)
    ]
    assert all("token_hash" not in r for r in listed)


def test_revocation_is_a_tombstone_checked_on_the_next_verify(conn):
    key = _enrol(conn)["key"]
    assert lp.revoke_pusher(conn, app=APP) is True
    assert lp.verify_pusher_key(conn, key) is None
    assert lp.revoke_pusher(conn, app=APP) is False
    assert lp.list_pushers(conn)[0]["revoked_at"]


def test_a_revoked_app_re_enrols_only_on_a_fresh_approval(conn):
    _enrol(conn)
    lp.revoke_pusher(conn, app=APP)
    with pytest.raises(lp.PusherRefused, match="fresh approval"):
        _enrol(conn)
    again = _enrol(conn, approved=True)
    assert lp.verify_pusher_key(conn, again["key"])["revoked_at"] is None


def test_re_enrolment_rotates_the_key_and_replaces_the_grant(conn):
    first = _enrol(conn)["key"]
    second = _enrol(conn, grant=_grant(sources=("browser_visits",), origins=(OTHER_ORIGIN,)))["key"]
    assert first != second
    assert lp.verify_pusher_key(conn, first) is None
    assert lp.verify_pusher_key(conn, second)["grant"] == _grant(("browser_visits",), (OTHER_ORIGIN,))


def test_sources_must_exist_and_take_pushes():
    with pytest.raises(lp.PusherRefused, match="no source named"):
        lp.parse_sources(["no_such_source"])
    with pytest.raises(lp.PusherRefused, match="not a source an app pushes"):
        lp.parse_sources(["imessage"])
    with pytest.raises(lp.PusherRefused, match="not a source an app pushes"):
        lp.parse_sources(["chatgpt_file_ingestion"])
    with pytest.raises(lp.PusherRefused, match="at least one"):
        lp.parse_sources(["", " "])
    assert lp.parse_sources(["browser_visits", "browser_events", "browser_visits"]) == SOURCES


@pytest.mark.parametrize("bad", [
    "chrome-extension://*", "*", "chrome-extension://", "https://example.test", "http://127.0.0.1:8676",
    "chrome-extension://" + "a" * 32 + "/*", "chrome-extension://abc?x=1", "null",
])
def test_an_origin_is_one_exact_extension_origin_never_a_wildcard(bad):
    with pytest.raises(lp.PusherRefused, match="not an extension origin"):
        lp.parse_origins([bad])


def test_the_three_extension_schemes_are_accepted_exactly():
    uuid = "9a3f1c0e-0000-4000-8000-00000000cafe"
    assert lp.parse_origins([ORIGIN, f"moz-extension://{uuid}", f"safari-web-extension://{uuid.upper()}", ORIGIN + "/"]) == (
        ORIGIN, f"moz-extension://{uuid}", f"safari-web-extension://{uuid.upper()}")


def test_the_namespaces_never_cross(conn):
    key = _enrol(conn)["key"]
    tpk = mint_client_token(conn, client_id="desk-app")["token"]
    tpa = mint_agent_label(conn, client="codex")["label"]
    # A pusher key is no client token and no agent label; theirs are no pusher key.
    assert verify_client_token(conn, key) is None and verify_agent_label(conn, key) is None
    assert lp.verify_pusher_key(conn, tpk) is None and lp.verify_pusher_key(conn, tpa) is None
    # Connected apps neither lists nor revokes a pusher, and cannot take its row id.
    assert [r["client_id"] for r in list_clients(conn)] == ["desk-app"]
    assert revoke_client(conn, "pusher-browser-history-plugin") is False
    assert lp.verify_pusher_key(conn, key) is not None
    with pytest.raises(ValueError, match="reserved for local pushers"):
        mint_client_token(conn, client_id="pusher-browser-history-plugin")
    with pytest.raises(ValueError, match="not its row id"):
        lp.pusher_ids("pusher-browser-history-plugin")
    with pytest.raises(ValueError, match="not its row id"):
        lp.pusher_ids("agent-codex")
    # A pusher never reads: nothing to elevate.
    from topos.mcp_clients import request_elevation

    with pytest.raises(ValueError, match="unknown client"):
        request_elevation(conn, client_id="pusher-browser-history-plugin", scope_id="activity")


# ---------------------------------------------------------------------- auth
def test_the_generic_resolvers_refuse_the_key_so_its_surface_is_the_pusher_doors(conn):
    key = _enrol(conn)["key"]
    for resolver in (require_api_key, resolve_request_principal):
        with pytest.raises(HTTPException) as exc:
            resolver(_request(), _creds(key))
        assert exc.value.status_code == 401
    # Legacy mode (no owner key) too.
    settings.topos_owner_key = None
    for resolver in (require_api_key, resolve_request_principal):
        with pytest.raises(HTTPException) as exc:
            resolver(_request(), _creds(key))
        assert exc.value.status_code == 401


def test_the_pusher_door_resolves_the_owners_capture_app_over_loopback(conn):
    key = _enrol(conn)["key"]
    principal = resolve_local_pusher(_request(), _creds(key))
    assert principal == Principal(cls=OWNER_APP, channel="local_pusher", client_id=APP, app_grant=_grant())
    from topos.features.provenance.writer_class import writer_app_for_principal, writer_class_for_principal

    assert writer_class_for_principal(principal) == "owner_app"
    assert writer_app_for_principal(principal) == APP
    # A socket owner names no app; a stamped relay still does.
    assert writer_app_for_principal(Principal(cls=OWNER_APP, channel="uds")) is None
    assert writer_app_for_principal(Principal(cls=OWNER_APP, channel="cp_relay", client_id=APP)) == APP


@pytest.mark.parametrize("peer", [("10.0.0.5", 40000), ("192.168.1.9", 1), ("fe80::1", 2)])
def test_the_pusher_door_refuses_a_peer_that_is_not_this_machine(conn, peer):
    key = _enrol(conn)["key"]
    with pytest.raises(HTTPException) as exc:
        resolve_local_pusher(_request(client=peer), _creds(key))
    assert exc.value.status_code == 403 and exc.value.detail["error"] == "loopback_required"


def test_the_pusher_door_refuses_every_other_credential(conn):
    _enrol(conn)
    tpk = mint_client_token(conn, client_id="desk-app")["token"]
    tpa = mint_agent_label(conn, client="codex")["label"]
    for token in (tpk, tpa, "legacy-key", "owner-key", "tpw_browser-history-plugin." + "0" * 40, "tpw_nobody.x"):
        with pytest.raises(HTTPException) as exc:
            resolve_local_pusher(_request(), _creds(token))
        assert exc.value.status_code == 401, token
    with pytest.raises(HTTPException) as exc:
        resolve_local_pusher(_request(), None)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:  # a label over TCP is the usual 401
        resolve_local_pusher(_request({"X-Topos-Agent": tpa}), _creds(tpa))
    assert exc.value.status_code == 401


def test_a_revoked_key_is_refused_on_the_very_next_request(conn):
    key = _enrol(conn)["key"]
    resolve_local_pusher(_request(), _creds(key))
    lp.revoke_pusher(conn, app=APP)
    with pytest.raises(HTTPException) as exc:
        resolve_local_pusher(_request(), _creds(key))
    assert exc.value.status_code == 401 and exc.value.detail["error"] == "pusher_key_refused"


def test_the_owner_socket_passes_as_the_owner_and_an_agent_label_does_not(conn):
    with socket():
        assert resolve_local_pusher(_request(), None) == Principal(cls=OWNER_APP, channel="uds")
        tpa = mint_agent_label(conn, client="codex")["label"]
        with pytest.raises(HTTPException) as exc:
            resolve_local_pusher(_request({"X-Topos-Agent": tpa}), None)
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------- door
@pytest.mark.asyncio
async def test_a_batch_lands_under_the_local_dataset_with_the_writer_stamped_and_dedupes_by_write_id(conn):
    key = (await _in_thread(_enrol, conn))["key"]
    scope = li.identity_mode(conn)
    assert scope == "local"
    from topos.ingestion.ai_tool_sync import resolve_owner_scope

    dataset = resolve_owner_scope(conn)["dataset_id"]
    async with _client(_door()) as client:
        headers = {"Authorization": f"Bearer {key}"}
        body = {"records": [_visit(1), _visit(2)], "write_id": "batch-1"}
        first = await client.post("/sources/browser_visits/ingest/batch", json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json() == {"status": "ok", "source_id": "browser_visits", "write_id": "batch-1",
                                "records_processed": 2, "records_total": 2, "errors": [], "deduplicated": False}
        rows = _activity_rows(conn)
        assert len(rows) == 2
        assert {tuple(r) for r in rows} == {("owner_app", APP, dataset)}
        assert conn.execute("SELECT COUNT(*) FROM browser_visits").fetchone()[0] == 2

        second = await client.post("/sources/browser_visits/ingest/batch", json=body, headers=headers)
        assert second.status_code == 200
        assert second.json()["deduplicated"] is True and second.json()["records_processed"] == 2
        assert len(_activity_rows(conn)) == 2

        # The control plane's header spelling of the same id dedupes too.
        third = await client.post("/sources/browser_visits/ingest/batch", json={"records": [_visit(3)]},
                                  headers={**headers, "Idempotency-Key": "batch-1"})
        assert third.json()["deduplicated"] is True
        assert len(_activity_rows(conn)) == 2

        # A new id writes.
        fourth = await client.post("/sources/browser_visits/ingest/batch", json={"records": [_visit(3)]},
                                   headers={**headers, "Idempotency-Key": "batch-2"})
        assert fourth.json()["records_processed"] == 1 and fourth.json()["deduplicated"] is False
        assert len(_activity_rows(conn)) == 3
        # One deferred-enrichment job per written batch, keyed by write id.
        keys = [r[0] for r in conn.execute("SELECT idempotency_key FROM pipeline_jobs ORDER BY rowid").fetchall()]
        assert keys == ["inbox_derivation:batch-1", "inbox_derivation:batch-2"]


@pytest.mark.asyncio
async def test_the_allowlist_is_the_owners_and_a_source_outside_it_is_refused(conn):
    key = (await _in_thread(_enrol, conn, grant=_grant(sources=("browser_visits",))))["key"]
    async with _client(_door()) as client:
        headers = {"Authorization": f"Bearer {key}"}
        refused = await client.post("/sources/browser_events/ingest/batch", json={"records": [_visit(1)]}, headers=headers)
        assert refused.status_code == 403
        assert refused.json()["error"] == "source_not_allowed"
        assert "browser_events" in refused.json()["message"] and "pushers add browser-history-plugin" in refused.json()["message"]
        assert _activity_rows(conn) == []
        # An unknown source, a source nobody pushes, an empty batch, an oversize one.
        assert (await client.post("/sources/no_such/ingest/batch", json={"records": [{}]}, headers=headers)).status_code == 404
        assert (await client.post("/sources/imessage/ingest/batch", json={"records": [{}]}, headers=headers)).status_code == 400
        assert (await client.post("/sources/browser_visits/ingest/batch", json={"records": []}, headers=headers)).status_code == 400
        assert (await client.post("/sources/browser_visits/ingest/batch", json={}, headers=headers)).status_code == 400
        big = {"records": [_visit(0)] * (lp.MAX_BATCH_RECORDS + 1)}
        assert (await client.post("/sources/browser_visits/ingest/batch", json=big, headers=headers)).status_code == 413
        # A batch of nothing valid writes nothing and says so; no delivery is recorded, so a retry can land.
        bad = await client.post("/sources/browser_visits/ingest/batch",
                                json={"records": [{"title": "no url"}], "write_id": "bad-1"}, headers=headers)
        assert bad.status_code == 422 and bad.json()["error"] == "all_records_failed"
        assert bad.json()["records_processed"] == 0 and bad.json()["errors"]
        from topos.ingestion.usage_inbox_dedupe import get_prior_delivery

        assert get_prior_delivery("bad-1") is None


@pytest.mark.asyncio
async def test_the_door_refuses_what_the_dependency_refuses(conn):
    key = (await _in_thread(_enrol, conn))["key"]
    body = {"records": [_visit(1)]}
    async with _client(_door(), peer=("10.0.0.5", 1)) as client:
        off_machine = await client.post("/sources/browser_visits/ingest/batch", json=body,
                                        headers={"Authorization": f"Bearer {key}"})
        assert off_machine.status_code == 403 and off_machine.json()["detail"]["error"] == "loopback_required"
    async with _client(_door()) as client:
        for token in ("legacy-key", "owner-key", (await _in_thread(mint_client_token, conn, client_id="desk-app"))["token"]):
            r = await client.post("/sources/browser_visits/ingest/batch", json=body, headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401, token
        assert (await client.post("/sources/browser_visits/ingest/batch", json=body)).status_code == 401
    assert _activity_rows(conn) == []


@pytest.mark.asyncio
async def test_me_says_what_the_key_is_good_for(conn):
    key = (await _in_thread(_enrol, conn))["key"]
    async with _client(_door()) as client:
        me = await client.get("/local-pushers/me", headers={"Authorization": f"Bearer {key}"})
        assert me.status_code == 200
        assert me.json() == {"status": "ok", "app": APP, "owner": False, "sources": list(SOURCES), "origins": [ORIGIN]}
        assert (await client.get("/local-pushers/me")).status_code == 401


@pytest.mark.asyncio
async def test_the_owner_on_the_socket_pushes_any_source_with_no_app_named(conn):
    from topos.ingestion.ai_tool_sync import resolve_owner_scope

    dataset = resolve_owner_scope(conn)["dataset_id"]
    async with _client(_door()) as client:
        with socket():
            r = await client.post("/sources/browser_events/ingest/batch",
                                  json={"records": [{"url": "https://example.test/a", "event_type": "click",
                                                     "visited_at": "2026-11-05T10:00:00Z", "capture_id": "e1"}]})
            me = await client.get("/local-pushers/me")
    assert r.status_code == 200 and r.json()["records_processed"] == 1
    assert [tuple(x) for x in _activity_rows(conn)] == [("owner_app", None, dataset)]
    assert me.json()["owner"] is True and me.json()["app"] is None


# ---------------------------------------------------------------------- CORS
@pytest.mark.asyncio
async def test_cors_answers_the_exact_enrolled_origin_and_nothing_else(conn):
    key = (await _in_thread(_enrol, conn))["key"]
    async with _client(_door()) as client:
        preflight = await client.options("/sources/browser_visits/ingest/batch", headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == ORIGIN
        assert preflight.headers["vary"] == "Origin"
        assert "Idempotency-Key" in preflight.headers["access-control-allow-headers"]
        assert "access-control-allow-credentials" not in preflight.headers

        actual = await client.post("/sources/browser_visits/ingest/batch", json={"records": [_visit(1)]},
                                   headers={"Authorization": f"Bearer {key}", "Origin": ORIGIN})
        assert actual.status_code == 200 and actual.headers["access-control-allow-origin"] == ORIGIN

        # Another extension, a web page, a wildcard: no allow header on the pusher path.
        for origin in (OTHER_ORIGIN, "https://example.test", "chrome-extension://*", "*", "null"):
            r = await client.post("/sources/browser_visits/ingest/batch", json={"records": [_visit(2)]},
                                  headers={"Authorization": f"Bearer {key}", "Origin": origin})
            assert "access-control-allow-origin" not in r.headers, origin
            p = await client.options("/sources/browser_visits/ingest/batch",
                                     headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
            assert "access-control-allow-origin" not in p.headers, origin

        # No Origin: untouched.
        plain = await client.get("/local-pushers/me", headers={"Authorization": f"Bearer {key}"})
        assert "access-control-allow-origin" not in plain.headers

        # Disconnecting closes the origin on the next request.
        await _in_thread(lp.revoke_pusher, conn, app=APP)
        gone = await client.options("/sources/browser_visits/ingest/batch",
                                    headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in gone.headers


@pytest.mark.asyncio
async def test_cors_layer_touches_no_other_path(conn):
    await _in_thread(_enrol, conn)
    app = _door()

    @app.get("/elsewhere")
    async def elsewhere():
        return {"ok": True}

    async with _client(app) as client:
        r = await client.get("/elsewhere", headers={"Origin": ORIGIN})
        assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


def test_enrolled_origins_read_every_live_pusher_and_fail_closed(conn):
    assert lp.enrolled_origins(conn) == frozenset()
    _enrol(conn)
    _enrol(conn, app="voxterm", grant=_grant(sources=("browser_events",), origins=(OTHER_ORIGIN,)))
    assert lp.enrolled_origins(conn) == {ORIGIN, OTHER_ORIGIN}
    lp.revoke_pusher(conn, app="voxterm")
    assert lp.enrolled_origins(conn) == {ORIGIN}
    assert lp.enrolled_origins(None) == frozenset()
    assert lp.enrolled_origins(sqlite3.connect(":memory:")) == frozenset()


# ------------------------------------------------------------------- rescope
@pytest.mark.asyncio
async def test_connecting_an_account_later_carries_the_pushed_rows_and_keeps_the_app(conn, tmp_path):
    key = (await _in_thread(_enrol, conn))["key"]
    from topos.ingestion.ai_tool_sync import resolve_owner_scope

    before = resolve_owner_scope(conn)
    async with _client(_door()) as client:
        r = await client.post("/sources/browser_visits/ingest/batch",
                              json={"records": [_visit(1), _visit(2)], "write_id": "pre-account"},
                              headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
    report = await _in_thread(li.rescope_to_account, conn, user_id=ACCOUNT, raw_base_path=tmp_path / "raw")
    after = resolve_owner_scope(conn)
    assert after["user_id"] == ACCOUNT and after["identity_mode"] == "account"
    assert after["dataset_id"] == f"{ACCOUNT}:topos:{before['topos_id']}"
    rows = _activity_rows(conn)
    assert len(rows) == 2 and {tuple(x) for x in rows} == {("owner_app", APP, after["dataset_id"])}
    assert conn.execute("SELECT COUNT(*) FROM browser_visits WHERE dataset_id = ?", (after["dataset_id"],)).fetchone()[0] == 2
    assert "activity_events" in report["tables"] and "browser_visits" in report["tables"]
    # The enrolment and the dedupe record carry no owner id: untouched, still good.
    assert lp.verify_pusher_key(conn, key)["app"] == APP
    from topos.ingestion.usage_inbox_dedupe import get_prior_delivery

    assert get_prior_delivery("pre-account") == {"records_processed": 2, "records_total": 2}
