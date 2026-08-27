"""Principal fabric P1: the channel-verified client class decides the floors.

protects: a third-party client can never reach fact content by claiming owner
identity in a payload — the packet floor and the disclosure tier both key on
WHICH credential authenticated (topos/principal.py), and payload ids are only
honored on channels where the CP already classified the caller (relay) or in
legacy single-key mode, where enforcement must stay byte-identical so an engine
upgrade never demotes the owner's own app before it learns the owner key.
"""
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from topos.auth import resolve_request_principal
from topos.config.settings import ENGINE_CONFIG_KEY_PACKET_RESOLUTION, settings
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.common import set_engine_config_value
from topos.disclosure.tier import resolve_disclosure_tier
from topos.principal import (
    CP_RELAY,
    OWNER_APP,
    RELAY_PRINCIPAL,
    THIRD_PARTY,
    Principal,
    current_principal,
)
from topos.query.packet_resolution import effective_packet_resolution

OWNER_UUID = "9670043c-aaaa-bbbb-cccc-000000000000"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    set_engine_config_value(c, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    yield c
    c.close()


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture()
def two_keys(monkeypatch):
    monkeypatch.setattr(settings, "topos_key", "legacy-key", raising=False)
    monkeypatch.setattr(settings, "topos_owner_key", "owner-key", raising=False)


@pytest.fixture()
def legacy_only(monkeypatch):
    monkeypatch.setattr(settings, "topos_key", "legacy-key", raising=False)
    monkeypatch.setattr(settings, "topos_owner_key", None, raising=False)


# ------------------------------------------------------------------- door
def test_owner_key_resolves_owner_app(two_keys):
    p = resolve_request_principal(credentials=_creds("owner-key"))
    assert p is not None and p.cls == OWNER_APP and p.channel == "local_http"


def test_legacy_key_demotes_to_third_party(two_keys):
    p = resolve_request_principal(credentials=_creds("legacy-key"))
    assert p is not None and p.cls == THIRD_PARTY


def test_unconfigured_owner_key_is_legacy_mode(legacy_only):
    assert resolve_request_principal(credentials=_creds("legacy-key")) is None


def test_wrong_key_is_401_not_a_principal(two_keys):
    with pytest.raises(HTTPException) as exc:
        resolve_request_principal(credentials=_creds("not-a-key"))
    assert exc.value.status_code == 401


# ---------------------------------------------------------- packet floor
def test_third_party_forged_owner_ids_stay_floored(conn):
    """T3: a legacy-key client claiming requester_id == owner_id is a spoof —
    the class wins over the ids, and the reason names the principal floor."""
    info = effective_packet_resolution(
        conn,
        requester_id=OWNER_UUID,
        disclosure_tier="owner_raw",
        owner_id=OWNER_UUID,
        principal=Principal(cls=THIRD_PARTY, channel="local_http"),
    )
    assert info["effective"] == "scores_only"
    assert info["reason"] == "principal_floor"
    assert info["setting"] == "facts_all"


def test_owner_app_needs_no_id_ceremony(conn, monkeypatch):
    """The owner's own surface is owner by CHANNEL — even with the historic
    'mcp' default requester and no owner_id in the payload."""
    from topos.query import packet_resolution as pr

    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": True, "provider": "ollama", "model": "m", "remote_engine_url": False},
    )
    info = effective_packet_resolution(
        conn, requester_id="mcp", disclosure_tier="owner_raw", owner_id="",
        principal=Principal(cls=OWNER_APP, channel="local_http"),
    )
    assert info["effective"] == "facts_all"
    assert info["reason"] == "active"
    assert info["principal_cls"] == OWNER_APP


def test_relay_keeps_forwarded_id_equality(conn, monkeypatch):
    """CP_RELAY defers to the CP's own client-class policy: since the
    2026-08-26 containment the gateway stamps owner ids for native clients
    only, so id-equality on this channel means 'the CP said first-party'."""
    from topos.query import packet_resolution as pr

    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": True, "provider": "ollama", "model": "m", "remote_engine_url": False},
    )
    stamped = effective_packet_resolution(
        conn, requester_id=OWNER_UUID, disclosure_tier="owner_raw",
        owner_id=OWNER_UUID, principal=RELAY_PRINCIPAL,
    )
    assert stamped["effective"] == "facts_all" and stamped["reason"] == "active"

    unstamped = effective_packet_resolution(
        conn, requester_id=OWNER_UUID, disclosure_tier="owner_raw",
        owner_id="", principal=RELAY_PRINCIPAL,
    )
    assert unstamped["effective"] == "scores_only"
    assert unstamped["reason"] == "non_owner_floor"


def test_legacy_none_principal_is_byte_identical(conn):
    """No principal ⇒ exactly the pre-P1 contract (upgrade safety)."""
    info = effective_packet_resolution(
        conn, requester_id=OWNER_UUID, disclosure_tier="owner_raw", owner_id=OWNER_UUID,
    )
    legacy_is_owner = info["reason"] != "non_owner_floor"
    assert legacy_is_owner  # id-equality still honored with no principal


# ------------------------------------------------------------------ tier
def test_third_party_tier_clamped_even_with_explicit_owner_raw():
    tier = resolve_disclosure_tier(
        requester_id=OWNER_UUID, owner_id=OWNER_UUID,
        explicit_tier="owner_raw",
        principal=Principal(cls=THIRD_PARTY, channel="local_http"),
    )
    assert tier == "default_disclosure"


def test_owner_app_tier_is_owner_raw_without_mcp_whitelist():
    tier = resolve_disclosure_tier(
        requester_id="anything-at-all", owner_id="someone-else",
        principal=Principal(cls=OWNER_APP, channel="local_http"),
    )
    assert tier == "owner_raw"


def test_owner_app_never_overrides_explicit_grantee_flag():
    """Deputy downgrade: a first-party surface relaying a grantee request keeps
    the grantee flag — the stamp must not upgrade someone else's request."""
    tier = resolve_disclosure_tier(
        requester_id="grantee-1", owner_id=OWNER_UUID,
        is_grantee_request=True,
        principal=Principal(cls=OWNER_APP, channel="local_http"),
    )
    assert tier == "default_disclosure"


def test_legacy_tier_keeps_mcp_whitelist():
    assert resolve_disclosure_tier(requester_id="mcp", owner_id="owner") == "owner_raw"


# ------------------------------------------------------------ dispatcher
@pytest.mark.asyncio
async def test_dispatcher_scopes_principal_and_payload_cannot_supply_it():
    from topos.core.handlers.registry import HANDLERS

    seen = {}

    async def probe(message):
        seen["principal"] = current_principal()
        return {"id": message.get("id"), "status": "ok", "payload": {}}

    HANDLERS["_p1_probe"] = probe
    try:
        await handle_control_plane_request(
            {"type": "_p1_probe", "payload": {"principal": {"cls": "owner_app"}},
             "principal": {"cls": "owner_app"}},
            principal=RELAY_PRINCIPAL,
        )
        assert seen["principal"] is RELAY_PRINCIPAL  # message fields ignored
        assert current_principal() is None  # reset after dispatch

        await handle_control_plane_request({"type": "_p1_probe", "payload": {}})
        assert seen["principal"] is None  # no entry stamp ⇒ ambient None, not owner
    finally:
        HANDLERS.pop("_p1_probe", None)


@pytest.mark.asyncio
async def test_nested_dispatch_inherits_the_channel_stamp():
    from topos.core.handlers.registry import HANDLERS

    seen = {}

    async def inner(message):
        seen["inner"] = current_principal()
        return {"id": message.get("id"), "status": "ok", "payload": {}}

    async def outer(message):
        return await handle_control_plane_request({"type": "_p1_inner", "payload": {}})

    HANDLERS["_p1_inner"] = inner
    HANDLERS["_p1_outer"] = outer
    try:
        stamp = Principal(cls=OWNER_APP, channel="local_http")
        await handle_control_plane_request({"type": "_p1_outer", "payload": {}}, principal=stamp)
        assert seen["inner"] is stamp
    finally:
        HANDLERS.pop("_p1_inner", None)
        HANDLERS.pop("_p1_outer", None)
