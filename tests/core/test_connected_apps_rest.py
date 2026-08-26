"""Connected-apps REST wrappers: the principal rides through, refusals hold.

protects: the Settings surface works for the owner and stays inert for an
enrolled client's own token — a tpk bearer cannot list, mint, or approve
through the REST lane any more than through the message lane.
"""
import sqlite3

import pytest

from topos.api.connected_apps import (
    decide_elevation,
    enroll_connected_app,
    list_connected_apps,
    list_elevations,
    revoke_connected_app,
)
from topos.principal import OWNER_APP, THIRD_PARTY, Principal


@pytest.fixture()
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


OWNER = Principal(cls=OWNER_APP, channel="local_http")
CLIENT = Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop")


@pytest.mark.asyncio
async def test_owner_roundtrip(conn):
    out = await enroll_connected_app(
        {"client_id": "claude-desktop", "display_name": "Claude Desktop"}, OWNER
    )
    assert out["status"] == "ok" and out["token"].startswith("tpk_claude-desktop.")

    out = await list_connected_apps(OWNER)
    assert out["status"] == "ok" and out["clients"][0]["client_id"] == "claude-desktop"

    out = await list_elevations("", OWNER)
    assert out["status"] == "ok" and out["elevations"] == []

    out = await revoke_connected_app({"client_id": "claude-desktop"}, OWNER)
    assert out["status"] == "ok" and out["revoked"] is True


@pytest.mark.asyncio
async def test_third_party_token_is_inert_on_this_surface(conn):
    await enroll_connected_app({"client_id": "claude-desktop"}, OWNER)
    for call in (
        list_connected_apps(CLIENT),
        enroll_connected_app({"client_id": "evil"}, CLIENT),
        revoke_connected_app({"client_id": "claude-desktop"}, CLIENT),
        list_elevations("", CLIENT),
        decide_elevation({"request_id": 1, "approve": True}, CLIENT),
    ):
        out = await call
        assert out["status"] == "error"
