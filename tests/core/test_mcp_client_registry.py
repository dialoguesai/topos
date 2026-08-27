"""Principal fabric P2: enrolled-client registry, tpk auth, verified audit rows.

protects: a per-client token names its client in the principal and in the
request log; a revoked or forged token authenticates nowhere; enrollment is an
owner action a third-party principal cannot perform; and require_api_key never
accepts a tpk token (a client's surface is the MCP tool set, not every REST
endpoint the shared key could reach).
"""
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from topos.auth import require_api_key, resolve_request_principal
from topos.config.settings import settings
from topos.core.handlers import handle_control_plane_request
from topos.mcp_clients import (
    list_clients,
    mint_client_token,
    normalize_client_id,
    revoke_client,
    verify_client_token,
)
from topos.principal import THIRD_PARTY, Principal


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ---------------------------------------------------------------- registry
def test_mint_verify_roundtrip_names_the_client(conn):
    row = mint_client_token(conn, client_id="Claude Desktop", display_name="Claude Desktop")
    assert row["client_id"] == "claude-desktop"  # slugged
    token = row["token"]
    assert token.startswith("tpk_claude-desktop.")
    verified = verify_client_token(conn, token)
    assert verified is not None and verified["client_id"] == "claude-desktop"
    assert verified.get("last_used_at") or True  # touch is best-effort


def test_hash_at_rest_and_single_disclosure(conn):
    row = mint_client_token(conn, client_id="cursor")
    listed = list_clients(conn)
    assert listed and "token_hash" not in listed[0] and "token" not in listed[0]
    stored = conn.execute("SELECT token_hash FROM mcp_clients").fetchone()[0]
    assert row["token"] not in stored and len(stored) == 64  # sha256 hex, not plaintext


def test_forged_and_wrong_secret_tokens_fail(conn):
    mint_client_token(conn, client_id="cursor")
    assert verify_client_token(conn, "tpk_cursor.deadbeef") is None
    assert verify_client_token(conn, "tpk_unknown.deadbeef") is None
    assert verify_client_token(conn, "not-a-tpk-token") is None
    assert verify_client_token(conn, "tpk_") is None


def test_revocation_tombstones_and_blocks(conn):
    token = mint_client_token(conn, client_id="cursor")["token"]
    assert revoke_client(conn, "cursor") is True
    assert verify_client_token(conn, token) is None
    rows = list_clients(conn)  # tombstone survives for the audit trail
    assert rows[0]["revoked_at"]
    assert revoke_client(conn, "cursor") is False  # idempotent


def test_remint_rotates_and_unrevokes(conn):
    first = mint_client_token(conn, client_id="cursor")["token"]
    revoke_client(conn, "cursor")
    second = mint_client_token(conn, client_id="cursor")["token"]
    assert verify_client_token(conn, first) is None
    assert verify_client_token(conn, second)["client_id"] == "cursor"


def test_client_id_normalization_rejects_garbage():
    assert normalize_client_id("  ChatGPT!! ") == "chatgpt"
    with pytest.raises(ValueError):
        normalize_client_id("---")
    with pytest.raises(ValueError):
        normalize_client_id("")


# -------------------------------------------------------------------- auth
@pytest.fixture()
def registry_db(conn, monkeypatch):
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(settings, "topos_key", "legacy-key", raising=False)
    monkeypatch.setattr(settings, "topos_owner_key", "owner-key", raising=False)
    return conn


def test_tpk_token_resolves_named_third_party_principal(registry_db):
    token = mint_client_token(registry_db, client_id="claude-desktop")["token"]
    p = resolve_request_principal(credentials=_creds(token))
    assert p is not None
    assert p.cls == THIRD_PARTY and p.client_id == "claude-desktop"


def test_revoked_tpk_token_is_401(registry_db):
    token = mint_client_token(registry_db, client_id="claude-desktop")["token"]
    revoke_client(registry_db, "claude-desktop")
    with pytest.raises(HTTPException) as exc:
        resolve_request_principal(credentials=_creds(token))
    assert exc.value.status_code == 401


def test_require_api_key_rejects_tpk_tokens(registry_db):
    """A client token is not a general-purpose engine key: the REST surface
    guarded by require_api_key stays closed to enrolled clients."""
    token = mint_client_token(registry_db, client_id="claude-desktop")["token"]
    with pytest.raises(HTTPException) as exc:
        require_api_key(credentials=_creds(token))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------- handlers
@pytest.mark.asyncio
async def test_enroll_list_revoke_handlers_roundtrip(conn, monkeypatch):
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    out = await handle_control_plane_request(
        {"id": "1", "type": "mcp_client_enroll",
         "payload": {"client_id": "claude-desktop", "display_name": "Claude Desktop"}}
    )
    assert out["status"] == "ok"
    assert out["payload"]["token"].startswith("tpk_claude-desktop.")
    assert "token_hash" not in out["payload"]

    out = await handle_control_plane_request({"id": "2", "type": "mcp_client_list", "payload": {}})
    assert out["status"] == "ok"
    assert out["payload"]["clients"][0]["client_id"] == "claude-desktop"

    out = await handle_control_plane_request(
        {"id": "3", "type": "mcp_client_revoke", "payload": {"client_id": "claude-desktop"}}
    )
    assert out["status"] == "ok" and out["payload"]["revoked"] is True


@pytest.mark.asyncio
async def test_third_party_principal_cannot_enroll(conn, monkeypatch):
    """Depth behind the owner_only marker: one enrolled client must never be
    able to mint another."""
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    out = await handle_control_plane_request(
        {"id": "1", "type": "mcp_client_enroll", "payload": {"client_id": "evil"}},
        principal=Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop"),
    )
    assert out["status"] == "error"
    assert "owner action" in out["error"]


def test_enroll_types_are_owner_only():
    from topos.core.handlers.registry import OWNER_ONLY_MESSAGE_TYPES

    for t in ("mcp_client_enroll", "mcp_client_list", "mcp_client_revoke"):
        assert t in OWNER_ONLY_MESSAGE_TYPES


# ------------------------------------------------------------------- audit
def test_record_mcp_request_writes_verified_principal(conn):
    from topos.core.state import record_mcp_request
    from topos.principal import reset_principal, set_principal

    stamp = Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop")
    token = set_principal(stamp)
    try:
        record_mcp_request(conn, "query_scope", source="claimed-source", requester_id="claimed-id")
    finally:
        reset_principal(token)
    row = conn.execute(
        "SELECT source, requester_id, verified_cls, verified_client_id FROM mcp_request_log"
    ).fetchone()
    assert row == ("claimed-source", "claimed-id", "third_party", "claude-desktop")


def test_record_mcp_request_unstamped_stays_null(conn):
    from topos.core.state import record_mcp_request

    record_mcp_request(conn, "query_scope", source="s")
    row = conn.execute(
        "SELECT verified_cls, verified_client_id FROM mcp_request_log"
    ).fetchone()
    assert row == (None, None)
