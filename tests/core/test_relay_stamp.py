"""P3 relay stamps: only a verified stamp changes anything, and then only names.

protects: a forged, tampered, replayed, expired, or class-smuggling stamp
resolves to legacy CP_RELAY behavior — never to owner, never to a named
client; a verified stamp finally gives RELAY third-party clients an identity
the elevation ledger can attach to.
"""
import base64
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from topos.principal import OWNER_APP, THIRD_PARTY
from topos.relay_stamp import (
    STAMP_FIELD,
    canonical_signing_payload,
    verify_relay_stamp,
)

KEY = Ed25519PrivateKey.generate()
PUB_B64 = base64.b64encode(
    KEY.public_key().public_bytes_raw()
).decode()


def _sign(message, *, cls, client_id="", acting_user="", iat=None, exp=None, key=KEY):
    now = time.time()
    stamp = {
        "v": 1,
        "cls": cls,
        "client_id": client_id,
        "acting_user": acting_user,
        "iat": now if iat is None else iat,
        "exp": now + 120 if exp is None else exp,
    }
    payload = canonical_signing_payload(
        stamp, msg_id=str(message.get("id") or ""), msg_type=str(message.get("type") or "")
    )
    stamp["sig"] = base64.b64encode(key.sign(payload)).decode()
    message[STAMP_FIELD] = stamp
    return message


@pytest.fixture(autouse=True)
def pinned_key(monkeypatch):
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", PUB_B64)


def _msg(msg_id="m1", msg_type="query"):
    return {"id": msg_id, "type": msg_type, "payload": {}}


def test_verified_stamp_names_the_client():
    p = verify_relay_stamp(_sign(_msg(), cls=THIRD_PARTY, client_id="chatgpt",
                                 acting_user="user-uuid"))
    assert p is not None
    assert (p.cls, p.client_id, p.acting_user, p.channel) == (
        THIRD_PARTY, "chatgpt", "user-uuid", "cp_relay")


def test_owner_app_stamp_verifies():
    p = verify_relay_stamp(_sign(_msg(), cls=OWNER_APP, client_id="topos_home_chat"))
    assert p is not None and p.cls == OWNER_APP


def test_missing_stamp_is_legacy():
    assert verify_relay_stamp(_msg()) is None


def test_no_pinned_key_ignores_stamps(monkeypatch):
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", "")
    monkeypatch.setattr("topos.relay_stamp._load_public_key_bytes", lambda: None)
    assert verify_relay_stamp(_sign(_msg(), cls=THIRD_PARTY, client_id="x")) is None


def test_wrong_key_is_legacy():
    other = Ed25519PrivateKey.generate()
    assert verify_relay_stamp(_sign(_msg(), cls=THIRD_PARTY, client_id="x", key=other)) is None


def test_tampered_field_is_legacy():
    m = _sign(_msg(), cls=THIRD_PARTY, client_id="chatgpt")
    m[STAMP_FIELD]["cls"] = OWNER_APP  # promote after signing
    assert verify_relay_stamp(m) is None


def test_replay_onto_another_message_is_legacy():
    m1 = _sign(_msg("m1", "query"), cls=THIRD_PARTY, client_id="chatgpt")
    m2 = {"id": "m2", "type": "query", "payload": {}, STAMP_FIELD: m1[STAMP_FIELD]}
    assert verify_relay_stamp(m2) is None
    m3 = {"id": "m1", "type": "delete_database_table", "payload": {}, STAMP_FIELD: m1[STAMP_FIELD]}
    assert verify_relay_stamp(m3) is None  # type binding too


def test_expired_and_overlong_stamps_are_legacy():
    now = time.time()
    assert verify_relay_stamp(
        _sign(_msg(), cls=THIRD_PARTY, client_id="x", iat=now - 300, exp=now - 10)) is None
    assert verify_relay_stamp(
        _sign(_msg(), cls=THIRD_PARTY, client_id="x", iat=now, exp=now + 86400)) is None


def test_unknown_class_is_legacy_not_trusted():
    assert verify_relay_stamp(_sign(_msg(), cls="grantee")) is None
    assert verify_relay_stamp(_sign(_msg(), cls="root")) is None


# ------------------------------------------------- the point of all this
def test_stamped_remote_client_is_elevatable(monkeypatch):
    """The ladder's gap, closed: a RELAY client with a verified stamp carries a
    client_id, so an elevation grant finally applies over the relay."""
    from topos.config.settings import ENGINE_CONFIG_KEY_PACKET_RESOLUTION
    from topos.core.handlers.common import set_engine_config_value
    from topos.mcp_clients import decide_elevation, mint_client_token, request_elevation
    from topos.query import packet_resolution as pr
    from topos.query.packet_resolution import effective_packet_resolution

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    mint_client_token(conn, client_id="chatgpt")
    req = request_elevation(conn, client_id="chatgpt", scope_id="relationships.social")
    decide_elevation(conn, request_id=req["id"], approve=True)
    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": True, "provider": "ollama", "model": "m", "remote_engine_url": False},
    )

    principal = verify_relay_stamp(
        _sign(_msg(), cls=THIRD_PARTY, client_id="chatgpt", acting_user="owner-uuid"))
    info = effective_packet_resolution(
        conn, principal=principal, scope_id="relationships.social",
        disclosure_tier="default_disclosure",
    )
    assert info["effective"] == "facts"
    assert info["reason"].startswith("consent_grant:")


def test_grantee_turn_drops_the_principal(monkeypatch):
    """A stamped grantee must not inherit the owner's elevation for a
    same-named client — grantee turns run pure legacy floor logic."""
    import inspect

    from topos.query import pipeline

    src = inspect.getsource(pipeline)
    anchor = src.index("_principal = current_principal()")
    window = src[anchor:anchor + 400]
    assert "if is_grantee_request:" in window and "_principal = None" in window
