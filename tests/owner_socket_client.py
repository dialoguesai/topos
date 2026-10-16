"""A test client on the owner socket that confirms its own changes, the way the owner's TTY paths do.

Since the dangerous-door split (AI-tool on-ramp, CONTRACTS §1.6; spike P0.11b) a mutating request on the real owner
socket to a route the census names dangerous (``topos/owner_door_routes.py``) is refused before the route runs
unless the peer is an attested app, the dev proxy on a node that allows it, or presents a live owner-confirmation
token in ``X-Topos-Owner-Confirm`` — one token, one change. A test that models the owner's own process on the socket
(the CLI after its TTY yes, the owner-review script) therefore presents a fresh token on every mutating request;
reads are untouched. The token is written where the node reads it (next to the pinned ``TOPOS_UDS_PATH`` socket, so
never under the real ``~/.topos``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from topos.uds import write_owner_confirmation

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OwnerConfirmedClient(TestClient):
    """``TestClient(UDSChannelApp(app))`` whose every mutating request carries a fresh owner-confirmation token."""

    def request(self, method, url, **kwargs):  # type: ignore[override]
        if str(method).upper() in MUTATING:
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("X-Topos-Owner-Confirm", write_owner_confirmation())
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)
