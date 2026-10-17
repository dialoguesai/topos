"""The owner socket's principal step (L1 verifier finding, 15 Oct 2026; lane L2).

protects: every http request on the socket resolves its channel-verified principal BEFORE the route, and the route
runs under it — so a labelled agent on a ``require_api_key``-only route is never served with no principal (which
``install_authority.principal_may_install`` and the tier heuristic read as the legacy owner). A labelled agent
reaches only the §1.2 route list (``POST /api/local/agent/dispatch``, ``GET /sources``, ``GET /sources/{id}/settings``,
``GET /v1/shell/status``, ``GET /api/local/owner/scope``) and is 403 ``{"error": "owner_agent_not_allowed"}``
everywhere else, before any auth of the route's own; an invalid label is the auth's 401 with the ``error`` key at
top level, on every route. No label is ``owner_app``, as before.

The label itself is L1's (``auth.resolve_request_principal`` reads ``X-Topos-Agent`` in onramp/l1-access); here a
stub with the same contract stands in for it, so these guards prove the socket wrapper's plumbing in this tree and
the real resolution at integration.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest
from fastapi import HTTPException

import topos.app  # noqa: F401  (import the routers BEFORE any monkeypatch of auth below: a router imported while the
#                                stub is in place would capture the stub in Depends(...) for the rest of the session)
from topos import auth, owner_door_routes as census, uds
from topos.principal import OWNER_APP, Principal, current_principal

LABEL = "tpa_claude-code." + "a" * 40
AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
DISCONNECTED = {"error": "agent_disconnected", "message": "This agent was disconnected from Topos."}


@pytest.fixture()
def labelled(monkeypatch):
    """L1's contract for the socket: a valid label mints owner_agent, an invalid one is the 401 whose detail is the
    error object, no label is owner_app."""
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")

    def resolve(request=None, credentials=None):
        presented = request.headers.get("x-topos-agent", "") if request is not None else ""
        if not presented:
            return Principal(cls=OWNER_APP, channel="uds")
        if presented == LABEL:
            return AGENT
        raise HTTPException(status_code=401, detail=dict(DISCONNECTED))

    monkeypatch.setattr(auth, "resolve_request_principal", resolve)


def _headers(label: Optional[str]) -> List[Tuple[bytes, bytes]]:
    return [(b"x-topos-agent", label.encode())] if label else []


async def _call(method: str, path: str, *, label: Optional[str] = None, body: bytes = b"{}"):
    seen: Dict[str, Any] = {}

    async def inner(scope, receive, send):
        seen["principal"] = current_principal()
        seen["path"] = scope["path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: List[Dict[str, Any]] = []
    queue = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": _headers(label), "client": ("uds", 0)}
    await uds.UDSChannelApp(inner)(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, out, seen


@pytest.mark.asyncio
async def test_a_labelled_agent_on_get_sources_is_served_as_owner_agent(labelled) -> None:
    status, _, seen = await _call("GET", "/sources", label=LABEL)
    assert status == 200
    assert seen["principal"] == AGENT, "the route runs under the agent's principal, never under None"


@pytest.mark.asyncio
@pytest.mark.parametrize("method, template", census.OWNER_AGENT_ROUTES)
async def test_each_route_on_the_list_is_reachable_for_the_agent(labelled, method: str, template: str) -> None:
    status, _, seen = await _call(method, template.replace("{source_id}", "imessage"), label=LABEL)
    assert status == 200 and seen["principal"] == AGENT


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path", [
    ("POST", "/sources/x/sync"), ("PUT", "/sources/x/settings"), ("DELETE", "/v1/source-install"),
    ("POST", "/v1/source-scrub"), ("GET", "/v1/signal/entities"), ("GET", "/health"), ("POST", "/v1/query"),
    ("POST", "/api/local/list_database_tables"), ("GET", "/api/local/owner/sources/claude_code_sessions/preview"),
])
async def test_the_agent_is_refused_everywhere_else_before_the_route(labelled, method: str, path: str) -> None:
    status, out, seen = await _call(method, path, label=LABEL)
    assert status == 403 and json.loads(out) == {"error": "owner_agent_not_allowed"}
    assert seen == {}


@pytest.mark.asyncio
async def test_the_agents_refusal_comes_before_the_door_gate(labelled) -> None:
    """On a dangerous route the agent hears its own code, not owner_confirmation_required."""
    status, out, _ = await _call("POST", "/v1/source-scrub", label=LABEL)
    assert status == 403 and json.loads(out)["error"] == "owner_agent_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path", [("GET", "/sources"), ("DELETE", "/v1/source-install"), ("GET", "/health"),
                                          ("POST", "/api/local/agent/dispatch")])
async def test_an_invalid_label_is_the_auths_401_with_the_error_key_at_top_level(labelled, method, path) -> None:
    status, out, seen = await _call(method, path, label="tpa_claude-code." + "f" * 40)
    assert status == 401, (status, out)
    assert json.loads(out) == DISCONNECTED
    assert seen == {}


@pytest.mark.asyncio
async def test_no_label_is_owner_app_and_the_route_sees_it(labelled) -> None:
    """The require_api_key-only case: the route runs under owner_app, not None."""
    status, _, seen = await _call("GET", "/sources")
    assert status == 200 and seen["principal"] == Principal(cls=OWNER_APP, channel="uds")


@pytest.mark.asyncio
async def test_the_real_resolver_on_the_socket_is_owner_app_and_visible_to_the_route(monkeypatch) -> None:
    """Without the stub: this tree's auth mints owner_app on the socket; the wrapper sets it for the route."""
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    status, _, seen = await _call("GET", "/sources")
    assert status == 200 and seen["principal"] == Principal(cls=OWNER_APP, channel="uds")
    assert current_principal() is None, "reset after the request"


@pytest.mark.asyncio
async def test_a_plain_http_exception_from_auth_keeps_fastapis_body_shape(monkeypatch) -> None:
    def resolve(request=None, credentials=None):
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    monkeypatch.setattr(auth, "resolve_request_principal", resolve)
    status, out, seen = await _call("GET", "/sources")
    assert status == 401 and json.loads(out) == {"detail": "Invalid authorization token"} and seen == {}


@pytest.mark.asyncio
async def test_the_real_app_serves_the_agents_reads_and_refuses_its_writes(labelled) -> None:
    from httpx import ASGITransport, AsyncClient

    from topos.app import app

    async with AsyncClient(transport=ASGITransport(app=uds.UDSChannelApp(app)), base_url="http://topos-owner") as c:
        sources = await c.get("/sources", headers={"X-Topos-Agent": LABEL})
        assert sources.status_code not in (401, 403), sources.text
        sync = await c.post("/sources/imessage/sync", headers={"X-Topos-Agent": LABEL}, json={})
        assert sync.status_code == 403 and sync.json() == {"error": "owner_agent_not_allowed"}
        bad = await c.get("/sources", headers={"X-Topos-Agent": "tpa_claude-code." + "0" * 40})
        assert bad.status_code == 401 and bad.json() == DISCONNECTED
