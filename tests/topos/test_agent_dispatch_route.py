"""``POST /api/local/agent/dispatch`` — the agent's one door to the dispatcher (CONTRACTS §1.2, lane L2).

protects: the route forwards ``{type, payload}`` to the node's dispatcher under the request's channel-verified
principal; an ``owner_agent`` gets 403 ``{"error": "owner_agent_not_allowed"}`` for any type off its list
before anything is dispatched; ``owner_app`` keeps today's rights through the same door; any other class is
refused; a 401 from the auth dependency passes through untouched; and the router is mounted on the app.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos.api import agent_dispatch
from topos.auth import resolve_request_principal
from topos.core.handlers.registry import HANDLERS
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, current_principal

OWNER_AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
OWNER = Principal(cls=OWNER_APP, channel="uds")
TPK_CLIENT = Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop")
LEGACY: Optional[Principal] = None

ROUTE = "/api/local/agent/dispatch"

OFF_LIST = (
    "source_sync", "query_live", "create_routine", "signal_list_entities", "permissions_v2_mutate",
    "delete_jsonl_file", "ollama_install", "truth_seed_fact", "mcp_client_list", "no_such_type_at_all",
)


@pytest.fixture()
def door():
    app = FastAPI()
    app.include_router(agent_dispatch.router)

    def _as(principal: Optional[Principal]) -> TestClient:
        app.dependency_overrides[resolve_request_principal] = lambda: principal
        return TestClient(app)

    return _as


@pytest.fixture()
def probe(monkeypatch):
    """Replace one handler with a recorder that also notes the principal it ran under."""
    seen: List[Dict[str, Any]] = []

    def install(msg_type: str, result: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        async def handler(message: Dict[str, Any]) -> Dict[str, Any]:
            seen.append({"type": msg_type, "message": message, "principal": current_principal()})
            if result is not None:
                return {"id": message.get("id"), **result}
            return {"id": message.get("id"), "status": "ok", "payload": {"probe": msg_type, "echo": message.get("payload")}}

        monkeypatch.setitem(HANDLERS, msg_type, handler)
        return seen

    return install


# ------------------------------------------------------------------ owner_agent


@pytest.mark.parametrize("msg_type", OFF_LIST)
def test_off_list_type_is_403_before_dispatch(door, probe, msg_type: str) -> None:
    seen = probe(msg_type) if msg_type in HANDLERS else []

    r = door(OWNER_AGENT).post(ROUTE, json={"type": msg_type, "payload": {"source_id": "imessage"}})

    assert r.status_code == 403, r.text
    assert r.json() == {"error": "owner_agent_not_allowed"}
    assert seen == []


@pytest.mark.parametrize("msg_type", ("query", "aggregate", "get_sources"))
def test_on_list_type_is_forwarded_under_the_request_principal(door, probe, msg_type: str) -> None:
    seen = probe(msg_type)

    r = door(OWNER_AGENT).post(ROUTE, json={"type": msg_type, "payload": {"scope_id": "work_context:read"}})

    assert r.status_code == 200, r.text
    assert len(seen) == 1 and seen[0]["type"] == msg_type
    assert seen[0]["principal"] == OWNER_AGENT                       # the channel's principal, not a payload field
    message = seen[0]["message"]
    assert message["type"] == msg_type and message["payload"] == {"scope_id": "work_context:read"}
    assert isinstance(message["id"], str) and message["id"]
    assert r.json() == {"id": message["id"], "status": "ok",
                        "payload": {"probe": msg_type, "echo": {"scope_id": "work_context:read"}}}


def test_the_type_is_normalised_like_the_dispatcher_normalises_it(door, probe) -> None:
    seen = probe("query")
    r = door(OWNER_AGENT).post(ROUTE, json={"type": "  Query ", "payload": {}})
    assert r.status_code == 200, r.text
    assert seen[0]["message"]["type"] == "query"


# ------------------------------------------------------------------ owner_app and others


def test_owner_app_keeps_todays_rights_through_the_same_door(door, probe) -> None:
    """The allowlist is the agent's, not the door's: the owner's own socket process reaches a routine door."""
    seen = probe("create_routine")

    r = door(OWNER).post(ROUTE, json={"type": "create_routine", "payload": {"user_id": "u-owner"}})

    assert r.status_code == 200, r.text
    assert len(seen) == 1 and seen[0]["principal"] == OWNER


@pytest.mark.parametrize("who", ("third_party", "legacy_none"))
def test_any_other_class_is_refused(door, probe, who: str) -> None:
    seen = probe("query")

    r = door({"third_party": TPK_CLIENT, "legacy_none": LEGACY}[who]).post(ROUTE, json={"type": "query", "payload": {}})

    assert r.status_code == 403, r.text
    assert r.json() == {"error": "owner_mode_required"}
    assert seen == []


def test_401_from_auth_passes_through() -> None:
    """The real dependency on TCP with no bearer: the auth's own 401, untouched."""
    app = FastAPI()
    app.include_router(agent_dispatch.router)

    r = TestClient(app).post(ROUTE, json={"type": "query", "payload": {}})

    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "Missing authorization"}


# ------------------------------------------------------------------ body and errors


def test_missing_type_is_400(door, probe) -> None:
    r = door(OWNER_AGENT).post(ROUTE, json={"payload": {}})
    assert r.status_code == 400 and r.json() == {"error": "type_required"}


def test_payload_must_be_an_object(door, probe) -> None:
    probe("query")
    r = door(OWNER_AGENT).post(ROUTE, json={"type": "query", "payload": ["not", "an", "object"]})
    assert r.status_code == 400 and r.json() == {"error": "payload_must_be_an_object"}


def test_a_missing_payload_is_an_empty_object(door, probe) -> None:
    seen = probe("get_sources")
    r = door(OWNER_AGENT).post(ROUTE, json={"type": "get_sources"})
    assert r.status_code == 200, r.text
    assert seen[0]["message"]["payload"] == {}


def test_a_dispatcher_refusal_becomes_its_http_status(door, probe) -> None:
    probe("create_routine", result={"status": "error", "code": 403, "error": "owner_confirmation_required",
                                    "message": "This change needs the Topos app."})
    r = door(OWNER).post(ROUTE, json={"type": "create_routine", "payload": {}})
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"] == "owner_confirmation_required" and body["message"] == "This change needs the Topos app."


def test_a_handler_error_without_a_code_is_400(door, probe) -> None:
    probe("query", result={"status": "error", "error": "scope_id required"})
    r = door(OWNER_AGENT).post(ROUTE, json={"type": "query", "payload": {}})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "scope_id required"


# ------------------------------------------------------------------ mounted


def test_the_router_is_mounted_on_the_app() -> None:
    from topos.app import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert ROUTE in paths, sorted(p for p in paths if p and "/api/local" in p)


def test_the_agent_label_refusal_handler_is_registered_once_l1_lands() -> None:
    """L1's ``AgentLabelRefused`` (auth.py) answers 401 with ``{"error", "message"}`` at top level only once app.py
    registers its handler; that registration is L2's line. Skips until L1's names are in this tree."""
    from topos import auth

    if not hasattr(auth, "AgentLabelRefused"):
        pytest.skip("L1's AgentLabelRefused is not in this tree yet (onramp/l1-access)")
    from topos.app import app

    assert app.exception_handlers.get(auth.AgentLabelRefused) is auth.agent_label_refusal_handler


def test_the_route_keys_on_the_resolved_principal_not_the_bearer_check() -> None:
    """A ``require_api_key``-only route would 401 a bad label but serve a valid one with no principal (L1's note):
    this route's only dependency is ``resolve_request_principal``."""
    import inspect

    from fastapi.params import Depends as DependsParam

    sig = inspect.signature(agent_dispatch.agent_dispatch)
    dependencies = [p.default.dependency for p in sig.parameters.values() if isinstance(p.default, DependsParam)]
    assert dependencies == [resolve_request_principal]
    assert not agent_dispatch.router.dependencies      # no router-level require_api_key either
