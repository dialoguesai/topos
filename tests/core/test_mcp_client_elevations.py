"""Principal fabric P2 — elevation consent, the UMA-ledger piece.

protects: fact content reaches a third-party client ONLY through an approved,
unexpired, per-scope consent record — clamped to `facts` (special-class stays
owner-first-party), capped by the owner's global setting, still floored by the
locality gate — and a client can only ever request elevation for itself.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.config.settings import ENGINE_CONFIG_KEY_PACKET_RESOLUTION
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.common import set_engine_config_value
from topos.mcp_clients import (
    active_elevation,
    decide_elevation,
    mint_client_token,
    request_elevation,
    revoke_client,
    revoke_elevation,
)
from topos.principal import THIRD_PARTY, Principal
from topos.query.packet_resolution import effective_packet_resolution

CLIENT = "claude-desktop"
SCOPE = "relationships.social"


def _iso(dt) -> str:
    return dt.isoformat()


@pytest.fixture()
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    set_engine_config_value(c, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    mint_client_token(c, client_id=CLIENT)
    from topos.query import packet_resolution as pr

    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": True, "provider": "ollama", "model": "m", "remote_engine_url": False},
    )
    yield c
    c.close()


def _principal(client_id=CLIENT) -> Principal:
    return Principal(cls=THIRD_PARTY, channel="local_http", client_id=client_id)


def _approve(conn, scope=SCOPE, expires_at=None):
    row = request_elevation(conn, client_id=CLIENT, scope_id=scope)
    return decide_elevation(conn, request_id=row["id"], approve=True, expires_at=expires_at)


# ------------------------------------------------------------- enforcement
def test_no_grant_stays_floored(conn):
    info = effective_packet_resolution(
        conn, principal=_principal(), scope_id=SCOPE, disclosure_tier="default_disclosure"
    )
    assert info["effective"] == "scores_only" and info["reason"] == "principal_floor"


def test_approved_grant_lifts_to_facts_never_facts_all(conn):
    """Owner setting is facts_all; the elevated client gets exactly `facts`."""
    grant = _approve(conn)
    info = effective_packet_resolution(
        conn, principal=_principal(), scope_id=SCOPE, disclosure_tier="default_disclosure"
    )
    assert info["effective"] == "facts"
    assert info["reason"] == f"consent_grant:{grant['id']}"


def test_grant_is_per_scope(conn):
    _approve(conn, scope=SCOPE)
    info = effective_packet_resolution(
        conn, principal=_principal(), scope_id="health:read",
        disclosure_tier="default_disclosure",
    )
    assert info["effective"] == "scores_only" and info["reason"] == "principal_floor"


def test_wildcard_scope_covers_everything(conn):
    _approve(conn, scope="*")
    info = effective_packet_resolution(
        conn, principal=_principal(), scope_id="health:read",
        disclosure_tier="default_disclosure",
    )
    assert info["effective"] == "facts"


def test_owner_global_dial_caps_elevation(conn):
    """scores_only owner setting means NO fact content flows, consent or not."""
    _approve(conn)
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "scores_only")
    info = effective_packet_resolution(
        conn, principal=_principal(), scope_id=SCOPE, disclosure_tier="default_disclosure"
    )
    assert info["effective"] == "scores_only" and info["reason"] == "principal_floor"


def test_locality_gate_outranks_consent(conn, monkeypatch):
    _approve(conn)
    from topos.query import packet_resolution as pr

    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": False, "provider": "openai", "model": "m", "remote_engine_url": False},
    )
    info = effective_packet_resolution(
        conn, principal=_principal(), scope_id=SCOPE, disclosure_tier="default_disclosure"
    )
    assert info["effective"] == "scores_only" and info["reason"] == "hosted_binding"


def test_expired_grant_is_inert(conn):
    _approve(conn, expires_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert active_elevation(conn, client_id=CLIENT, scope_id=SCOPE) is None
    info = effective_packet_resolution(
        conn, principal=_principal(), scope_id=SCOPE, disclosure_tier="default_disclosure"
    )
    assert info["effective"] == "scores_only"


def test_revoked_elevation_and_revoked_client_are_inert(conn):
    _approve(conn)
    assert revoke_elevation(conn, client_id=CLIENT, scope_id=SCOPE) == 1
    assert active_elevation(conn, client_id=CLIENT, scope_id=SCOPE) is None
    grant2 = _approve(conn)
    assert grant2["status"] == "approved"
    revoke_client(conn, CLIENT)  # killing the CLIENT kills its elevations too
    assert active_elevation(conn, client_id=CLIENT, scope_id=SCOPE) is None


def test_unenrolled_client_cannot_be_granted(conn):
    with pytest.raises(ValueError):
        request_elevation(conn, client_id="never-enrolled", scope_id=SCOPE)


def test_denied_request_grants_nothing(conn):
    row = request_elevation(conn, client_id=CLIENT, scope_id=SCOPE)
    decided = decide_elevation(conn, request_id=row["id"], approve=False)
    assert decided["status"] == "denied"
    assert active_elevation(conn, client_id=CLIENT, scope_id=SCOPE) is None


# ---------------------------------------------------------------- handlers
@pytest.mark.asyncio
async def test_third_party_requests_for_itself_only(conn, monkeypatch):
    """The stamp decides the subject: a payload naming another client is ignored."""
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    mint_client_token(conn, client_id="other-client")
    out = await handle_control_plane_request(
        {"id": "1", "type": "mcp_client_request_elevation",
         "payload": {"client_id": "other-client", "scope_id": SCOPE}},
        principal=_principal(),
    )
    assert out["status"] == "ok"
    assert out["payload"]["client_id"] == CLIENT  # not other-client


@pytest.mark.asyncio
async def test_third_party_cannot_decide_or_revoke(conn, monkeypatch):
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    row = request_elevation(conn, client_id=CLIENT, scope_id=SCOPE)
    for msg_type, payload in (
        ("mcp_client_decide_elevation", {"request_id": row["id"], "approve": True}),
        ("mcp_client_revoke_elevation", {"client_id": CLIENT}),
        ("mcp_client_list_elevations", {}),
    ):
        out = await handle_control_plane_request(
            {"id": "x", "type": msg_type, "payload": payload}, principal=_principal()
        )
        assert out["status"] == "error", msg_type
    assert active_elevation(conn, client_id=CLIENT, scope_id=SCOPE) is None


@pytest.mark.asyncio
async def test_owner_lifecycle_roundtrip_with_uma_mirror(conn, monkeypatch):
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    out = await handle_control_plane_request(
        {"id": "1", "type": "mcp_client_request_elevation",
         "payload": {"client_id": CLIENT, "scope_id": SCOPE, "note": "wants family facts"}}
    )
    assert out["status"] == "ok" and out["payload"]["status"] == "pending"
    rid = out["payload"]["id"]

    out = await handle_control_plane_request(
        {"id": "2", "type": "mcp_client_decide_elevation",
         "payload": {"request_id": rid, "approve": True}}
    )
    assert out["status"] == "ok" and out["payload"]["status"] == "approved"
    assert out["payload"]["resolution"] == "facts"  # ceiling clamped

    out = await handle_control_plane_request(
        {"id": "3", "type": "mcp_client_list_elevations", "payload": {}}
    )
    assert out["status"] == "ok" and len(out["payload"]["elevations"]) == 1

    # UMA audit mirror: consent decisions are visible in the UMA ledger view.
    mirrored = conn.execute(
        "SELECT COUNT(*) FROM uma_access_requests WHERE requesting_user_id = ?",
        (f"client:{CLIENT}",),
    ).fetchone()[0]
    assert mirrored >= 2  # requested + approved

    out = await handle_control_plane_request(
        {"id": "4", "type": "mcp_client_revoke_elevation", "payload": {"client_id": CLIENT}}
    )
    assert out["status"] == "ok" and out["payload"]["revoked"] == 1
    assert active_elevation(conn, client_id=CLIENT, scope_id=SCOPE) is None


def test_elevation_types_owner_only_except_request():
    from topos.core.handlers.registry import OWNER_ONLY_MESSAGE_TYPES

    assert "mcp_client_request_elevation" not in OWNER_ONLY_MESSAGE_TYPES
    for t in ("mcp_client_decide_elevation", "mcp_client_revoke_elevation",
              "mcp_client_list_elevations"):
        assert t in OWNER_ONLY_MESSAGE_TYPES
