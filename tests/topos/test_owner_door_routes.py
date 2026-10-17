"""P0.11b: the dangerous-door split at the SOCKET layer, for the HTTP routes that never reach the dispatcher.

protects: on the real owner socket, a mutating request (POST/PUT/PATCH/DELETE) to a route the census names as
dangerous is refused 403 ``{"error": "owner_confirmation_required", "message": "This change needs the Topos app."}``
BEFORE the route runs, unless the peer is an attested app, the dev proxy on a node that allows it, or presents a live
owner-confirmation token in ``X-Topos-Owner-Confirm``. The census is complete (every mounted mutating route and every
``@router.post/put/patch/delete`` decorator under ``topos/api`` has an entry; an unlisted one is refused at runtime
and fails the test: fail closed both ways). Carve-outs that must pass: the transcript-source doors, the agent's
dispatch route (gated inside), ``PUT /sources/{id}/settings`` for enabled/posture/exclude_spam only, and every GET.
"""

from __future__ import annotations

import ast
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest

from topos import owner_door_routes as census, uds

REFUSAL = {"error": "owner_confirmation_required", "message": "This change needs the Topos app."}
TRANSCRIPTS = ("claude_code_sessions", "codex_sessions", "cursor_sessions")
API_DIR = Path(uds.__file__).resolve().parent / "api"
APP_PY = Path(uds.__file__).resolve().parent / "app.py"


# ----------------------------------------------------------------------------- harness


def _concrete(template: str) -> str:
    """A path that matches the template: ``{x}`` -> ``x1``, ``{a|b}`` -> ``a``, trailing ``/*`` -> ``/tail``."""
    path = re.sub(r"\{([^}|]+)\|[^}]*\}", r"\1", template)
    path = re.sub(r"\{[^}]+\}", "x1", path)
    return path[:-2] + "/tail" if path.endswith("/*") else path


async def _call(method: str, path: str, *, headers: Iterable[Tuple[bytes, bytes]] = (), body: bytes = b"",
                peer_pid: int = 0) -> Tuple[Optional[int], bytes, Dict[str, Any]]:
    """One request through the socket wrapper into a probe app; (status, response body, what the probe saw)."""
    reached: Dict[str, Any] = {}

    async def inner(scope, receive, send):
        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        reached["path"] = scope["path"]
        reached["body"] = b"".join(chunks)
        reached["peer"] = uds.current_socket_peer()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: List[Dict[str, Any]] = []
    queue = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": list(headers), "client": ("uds", peer_pid)}
    await uds.UDSChannelApp(inner)(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, out, reached


def _refused(result) -> None:
    status, out, reached = result
    assert status == 403, (status, out)
    assert json.loads(out) == REFUSAL
    assert reached == {}, "the route ran before the gate refused"


def _served(result) -> Dict[str, Any]:
    status, out, reached = result
    assert status == 200 and out == b"ok", (status, out)
    assert "path" in reached
    return reached


def _rules(verdict: str) -> List[census.RouteRule]:
    return [r for r in census.CENSUS if r.verdict == verdict]


def _ids(rules: Iterable[census.RouteRule]) -> List[str]:
    return [f"{r.method} {r.template}" for r in rules]


@pytest.fixture()
def socket_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    for name in ("TOPOS_OWNER_DOOR_TEAM_IDS", "TOPOS_UDS_TEAM_IDS", "TOPOS_DEV_PROXY_ALLOWED"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _token_header() -> List[Tuple[bytes, bytes]]:
    return [(b"x-topos-owner-confirm", uds.write_owner_confirmation().encode())]


DEV = [(b"x-topos-dev-proxy", b"1")]


# ----------------------------------------------------------------------------- the census is complete


def _mounted_mutating_routes() -> List[Tuple[str, str]]:
    from topos.app import app

    out = []
    for route in app.routes:
        methods = (getattr(route, "methods", None) or set()) & census.MUTATING_METHODS
        for method in sorted(methods):
            out.append((method, route.path))
    return out


def test_every_mounted_mutating_route_is_in_the_census() -> None:
    routes = _mounted_mutating_routes()
    assert len(routes) >= 140, len(routes)
    unknown = sorted(f"{m} {p}" for m, p in routes if census.lookup(m, p).verdict == census.UNKNOWN)
    assert not unknown, unknown


def _include_prefixes() -> Dict[str, str]:
    """module alias -> include prefix, read from app.py's include_router lines."""
    text = APP_PY.read_text(encoding="utf-8")
    aliases = dict(re.findall(r"^\s+(\w+) as (\w+),$", text, re.M))          # module -> alias
    prefixes = dict(re.findall(r'include_router\((\w+)\.router, prefix="([^"]+)"\)', text))
    return {module: prefixes.get(alias, "") for module, alias in aliases.items()}


def _decorated_mutating_routes() -> List[Tuple[str, str, str]]:
    """(method, full path, file) for every ``@router.<mutating>("...")`` under topos/api."""
    includes = _include_prefixes()
    out = []
    for file in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        router_prefix = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "APIRouter":
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        router_prefix = str(kw.value.value)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                method = dec.func.attr.upper()
                if method not in census.MUTATING_METHODS or getattr(dec.func.value, "id", None) != "router":
                    continue
                if not dec.args or not isinstance(dec.args[0], ast.Constant):
                    continue
                path = includes.get(file.stem, "") + router_prefix + str(dec.args[0].value)
                out.append((method, path, file.name))
    return out


def test_every_mutating_decorator_under_topos_api_is_in_the_census() -> None:
    """A new ``@router.post/put/patch/delete`` lands with a census entry or this fails (fail closed)."""
    decorated = _decorated_mutating_routes()
    assert len(decorated) >= 140, len(decorated)
    unknown = sorted(f"{m} {p} ({f})" for m, p, f in decorated if census.lookup(m, p).verdict == census.UNKNOWN)
    assert not unknown, unknown


def test_calibration_answers_need_confirmation_and_previews_are_reads() -> None:
    from topos.core.handlers import DANGEROUS_TYPES, OWNER_DOOR_READ_TYPES

    assert census.lookup("PUT", "/v1/signal/calibration/answer").verdict == census.DANGEROUS
    assert census.lookup("POST", "/v1/signal/calibration/verify-claim").verdict == census.OPEN
    assert "calibration_answer" in DANGEROUS_TYPES
    assert {"calibration_catalog", "calibration_preview"} <= OWNER_DOOR_READ_TYPES
    assert not {"calibration_catalog", "calibration_preview"} & DANGEROUS_TYPES


def test_the_census_names_only_routes_that_exist() -> None:
    """No stale entries: every census template (carve-outs for routes other lanes add excepted) is mounted."""
    mounted = {p for _m, p in _mounted_mutating_routes()}
    stale = sorted(f"{r.method} {r.template}" for r in census.CENSUS if r.template not in mounted)
    assert not stale, stale


def test_the_census_has_the_shape_the_contract_names() -> None:
    dangerous = {f"{r.method} {r.template}" for r in _rules(census.DANGEROUS)}
    for entry in ("DELETE /v1/source-install", "POST /v1/source-scrub", "POST /v1/sources/retention",
                  "POST /v1/sharing/message-search/entailment-review", "POST /v1/signal/entities/{entity_id}/blackhole",
                  "DELETE /v1/home-chat/sessions/{session_id}", "POST /v1/shell/update", "PUT /v1/user-identity",
                  "POST /sources/{source_id}/sync", "PUT /sources/{source_id}/settings", "POST /v1/upgrade/consent",
                  "PUT /v1/sanitization-ollama-config", "PUT /v1/signal-extraction-config", "POST /restore",
                  "POST /v1/ingestion/upload-local-path", "POST /v1/sharing/identity/command"):
        assert entry in dangerous, entry
    opened = {f"{r.method} {r.template}" for r in _rules(census.OPEN)}
    for entry in ("POST /v1/query", "POST /llm_generation", "POST /v1/signal/fit/evaluate", "PUT /v1/ui-config",
                  "POST /v1/sharing/capture-attestation/preview", "POST /v1/tools/retrieve"):
        assert entry in opened, entry
    assert len(_rules(census.DANGEROUS)) >= 90 and len(_rules(census.OPEN)) >= 30
    assert census.lookup("POST", "/api/local/agent/dispatch").verdict == census.CARVE_OUT
    assert census.lookup("POST", "/api/local/list_database_tables").verdict == census.DISPATCHER


# ----------------------------------------------------------------------------- refused / served


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", _rules(census.DANGEROUS), ids=_ids(_rules(census.DANGEROUS)))
async def test_each_dangerous_route_is_refused_for_an_unattested_peer(socket_dir, rule) -> None:
    _refused(await _call(rule.method, _concrete(rule.template), body=b'{"sync_schedule": "hourly"}'))


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", _rules(census.DANGEROUS), ids=_ids(_rules(census.DANGEROUS)))
async def test_each_dangerous_route_is_served_with_a_live_token(socket_dir, rule) -> None:
    _served(await _call(rule.method, _concrete(rule.template), headers=_token_header()))
    assert not uds.owner_confirmation_path().exists(), "a used confirmation is consumed"


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", _rules(census.DANGEROUS), ids=_ids(_rules(census.DANGEROUS)))
async def test_each_dangerous_route_is_served_for_the_dev_proxy_on_a_node_that_allows_it(socket_dir, monkeypatch, rule):
    monkeypatch.setenv("TOPOS_DEV_PROXY_ALLOWED", "1")
    _served(await _call(rule.method, _concrete(rule.template), headers=DEV))


@pytest.mark.asyncio
async def test_the_dev_header_alone_and_the_env_alone_open_nothing(socket_dir, monkeypatch) -> None:
    _refused(await _call("DELETE", "/v1/source-install", headers=DEV))
    monkeypatch.setenv("TOPOS_DEV_PROXY_ALLOWED", "1")
    _refused(await _call("DELETE", "/v1/source-install"))


@pytest.mark.asyncio
async def test_a_wrong_or_used_token_is_refused(socket_dir) -> None:
    _refused(await _call("DELETE", "/v1/source-install", headers=[(b"x-topos-owner-confirm", b"a" * 64)]))
    header = _token_header()
    _served(await _call("DELETE", "/v1/source-install", headers=header))
    _refused(await _call("DELETE", "/v1/source-install", headers=header))


@pytest.mark.asyncio
async def test_an_attested_app_peer_is_served_at_the_socket_layer(socket_dir, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_OWNER_DOOR_TEAM_IDS", "TEAMAPP01")
    proc = uds._PeerProcess(4242, "/Applications/Topos.app/Contents/MacOS/Topos")
    proc.team, proc.team_known, proc.connections = "TEAMAPP01", True, 1
    monkeypatch.setitem(uds._peer_processes, 4242, proc)
    _served(await _call("DELETE", "/v1/source-install", peer_pid=4242))
    proc.team = "TEAMOTHER"
    _refused(await _call("DELETE", "/v1/source-install", peer_pid=4242))


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", _rules(census.OPEN) + _rules(census.DISPATCHER),
                         ids=_ids(_rules(census.OPEN) + _rules(census.DISPATCHER)))
async def test_each_open_and_dispatcher_backed_route_is_served_unattested(socket_dir, rule) -> None:
    _served(await _call(rule.method, _concrete(rule.template)))


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", _rules(census.DANGEROUS)[::7], ids=_ids(_rules(census.DANGEROUS)[::7]))
async def test_gets_are_untouched(socket_dir, rule) -> None:
    for method in ("GET", "HEAD", "OPTIONS"):
        _served(await _call(method, _concrete(rule.template)))


@pytest.mark.asyncio
async def test_an_unlisted_mutating_route_is_refused_and_its_get_is_not(socket_dir) -> None:
    """Runtime fail-closed: a route the census does not name is dangerous until it is listed."""
    assert census.lookup("POST", "/v1/made-up-door").verdict == census.UNKNOWN
    _refused(await _call("POST", "/v1/made-up-door"))
    _served(await _call("GET", "/v1/made-up-door"))


# ----------------------------------------------------------------------------- carve-outs


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", TRANSCRIPTS)
async def test_the_transcript_source_doors_pass(socket_dir, source_id: str) -> None:
    for method, path in (("POST", f"/api/local/owner/sources/{source_id}/enable"),
                         ("POST", f"/api/local/owner/sources/{source_id}/sync"),
                         ("POST", f"/api/local/owner/sources/{source_id}/pause"),
                         ("POST", f"/api/local/owner/sources/{source_id}/resume"),
                         ("POST", f"/api/local/owner/sources/{source_id}/preview"),
                         ("POST", f"/sources/{source_id}/sync")):
        _served(await _call(method, path, body=b'{"skip": []}'))
    # the same door by body: the install of the bundled transcript definition itself, which is what the
    # route installs (api/source_install._install_source_core reads source_definition_json.source_id)
    from topos.sources.registry import BUNDLED_REGISTRY

    bundled = json.loads(json.dumps(BUNDLED_REGISTRY[source_id].to_dict()))
    _served(await _call("POST", "/v1/source-install",
                        body=json.dumps({"source_id": source_id, "source_definition_json": bundled}).encode()))
    _served(await _call("POST", "/v1/source-install", body=json.dumps({"source_definition_json": bundled}).encode()))


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", TRANSCRIPTS)
async def test_a_transcript_id_in_the_body_does_not_pass_a_foreign_install(socket_dir, source_id: str) -> None:
    """The body rule must read the field the route installs from, or ``{"source_id": "<transcript>",
    "source_definition_json": {"source_id": "imessage", ...}}`` replaces the iMessage definition past the gate."""
    from topos.sources.registry import BUNDLED_REGISTRY

    def _as_json(sid: str) -> Dict[str, Any]:
        return json.loads(json.dumps(BUNDLED_REGISTRY[sid].to_dict()))

    foreign = {"source_id": source_id, "source_definition_json": _as_json("imessage")}
    _refused(await _call("POST", "/v1/source-install", body=json.dumps(foreign).encode()))
    edited = {"source_id": source_id,
              "source_definition_json": {**_as_json(source_id), "allowed_scope_ids": ["ai_conversations:read"]}}
    _refused(await _call("POST", "/v1/source-install", body=json.dumps(edited).encode()))
    version_row = {"source_id": source_id, "source_version_row_json": {"source_id": source_id}}
    _refused(await _call("POST", "/v1/source-install", body=json.dumps(version_row).encode()))
    _refused(await _call("POST", "/v1/source-install", body=json.dumps({"source_id": source_id}).encode()))
    # a patch of a transcript definition is a change to it: dangerous like every other patch
    patch = {"source_id": source_id, "source_definition_json": {"allowed_scope_ids": ["ai_conversations:read"]}}
    _refused(await _call("PATCH", "/v1/source-install", body=json.dumps(patch).encode()))


@pytest.mark.asyncio
async def test_a_non_transcript_owner_sources_door_is_still_dangerous(socket_dir) -> None:
    # Lane L5's own doors are OPEN at the socket layer (settled at integration, 16 Oct): the door
    # serves only the three bundled transcript sources and answers 404/409 for any other id itself
    # (tests/api/test_owner_sources_doors.py). The shared sync door and the install doors stay dangerous.
    _served(await _call("POST", "/api/local/owner/sources/imessage/sync"))
    _refused(await _call("POST", "/sources/imessage/sync"))
    _refused(await _call("POST", "/v1/source-install", body=b'{"source_id": "imessage"}'))
    _refused(await _call("DELETE", "/v1/source-install", body=b'{"source_id": "codex_sessions"}'))


@pytest.mark.asyncio
async def test_the_agent_dispatch_route_passes_the_gate_because_the_dispatcher_decides(socket_dir) -> None:
    _served(await _call("POST", "/api/local/agent/dispatch", body=b'{"type": "query", "payload": {}}'))


@pytest.mark.asyncio
async def test_source_settings_pass_for_the_three_keys_only(socket_dir) -> None:
    for body in ({"enabled": False}, {"posture": "mixed"}, {"exclude_spam": True},
                 {"enabled": True, "posture": "work", "exclude_spam": False, "dataset_id": "d1"}):
        reached = _served(await _call("PUT", "/sources/imessage/settings", body=json.dumps(body).encode()))
        assert json.loads(reached["body"]) == body, "the body is replayed to the route after the gate read it"
    for body in ({"sync_schedule": "hourly"}, {"enabled": True, "proof_standing": "x"}, {"enabled": True, "other": 1}):
        _refused(await _call("PUT", "/sources/imessage/settings", body=json.dumps(body).encode()))


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_or_is_too_big_passes_no_body_rule(socket_dir) -> None:
    _refused(await _call("PUT", "/sources/imessage/settings", body=b"enabled=false"))
    _refused(await _call("PUT", "/sources/imessage/settings", body=b'["enabled"]'))
    # a body that WOULD pass the rule if it were read (only open keys), but is over the cap: no rule reads it
    big = json.dumps({"enabled": False, "posture": "x" * (census.BODY_RULE_MAX_BYTES + 1)}).encode()
    _refused(await _call("PUT", "/sources/imessage/settings", body=big))


@pytest.mark.asyncio
async def test_the_entailment_list_is_a_read_and_a_decision_is_not(socket_dir) -> None:
    route = "/v1/sharing/message-search/entailment-review"
    _served(await _call("POST", route, body=b'{"binding": {}, "operation": "list"}'))
    _refused(await _call("POST", route, body=b'{"binding": {}, "operation": "confirm", "candidate_id": "a"}'))
    _served(await _call("POST", route, body=b'{"operation": "confirm"}', headers=_token_header()))


# ----------------------------------------------------------------------------- the rest


@pytest.mark.asyncio
async def test_with_the_flag_off_the_gate_is_inert(socket_dir, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")
    _served(await _call("DELETE", "/v1/source-install"))
    _served(await _call("POST", "/v1/made-up-door"))


@pytest.mark.asyncio
async def test_the_refusal_is_the_contract_body_and_nothing_of_the_route(socket_dir) -> None:
    status, out, _ = await _call("POST", "/v1/signal/entities/e1/blackhole", body=b'{"note": "secret"}')
    assert status == 403
    assert json.loads(out) == REFUSAL and set(json.loads(out)) == {"error", "message"}


@pytest.mark.asyncio
async def test_the_real_app_composes_with_the_gate(socket_dir) -> None:
    from httpx import ASGITransport, AsyncClient

    from topos.app import app

    async with AsyncClient(transport=ASGITransport(app=uds.UDSChannelApp(app)), base_url="http://topos-owner") as c:
        refused = await c.delete("/v1/source-install")
        assert refused.status_code == 403 and refused.json() == REFUSAL
        health = await c.get("/health")
        assert health.status_code != 403
        confirmed = await c.request("DELETE", "/v1/source-install", headers={"X-Topos-Owner-Confirm": uds.write_owner_confirmation()},
                                    json={"source_id": "imessage"})
        assert confirmed.status_code != 403, confirmed.text     # past the gate; the route's own answer follows


def test_the_wrapper_verifies_the_token_once_per_request(socket_dir) -> None:
    token = uds.write_owner_confirmation()
    peer = uds.socket_peer_from_scope({"type": "http", "client": None, "headers": [(b"x-topos-owner-confirm", token.encode())]})
    assert peer.confirmed is True and peer.confirmation == token
    again = uds.socket_peer_from_scope({"type": "http", "client": None, "headers": [(b"x-topos-owner-confirm", token.encode())]})
    assert again.confirmed is False
    assert uds.peer_may_change(peer) is True and uds.peer_may_change(again) is False
