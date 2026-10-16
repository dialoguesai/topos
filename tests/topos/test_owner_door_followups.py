"""P0.11b follow-ups R1, R3, R4 (lane L2).

protects: (R1) ``sources/install_authority.principal_may_install`` no longer passes the legacy ``None`` principal —
installing, replacing, uninstalling or scrubbing a source definition takes the owner's socket or a verified owner
stamp, nothing less; (R3) the owner-confirmed entailment route asks the dangerous-door question for a decision
(confirm/reject/revoke) and not for ``list``; (R4) ``owner_entailment_review.py`` writes an owner-confirmation token
after the human's answer and presents it in ``X-Topos-Owner-Confirm`` on each decision, never on ``list``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos import uds
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.sources.install_authority import install_refusal, principal_may_install

REPO = Path(uds.__file__).resolve().parent.parent
UNATTESTED = uds.SocketPeer(pid=4242, executable="/usr/bin/curl")


# ----------------------------------------------------------------------------- R1


@pytest.mark.parametrize("principal, may", [
    (None, False),
    (Principal(cls=OWNER_APP, channel="uds"), True),
    (Principal(cls=OWNER_APP, channel="cp_relay", client_id="web-app"), True),
    (Principal(cls=CP_RELAY, channel="cp_relay"), False),
    (Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop"), False),
    (Principal(cls="owner_automation", channel="cp_relay"), False),
    (Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code"), False),
])
def test_only_the_owner_may_install(principal: Optional[Principal], may: bool) -> None:
    assert principal_may_install(principal) is may


def test_install_refusal_under_no_principal_is_the_owner_mode_refusal() -> None:
    token = set_principal(None)
    try:
        assert install_refusal("req-1") == {"id": "req-1", "status": "error", "code": 403, "error": "owner_mode_required"}
    finally:
        reset_principal(token)
    token = set_principal(Principal(cls=OWNER_APP, channel="uds"))
    try:
        assert install_refusal("req-1") is None
    finally:
        reset_principal(token)


# ----------------------------------------------------------------------------- R3


@pytest.fixture()
def entailment_door(monkeypatch):
    from topos.api import permissions_search_maintenance as routes
    from topos.auth import resolve_request_principal
    from topos.core.handlers import permissions_v2 as handlers

    seen: List[Dict[str, Any]] = []

    async def fake(message):
        seen.append(message)
        return {"id": message.get("id"), "status": "ok", "payload": {"candidates": []}}

    monkeypatch.setattr(handlers, "handle_permissions_v2_entailment_review", fake)
    app = FastAPI()
    app.include_router(routes.router)
    owner = Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1")   # acting_user set: no runtime lookup
    app.dependency_overrides[resolve_request_principal] = lambda: owner

    def _as(peer: Optional[uds.SocketPeer]) -> TestClient:
        monkeypatch.setattr(uds, "current_socket_peer", lambda: peer)
        return TestClient(app)

    return _as, seen


def test_a_decision_from_an_unattested_socket_peer_is_refused_before_the_handler(entailment_door, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    client, seen = entailment_door
    r = client(UNATTESTED).post("/v1/sharing/message-search/entailment-review",
                                json={"operation": "confirm", "candidate_id": "a" * 64})
    assert r.status_code == 403 and r.json()["detail"] == "owner_confirmation_required", r.text
    assert seen == []


def test_the_list_is_a_read_and_a_confirmed_or_in_process_decision_goes_through(entailment_door, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    client, seen = entailment_door
    assert client(UNATTESTED).post("/v1/sharing/message-search/entailment-review", json={"operation": "list"}).status_code == 200
    confirmed = uds.SocketPeer(pid=4242, executable="/usr/bin/curl", confirmation="t", confirmed=True)
    assert client(confirmed).post("/v1/sharing/message-search/entailment-review",
                                  json={"operation": "reject", "candidate_id": "b" * 64}).status_code == 200
    assert client(None).post("/v1/sharing/message-search/entailment-review",
                             json={"operation": "confirm", "candidate_id": "c" * 64}).status_code == 200
    assert [m["payload"]["operation"] for m in seen] == ["list", "reject", "confirm"]


# ----------------------------------------------------------------------------- R4


def _cli():
    path = REPO / "scripts" / "permissions_v2" / "owner_entailment_review.py"
    spec = importlib.util.spec_from_file_location("owner_entailment_review_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, body):
        self.status_code, self._body = 200, body

    def json(self):
        return self._body


class _Client:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def post(self, path, json, headers=None):
        self.calls.append({"path": path, "json": json, "headers": dict(headers or {})})
        if json["operation"] == "list":
            return _Response({"candidates": [{"candidate_id": "a" * 64, "kind": "fact", "claim": "c",
                                              "message": "m", "status": "pending"}]})
        return _Response({"status": "ok"})


def test_each_decision_carries_a_fresh_confirmation_and_the_list_carries_none(tmp_path) -> None:
    cli = _cli()
    client = _Client()
    socket_path = tmp_path / "engine.sock"
    token_path = tmp_path / "owner-confirm.token"

    cli.review(client, {"node_id": "n"}, ask=lambda _p: "c", show=lambda _t: None, socket_path=socket_path)

    assert [c["json"]["operation"] for c in client.calls] == ["list", "confirm"]
    assert client.calls[0]["headers"] == {}
    presented = client.calls[1]["headers"].get("X-Topos-Owner-Confirm")
    assert isinstance(presented, str) and len(presented) == 64
    assert token_path.exists() and oct(token_path.stat().st_mode & 0o777) == "0o600"
    assert uds.verify_owner_confirmation(presented, path=token_path) is True   # the node would have served it
    assert cli.post(client, {"operation": "list"}, socket_path=socket_path)[0] == 200
    assert client.calls[-1]["headers"] == {}


def test_a_decision_without_a_socket_path_still_confirms_next_to_the_default_socket(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    cli = _cli()
    client = _Client()
    cli.post(client, {"operation": "reject", "candidate_id": "b" * 64})
    presented = client.calls[-1]["headers"]["X-Topos-Owner-Confirm"]
    assert uds.verify_owner_confirmation(presented) is True
