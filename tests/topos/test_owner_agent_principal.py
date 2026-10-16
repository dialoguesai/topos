"""The ``owner_agent`` class: its label, its door (AI-tool on-ramp, CONTRACTS §1.1; lane L1).

protects: an approved coding agent on the owner socket is a class of its own, never ``owner_app``; it is
minted only on the socket and only by a valid, unrevoked ``tpa_`` label in ``X-Topos-Agent``; a revoked label
is refused on the very next request with no cache between; an invalid label never falls back to ``owner_app``;
a label over any TCP channel is refused and never resolves to ``third_party``; a ``tpk_`` client token is never
an agent label and an agent row is never a ``tpk_`` client; the agent id namespace (``agent-*``) is kept apart
from Connected-apps ids; the class is not one a relay stamp may mint.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import topos.core.state as state
from topos.auth import require_api_key, resolve_request_principal
from topos.config.settings import settings
from topos.mcp_clients import list_clients, mint_client_token, revoke_client, verify_client_token
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.uds import _transport

pytestmark = pytest.mark.public

AGENT_DISCONNECTED = {"error": "agent_disconnected", "message": "This agent was disconnected from Topos."}
HEADER = "X-Topos-Agent"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


@pytest.fixture()
def door(conn, monkeypatch):
    """The node's HTTP door over this scratch registry, with both keys minted (owner mode on)."""
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(settings, "topos_key", "legacy-key", raising=False)
    monkeypatch.setattr(settings, "topos_owner_key", "owner-key", raising=False)
    return conn


def _request(headers: dict | None = None, *, client=("127.0.0.1", 40000)) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/local/agent/dispatch",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


def _creds(token: str | None) -> HTTPAuthorizationCredentials | None:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None


@contextmanager
def socket():
    token = _transport.set("uds")
    try:
        yield
    finally:
        _transport.reset(token)


def _mint(conn, client="claude-code", **kw):
    from topos.mcp_clients import mint_agent_label

    return mint_agent_label(conn, client=client, **kw)


# ---------------------------------------------------------------- the class
def test_the_class_is_its_own_word_and_never_owner_app():
    from topos.principal import OWNER_AGENT

    assert OWNER_AGENT == "owner_agent"
    assert OWNER_AGENT != OWNER_APP and OWNER_AGENT != THIRD_PARTY


def test_the_class_is_not_one_a_relay_stamp_may_mint():
    """rev-1 security 19: minted on the socket only, so a verified stamp cannot name it."""
    from topos.principal import OWNER_AGENT
    from topos.relay_stamp import ALLOWED_CLASSES

    assert OWNER_AGENT not in ALLOWED_CLASSES


# ---------------------------------------------------------------- the label
def test_mint_agent_label_shape_row_and_hash(conn):
    row = _mint(conn)
    label = row["label"]
    assert re.fullmatch(r"tpa_claude-code\.[0-9a-f]{40}", label), label
    assert not label.startswith("tpk_")
    assert row["client_id"] == "agent-claude-code"
    assert row["principal_class"] == "owner_agent"
    assert row["display_name"] == "Claude Code"
    stored = conn.execute("SELECT token_hash, principal_class FROM mcp_clients WHERE client_id = 'agent-claude-code'").fetchone()
    assert stored == (hashlib.sha256(label.encode("utf-8")).hexdigest(), "owner_agent")


def test_verify_agent_label_roundtrip_and_never_a_tpk(conn):
    from topos.mcp_clients import verify_agent_label

    label = _mint(conn)["label"]
    row = verify_agent_label(conn, label)
    assert row is not None and row["client_id"] == "agent-claude-code" and row["principal_class"] == "owner_agent"
    tpk = mint_client_token(conn, client_id="cursor")["token"]
    assert verify_agent_label(conn, tpk) is None          # a client token is not a label
    assert verify_agent_label(conn, "tpa_claude-code.") is None
    assert verify_agent_label(conn, "tpa_claude-code." + "0" * 40) is None
    assert verify_agent_label(conn, "") is None


def test_verify_client_token_never_returns_an_agent_row(conn):
    """The class check, not only the hash: plant a tpk-shaped secret on the agent row and the tpk path must
    still refuse it — an agent label presented as a client token must not become a named third party
    (rev-1 security 12)."""
    from topos.mcp_clients import _hash_token

    _mint(conn)
    forged = "tpk_agent-claude-code." + "a" * 40
    conn.execute("UPDATE mcp_clients SET token_hash = ? WHERE client_id = 'agent-claude-code'", (_hash_token(forged),))
    conn.commit()
    assert verify_client_token(conn, forged) is None


def test_reminting_a_revoked_agent_is_refused_unless_approved(conn):
    from topos.mcp_clients import revoke_agent, verify_agent_label

    first = _mint(conn)["label"]
    assert revoke_agent(conn, client="claude-code") is True
    assert verify_agent_label(conn, first) is None
    assert revoke_agent(conn, client="claude-code") is False      # idempotent, a tombstone
    with pytest.raises(ValueError):
        _mint(conn)
    assert verify_agent_label(conn, first) is None                # the refusal changed nothing
    second = _mint(conn, approved=True)["label"]
    assert verify_agent_label(conn, second)["client_id"] == "agent-claude-code"
    assert verify_agent_label(conn, first) is None


def test_list_agents_names_the_agent_and_never_a_hash(conn):
    from topos.mcp_clients import list_agents, revoke_agent

    _mint(conn)
    _mint(conn, client="codex")
    revoke_agent(conn, client="codex")
    rows = list_agents(conn)
    assert [r["client"] for r in rows] == ["claude-code", "codex"]
    assert [r["client_id"] for r in rows] == ["agent-claude-code", "agent-codex"]
    assert [bool(r["revoked_at"]) for r in rows] == [False, True]
    for r in rows:
        assert "token_hash" not in r and "token" not in r and "label" not in r


def test_agent_rows_are_not_connected_apps_rows(conn):
    """Namespaces apart: Connected apps never lists, revokes or mints an ``agent-*`` row."""
    _mint(conn)
    mint_client_token(conn, client_id="cursor")
    assert [r["client_id"] for r in list_clients(conn)] == ["cursor"]
    assert revoke_client(conn, "agent-claude-code") is False
    with pytest.raises(ValueError):
        mint_client_token(conn, client_id="agent-gemini")
    row = conn.execute("SELECT revoked_at FROM mcp_clients WHERE client_id = 'agent-claude-code'").fetchone()
    assert row == (None,)


# ---------------------------------------------------------------- the door
def test_socket_with_a_valid_label_mints_owner_agent_with_its_client_id(door):
    label = _mint(door)["label"]
    with socket():
        p = resolve_request_principal(_request({HEADER: label}), None)
    assert p == Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")


def test_socket_without_a_label_is_owner_app_unchanged(door):
    _mint(door)
    with socket():
        p = resolve_request_principal(_request(), None)
        require_api_key(_request(), None)  # no raise, as before
    assert p == Principal(cls=OWNER_APP, channel="uds")


@pytest.mark.parametrize("presented", ["tpa_claude-code." + "f" * 40, "nonsense", "tpa_"])
def test_socket_with_an_invalid_label_is_401_never_owner_app(door, presented):
    _mint(door)
    with socket():
        with pytest.raises(HTTPException) as exc:
            resolve_request_principal(_request({HEADER: presented}), None)
        assert exc.value.status_code == 401
        assert exc.value.detail == AGENT_DISCONNECTED
        # The plain-auth door refuses it too: a bad label must not ride a require_api_key-only route as the owner.
        with pytest.raises(HTTPException) as exc2:
            require_api_key(_request({HEADER: presented}), None)
        assert exc2.value.status_code == 401 and exc2.value.detail == AGENT_DISCONNECTED


def test_a_revoked_label_is_refused_on_the_very_next_request(door):
    from topos.mcp_clients import revoke_agent

    label = _mint(door)["label"]
    with socket():
        before = resolve_request_principal(_request({HEADER: label}), None)
        assert before.cls == "owner_agent"
        assert revoke_agent(door, client="claude-code") is True
        with pytest.raises(HTTPException) as exc:
            resolve_request_principal(_request({HEADER: label}), None)
    assert exc.value.status_code == 401 and exc.value.detail == AGENT_DISCONNECTED


def test_a_tpk_client_token_presented_as_an_agent_label_is_refused(door):
    tpk = mint_client_token(door, client_id="cursor")["token"]
    with socket():
        with pytest.raises(HTTPException) as exc:
            resolve_request_principal(_request({HEADER: tpk}), None)
    assert exc.value.status_code == 401 and exc.value.detail == AGENT_DISCONNECTED


@pytest.mark.parametrize("bearer", [None, "owner-key", "legacy-key"])
@pytest.mark.parametrize("peer", [("127.0.0.1", 1), ("10.0.0.7", 1)])
def test_a_label_over_tcp_is_401_agent_label_over_tcp_never_third_party(door, bearer, peer):
    label = _mint(door)["label"]
    with pytest.raises(HTTPException) as exc:
        resolve_request_principal(_request({HEADER: label}, client=peer), _creds(bearer))
    assert exc.value.status_code == 401
    assert exc.value.detail["error"] == "agent_label_over_tcp"
    with pytest.raises(HTTPException) as exc2:
        require_api_key(_request({HEADER: label}, client=peer), _creds(bearer))
    assert exc2.value.status_code == 401 and exc2.value.detail["error"] == "agent_label_over_tcp"


def test_tcp_without_a_label_is_byte_for_byte_as_before(door):
    _mint(door)
    assert resolve_request_principal(_request(), _creds("owner-key")) == Principal(cls=THIRD_PARTY, channel="local_http")
    assert resolve_request_principal(_request(), _creds("legacy-key")) == Principal(cls=THIRD_PARTY, channel="local_http")
    tpk = mint_client_token(door, client_id="cursor")["token"]
    assert resolve_request_principal(_request(), _creds(tpk)) == Principal(cls=THIRD_PARTY, channel="local_http", client_id="cursor")
    with pytest.raises(HTTPException) as exc:
        resolve_request_principal(_request(), _creds("wrong"))
    assert exc.value.status_code == 401
