"""Night review B, node half: what a recipient's request leaves behind, and the truth doors.

B2  Every read a recipient makes claims one `p2a_requests` row before any floor
    runs -- before the fact is loaded, before the review is read -- so every
    refused read, including a read for an id that does not exist, grows the
    owner's node database at the recipient's request rate. What bounds it is
    `ledger_retention.compact_expired`: once an envelope is past `expires_at +
    SKEW`, its row keeps only `request_id`, `envelope_hash` and `status`, about
    120 bytes where the admitted row held ~2.8 KB. The tombstone is kept rather
    than deleted because signing.py's expiry check trusts the node's clock: a
    clock that steps back makes an expired envelope current again, and only the
    tombstone still refuses its replay. Deleting the rows was the alternative;
    the owner kept compaction on 2026-09-25, and the test below pins its bound.

B4  POST /api/local/verify_claim, /truth_prompts and /truth_seed_fact depend on
    `resolve_request_principal` and never read the class it returns
    (api/local_mcp.py:33-104), although all three are documented owner-key only
    and the last one writes an owner-stated fact. Recorded as F4 in
    WORK_ONLY_BOUNDARY_CATALOG. A fix exists on `fix/truth-door-owner-gate` @
    192626ff, which is NOT an ancestor of beta/permissions-v2 @ 212a0db4, so
    the lineage under review is still open.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos.auth import resolve_request_principal
from topos.permissions_v2.canonical import PolicyError
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from tests.permissions_v2.test_contract_and_ledger import (  # noqa: F401
    owner, sample_policy, setup, signed_request,
)

pytestmark = [pytest.mark.p0]


# --------------------------------------------------------------------------- B2


def request_ids(ledger):
    with sqlite3.connect(ledger.path) as db:
        return {row[0] for row in db.execute("SELECT request_id FROM p2a_requests")}


def row_sizes(ledger):
    """request_id -> (bytes of envelope kept, bytes of everything else in the row)."""
    with sqlite3.connect(ledger.path) as db:
        return {row[0]: (row[1], row[2]) for row in db.execute(
            "SELECT request_id, length(envelope_json), "
            "length(request_id) + length(envelope_hash) + length(status) FROM p2a_requests")}


#: What an expired admission may still cost the owner: a tombstone, not an envelope.
TOMBSTONE_BYTES = 128


def test_b2_expired_admissions_shrink_to_tombstones_that_still_refuse_a_replay(setup):
    """A recipient grows the owner's ledger by a small tombstone per read, never an envelope.

    Five reads are admitted and then left to expire; a sixth read, long past
    their envelopes' `expires_at` and the skew, compacts them. Each keeps its id
    and hash and drops its envelope, and a replay with the node's clock stepped
    back inside the old envelope's validity is still refused.
    """
    ledger = setup[0]
    spent = []
    for index in range(5):
        authority, request, payload, envelope = signed_request(setup, request_id=f"request-{index}")
        ledger.admit(envelope, request=request, payload=payload, now=1100)
        spent.append((f"request-{index}", request, payload, envelope))
    assert {request_id for request_id, *_ in spent} <= request_ids(ledger)

    # Well past every one of those envelopes (issued 1100, expiring 1200) and the skew.
    authority, request, payload, envelope = signed_request(
        setup, request_id="request-later", changes={"issued_at": 4000, "expires_at": 4100})
    ledger.admit(envelope, request=request, payload=payload, now=4000)

    sizes = row_sizes(ledger)
    for request_id, *_ in spent:
        kept, rest = sizes[request_id]
        assert kept == 0, f"{request_id} still holds its {kept}-byte envelope after expiry"
        assert rest <= TOMBSTONE_BYTES, f"{request_id}'s tombstone is {rest} bytes"
    assert sizes["request-later"][0] > TOMBSTONE_BYTES  # a live envelope is untouched

    # Why the tombstone stays: with the clock back inside its validity, the
    # expired envelope verifies again, and only the claimed id refuses it.
    request_id, request, payload, envelope = spent[0]
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.admit(envelope, request=request, payload=payload, now=1100)


def test_b2_a_refused_read_still_writes_the_row_before_any_floor(setup):
    """The row is written by admission, so a refusal at any floor still pays for it."""
    ledger = setup[0]
    authority, request, payload, envelope = signed_request(setup, request_id="request-refused")
    ledger.admit(envelope, request=request, payload=payload, now=1100)
    assert "request-refused" in request_ids(ledger)
    # The same envelope can never be used again -- which is the row's only job.
    with pytest.raises(PolicyError, match="request_replay"):
        ledger.admit(envelope, request=request, payload=payload, now=1100)


# --------------------------------------------------------------------------- B4


TRUTH_ROUTES = {
    "/api/local/verify_claim": ({"statement": "I live in the synthetic city", "app_id": "any-app-id"}, "verify_claim"),
    "/api/local/truth_prompts": ({"app_id": "any-app-id", "limit": 3}, "truth_prompts"),
    "/api/local/truth_seed_fact": ({"app_id": "any-app-id", "predicate": "favorite_food", "value": "tacos"}, "truth_seed_fact"),
}


@pytest.fixture
def truth_app(monkeypatch):
    """The engine-local router with a channel-verified THIRD_PARTY caller.

    This is what an enrolled `tpk_` client -- the production Claude Desktop
    credential -- resolves to (auth.py:127-144). Nothing is started and no
    database is touched: the dispatcher is replaced by a recorder, so the test
    measures only whether the door admits the caller.
    """
    from topos.api import local_mcp

    dispatched = []

    async def recorder(message, principal=None):
        dispatched.append((message.get("type"), principal))
        return {"status": "ok", "payload": {"reached": message.get("type")}}

    monkeypatch.setattr(local_mcp, "handle_control_plane_request", recorder)
    app = FastAPI()
    app.include_router(local_mcp.router)
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(
        cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop", acting_user="")
    return app, dispatched


@pytest.mark.parametrize("path", sorted(TRUTH_ROUTES))
def test_b4_engine_local_truth_doors_admit_a_third_party_principal(truth_app, path):
    """All three are documented owner-key only; none of them checks the class."""
    app, dispatched = truth_app
    body, message_type = TRUTH_ROUTES[path]
    with TestClient(app) as client:
        result = client.post(path, json=body)
    assert not dispatched, (
        f"{path} handed a THIRD_PARTY principal straight to the {message_type} handler "
        f"(status {result.status_code}); api/local_mcp.py takes resolve_request_principal and "
        "never reads the class. Fix: fix/truth-door-owner-gate @ 192626ff, not on this lineage")


def test_b4_the_owner_lane_is_the_one_that_should_pass(truth_app, monkeypatch):
    """The control: the owner's 0600 socket must keep working after any fix."""
    app, dispatched = truth_app
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(cls=OWNER_APP, channel="uds")
    with TestClient(app) as client:
        result = client.post("/api/local/truth_prompts", json={"app_id": "truth-mirror", "limit": 3})
    assert result.status_code == 200 and dispatched and dispatched[0][0] == "truth_prompts"
