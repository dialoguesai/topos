"""Who may use the truth door: verify_claim, truth_prompts, truth_seed_fact.

protects: the three truth message types answer only principals the channel
verified — never a payload field (`app_id` is caller-asserted and gates
nothing). An enrolled third-party client (tpk_), the shared TOPOS_KEY and the
owner key presented over TCP all resolve to THIRD_PARTY at the HTTP door, and
none of them may probe the owner's fun facts through verify_claim, list their
"ask me" topics, or author an owner-stated fact through truth_seed_fact. The
owner socket still reaches all three; the CP truth door (/v1/truth/*: owner
credential x TRUTH_APP_ALLOWLIST, relayed unstamped) still reaches the two
reads; the write needs the owner class on every channel; and a client the owner
names on TOPOS_TRUTH_CLIENT_ALLOWLIST may use the two reads, never the write.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import topos.core.handlers as hub
import topos.core.state as state
from topos.api.local_mcp import router
from topos.core.handlers import handle_control_plane_request
from topos.mcp_clients import mint_client_token
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, THIRD_PARTY, Principal
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload, verify_relay_stamp
from topos.uds import UDSChannelApp

SHARED_KEY = "shared-key"
OWNER_KEY = "owner-key"
TRUTH_TYPES = ("verify_claim", "truth_prompts", "truth_seed_fact")

# Route bodies exactly as truth-mirror sends them to the local door.
ROUTE_BODIES = {
    "verify_claim": {"statement": "I have never played chess", "app_id": "truth-mirror"},
    "truth_prompts": {"app_id": "truth-mirror", "limit": 5},
    "truth_seed_fact": {"predicate": "favorite_food", "value": "grilled cheese",
                        "app_id": "truth-mirror"},
}
# Relay payloads exactly as control_plane/routes/truth.py (and the local routes)
# build them.
MESSAGE_PAYLOADS = {
    "verify_claim": {"statement": "I have never played chess", "mode": "fun",
                     "caller_app_id": "truth-mirror"},
    "truth_prompts": {"mode": "fun", "caller_app_id": "truth-mirror", "limit": 5},
    "truth_seed_fact": {"mode": "fun", "caller_app_id": "truth-mirror",
                        "predicate": "favorite_food", "value": "grilled cheese"},
}
HTTP_REFUSAL = (403, {"detail": "owner_mode_required"})
HANDLER_REFUSAL = ("error", 403, "owner_mode_required")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.features.facts.store import FactStore
    from topos.storage.db.migrations import apply_all_migrations

    connection = sqlite3.connect(str(tmp_path / "truth-door.sqlite"), check_same_thread=False)
    apply_all_migrations(connection)
    # One owner fun fact: what the verify_claim oracle and the prompt list expose.
    FactStore(connection).assert_fact(
        subject_entity_id="self-1",
        predicate="enjoys",
        object_value="playing chess",
        dimension="interests",
        confidence=0.8,
        source_refs=[],
        disclosure="scoped",
    )
    monkeypatch.setattr(hub, "get_db_connection", lambda: connection)  # handlers
    monkeypatch.setattr(state, "get_db_connection", lambda: connection)  # tpk verification
    monkeypatch.setattr("topos.config.settings.settings.topos_key", SHARED_KEY, raising=False)
    monkeypatch.setattr("topos.config.settings.settings.topos_owner_key", OWNER_KEY, raising=False)
    monkeypatch.delenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", raising=False)  # the default: no client
    monkeypatch.setenv("TOPOS_TRUTH_LLM", "off")
    yield connection
    connection.close()


@pytest.fixture()
def app():
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.fixture()
def tcp(app):
    """The loopback HTTP door: a bearer is required and decides the class."""
    return TestClient(app)


@pytest.fixture()
def owner_socket(app):
    """The owner socket: the same app wrapped exactly as start_uds_server wraps it."""
    return TestClient(UDSChannelApp(app))


@pytest.fixture()
def bearers(conn):
    return {
        "tpk_enrolled_client": mint_client_token(conn, client_id="claude-desktop")["token"],
        "shared_topos_key": SHARED_KEY,
        "owner_key_over_tcp": OWNER_KEY,
    }


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _message(msg_type: str) -> dict:
    return {"id": f"req-{msg_type}", "type": msg_type, "payload": dict(MESSAGE_PAYLOADS[msg_type])}


def _seeded(conn) -> list:
    """Owner-stated facts the seed payload would have written."""
    rows = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchall()
    found = []
    for (raw,) in rows:
        payload = json.loads(raw)
        if payload.get("predicate") == "favorite_food":
            found.append((payload["predicate"], payload["object_value"], payload.get("asserted_by")))
    return found


def _handler_verdict(out: dict, conn) -> tuple:
    return (out.get("status"), out.get("code"), out.get("error"), _seeded(conn))


# ------------------------------------------------------------ the open door
@pytest.mark.parametrize("credential", ["tpk_enrolled_client", "shared_topos_key", "owner_key_over_tcp"])
@pytest.mark.parametrize("route", TRUTH_TYPES)
def test_tcp_bearers_are_refused_on_every_truth_route(conn, tcp, bearers, credential, route):
    response = tcp.post(f"/api/local/{route}", json=ROUTE_BODIES[route], headers=_auth(bearers[credential]))
    # One tuple, so a failure shows what the caller got back and, for the write,
    # the owner-stated fact it left behind.
    assert (response.status_code, response.json(), _seeded(conn)) == (*HTTP_REFUSAL, [])


def test_legacy_mode_shared_key_is_refused_too(conn, tcp, monkeypatch):
    """No owner key (a node that could not persist one): the shared key resolves
    no principal at all. Every client holds that key, so "legacy" is not the
    owner either — the owner socket works without any key in this mode."""
    monkeypatch.setattr("topos.config.settings.settings.topos_owner_key", None, raising=False)
    for route in TRUTH_TYPES:
        response = tcp.post(f"/api/local/{route}", json=ROUTE_BODIES[route], headers=_auth(SHARED_KEY))
        assert (route, response.status_code, response.json()) == (route, *HTTP_REFUSAL)
    assert _seeded(conn) == []


NON_OWNER_PRINCIPALS = {
    "tpk_enrolled_client": Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop"),
    "shared_topos_key": Principal(cls=THIRD_PARTY, channel="local_http"),
    "relay_stamped_third_party": Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="chatgpt"),
    "routine_automation": Principal(cls="owner_automation", channel="cp_relay",
                                    client_id="routine_executor"),
    "legacy_no_principal": None,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("who", sorted(NON_OWNER_PRINCIPALS))
@pytest.mark.parametrize("msg_type", TRUTH_TYPES)
async def test_handlers_refuse_non_owner_principals(conn, who, msg_type):
    """The dispatch layer, under every entry point: a surface that forwards these
    types without the local routes' gate still meets the refusal."""
    out = await handle_control_plane_request(_message(msg_type), principal=NON_OWNER_PRINCIPALS[who])
    assert _handler_verdict(out, conn) == (*HANDLER_REFUSAL, [])


@pytest.mark.asyncio
async def test_unstamped_relay_cannot_author_an_owner_stated_fact(conn):
    """No CP route forwards truth_seed_fact, and an unstamped relay proves only
    that the CP sent it, not that the owner did."""
    out = await handle_control_plane_request(_message("truth_seed_fact"), principal=RELAY_PRINCIPAL)
    assert _handler_verdict(out, conn) == (*HANDLER_REFUSAL, [])


def test_only_the_write_is_marked_owner_only():
    """The reads stay unmarked on purpose: the CP truth door relays them
    unstamped (CP_RELAY), and a dispatcher that enforces owner_only as
    owner_app-only would cut off that production lane."""
    from topos.core.handlers.registry import OWNER_ONLY_MESSAGE_TYPES

    assert {t for t in TRUTH_TYPES if t in OWNER_ONLY_MESSAGE_TYPES} == {"truth_seed_fact"}


# ------------------------------------------------------ the owner's own lanes
def test_owner_socket_reaches_all_three_without_a_bearer(conn, owner_socket):
    check = owner_socket.post("/api/local/verify_claim", json=ROUTE_BODIES["verify_claim"])
    assert check.status_code == 200, check.text
    assert check.json()["lanes"]["self"]["stance"] == "contradicts"

    prompts = owner_socket.post("/api/local/truth_prompts", json=ROUTE_BODIES["truth_prompts"])
    assert prompts.status_code == 200, prompts.text
    assert prompts.json()["prompts"], "the chess fact should seed a prompt"

    seed = owner_socket.post("/api/local/truth_seed_fact", json=ROUTE_BODIES["truth_seed_fact"])
    assert seed.status_code == 200, seed.text
    assert seed.json()["accepted"] is True
    assert _seeded(conn) == [("favorite_food", "grilled cheese", "owner")]


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", ["verify_claim", "truth_prompts"])
async def test_cp_truth_door_relay_still_reaches_the_reads(conn, msg_type):
    """The production lane: control_plane/routes/truth.py checks an owner
    credential and TRUTH_APP_ALLOWLIST, then relays unstamped; the node resolves
    it the way app.py's _relay_dispatch does."""
    message = _message(msg_type)
    principal = verify_relay_stamp(message) or RELAY_PRINCIPAL
    out = await handle_control_plane_request(message, principal=principal)
    assert out["status"] == "ok", out
    if msg_type == "verify_claim":
        assert out["payload"]["lanes"]["self"]["stance"] == "contradicts"
    else:
        assert out["payload"]["prompts"]


@pytest.mark.asyncio
async def test_owner_stamped_relay_may_seed(conn, monkeypatch, tmp_path):
    """A verified owner_app stamp is the relay half of the owner class."""
    import topos.relay_stamp as relay_stamp

    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY",
                       base64.b64encode(key.public_key().public_bytes_raw()).decode())
    monkeypatch.setattr(relay_stamp, "_PINNED_KEY_PATH", str(tmp_path / "unused.pub"))
    message = _message("truth_seed_fact")
    now = time.time()
    stamp = {"v": 1, "cls": OWNER_APP, "client_id": "topos-app", "acting_user": "",
             "iat": now, "exp": now + 120}
    signed = canonical_signing_payload(stamp, msg_id=message["id"], msg_type=message["type"])
    stamp["sig"] = base64.b64encode(key.sign(signed)).decode()
    message[STAMP_FIELD] = stamp

    principal = verify_relay_stamp(message) or RELAY_PRINCIPAL
    assert principal.cls == OWNER_APP
    out = await handle_control_plane_request(message, principal=principal)
    assert out["status"] == "ok", out
    assert out["payload"]["accepted"] is True
    assert _seeded(conn) == [("favorite_food", "grilled cheese", "owner")]


# ------------------------------------------------- the owner's explicit list
def test_allowlisted_client_may_use_the_reads_but_never_the_write(conn, tcp, bearers, monkeypatch):
    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", " Truth-Mirror , cursor ")
    token = mint_client_token(conn, client_id="truth-mirror")["token"]

    check = tcp.post("/api/local/verify_claim", json=ROUTE_BODIES["verify_claim"], headers=_auth(token))
    assert check.status_code == 200, check.text
    assert check.json()["lanes"]["self"]["stance"] == "contradicts"
    prompts = tcp.post("/api/local/truth_prompts", json=ROUTE_BODIES["truth_prompts"], headers=_auth(token))
    assert prompts.status_code == 200 and prompts.json()["prompts"], prompts.text

    seed = tcp.post("/api/local/truth_seed_fact", json=ROUTE_BODIES["truth_seed_fact"], headers=_auth(token))
    assert (seed.status_code, seed.json(), _seeded(conn)) == (*HTTP_REFUSAL, [])

    # The list names enrolled clients, not credentials: another enrolled client
    # and the unnamed shared key stay out, and so does the owner key over TCP.
    for credential in ("tpk_enrolled_client", "shared_topos_key", "owner_key_over_tcp"):
        response = tcp.post("/api/local/verify_claim", json=ROUTE_BODIES["verify_claim"],
                            headers=_auth(bearers[credential]))
        assert (credential, response.status_code) == (credential, 403)


@pytest.mark.asyncio
async def test_allowlist_governs_the_local_door_only(conn, monkeypatch):
    """A relay-stamped third party that shares an allowlisted name is not that
    local client: the CP door keeps its own list."""
    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", "truth-mirror")
    relayed = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="truth-mirror")
    out = await handle_control_plane_request(_message("verify_claim"), principal=relayed)
    assert _handler_verdict(out, conn) == (*HANDLER_REFUSAL, [])
