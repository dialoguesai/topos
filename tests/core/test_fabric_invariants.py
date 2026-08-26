"""Principal-fabric REGRESSION GATE — the load-bearing guarantees, as assertions.

These are not behavior examples; they are the invariants the whole fabric
exists to hold. Each test names the property it protects. If one of these goes
red, a security guarantee the design promised has been broken — that is the
signal, and it is why this file is wired into the pre-push suite.

The adversarial cases here deliberately take the ATTACKER'S view: a caller who
lies in every payload field it controls, replays captured proof, or arrives
with no credential at all. The guarantee is that none of that reaches owner
data — identity comes from the channel, never the payload.

protects (index):
  I1  a THIRD_PARTY caller can never reach fact content by payload claims
  I2  facts_all (special-class) is unreachable by ANY consent path
  I3  absence of credential/stamp/enrollment => most-restrictive class
  I4  a stamp can only NARROW or NAME — never mint grantee/stranger classes
  I5  grantee turns ignore the principal entirely
  I6  every closed hole stays closed (F2 gateway, "mcp" whitelist, tpk on REST)
"""
import itertools
import sqlite3

import pytest

from topos.config.settings import ENGINE_CONFIG_KEY_PACKET_RESOLUTION, settings
from topos.core.handlers.common import set_engine_config_value
from topos.disclosure.tier import resolve_disclosure_tier
from topos.mcp_clients import (
    ELEVATION_CEILING,
    decide_elevation,
    mint_client_token,
    request_elevation,
)
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal
from topos.query import packet_resolution as pr
from topos.query.packet_resolution import effective_packet_resolution, resolution_order

OWNER_UUID = "9670043c-aaaa-bbbb-cccc-000000000000"


@pytest.fixture()
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    set_engine_config_value(c, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": True, "provider": "ollama", "model": "m", "remote_engine_url": False},
    )
    yield c
    c.close()


# ---- I1: third-party payload spoofing can never reach fact content ---------
_SPOOFS = list(itertools.product(
    [OWNER_UUID, "owner", "mcp", ""],       # requester_id lies
    [OWNER_UUID, "owner", ""],              # owner_id lies
    ["owner_raw", "default_disclosure"],    # tier lies
))


@pytest.mark.parametrize("requester_id,owner_id,tier", _SPOOFS)
def test_I1_third_party_never_reaches_facts_by_payload(conn, requester_id, owner_id, tier):
    info = effective_packet_resolution(
        conn,
        requester_id=requester_id,
        owner_id=owner_id,
        disclosure_tier=tier,
        principal=Principal(cls=THIRD_PARTY, channel="local_http", client_id="attacker"),
    )
    assert info["effective"] == "scores_only", (requester_id, owner_id, tier)
    assert info["reason"] == "principal_floor"


# ---- I2: facts_all is unreachable by any consent path ----------------------
def test_I2_elevation_ceiling_excludes_facts_all(conn):
    assert ELEVATION_CEILING == "facts"
    assert resolution_order("facts_all") > resolution_order("facts")
    mint_client_token(conn, client_id="c")
    req = request_elevation(conn, client_id="c", scope_id="relationships.social")
    # Even an owner approving with a facts_all body is clamped at decide time.
    row = decide_elevation(conn, request_id=req["id"], approve=True)
    assert row["resolution"] == "facts"
    info = effective_packet_resolution(
        conn, scope_id="relationships.social", disclosure_tier="default_disclosure",
        principal=Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="c"),
    )
    assert info["effective"] == "facts"  # never facts_all


def test_I2_automation_also_capped_below_facts_all(conn):
    info = effective_packet_resolution(
        conn, disclosure_tier="owner_raw",
        principal=Principal(cls="owner_automation", channel="cp_relay"),
    )
    assert info["effective"] == "facts" and info["reason"] == "automation_cap"


# ---- I3: absence resolves to the most-restrictive outcome ------------------
def test_I3_no_principal_no_owner_id_is_floored(conn):
    """Legacy/relay with nothing to prove ownership stays floored."""
    info = effective_packet_resolution(
        conn, requester_id="mcp", owner_id="", disclosure_tier="owner_raw",
        principal=Principal(cls=CP_RELAY, channel="cp_relay"),
    )
    assert info["effective"] == "scores_only" and info["reason"] == "non_owner_floor"


def test_I3_unenrolled_relay_client_has_no_elevation(conn):
    """A stamped client id that was never enrolled cannot hold a grant."""
    info = effective_packet_resolution(
        conn, scope_id="relationships.social", disclosure_tier="default_disclosure",
        principal=Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="never-enrolled"),
    )
    assert info["effective"] == "scores_only" and info["reason"] == "principal_floor"


# ---- I4: a stamp can only narrow or name -----------------------------------
def test_I4_stamp_class_allowlist_rejects_grantee_and_stranger():
    from topos.relay_stamp import ALLOWED_CLASSES

    assert "grantee" not in ALLOWED_CLASSES
    assert ALLOWED_CLASSES <= {OWNER_APP, THIRD_PARTY, "owner_automation"}


# ---- I5: grantee turns ignore the principal --------------------------------
def test_I5_grantee_pipeline_drops_principal():
    import inspect

    from topos.query import pipeline

    src = inspect.getsource(pipeline)
    anchor = src.index("_principal = current_principal()")
    assert "if is_grantee_request:" in src[anchor:anchor + 400]
    assert "_principal = None" in src[anchor:anchor + 400]


def test_I5_grantee_tier_unmoved_by_owner_app_stamp():
    """Even an owner_app stamp cannot upgrade an explicit grantee request."""
    tier = resolve_disclosure_tier(
        requester_id="grantee-1", owner_id=OWNER_UUID, is_grantee_request=True,
        explicit_tier="owner_raw",
        principal=Principal(cls=OWNER_APP, channel="local_http"),
    )
    assert tier == "default_disclosure"


# ---- I6: closed holes stay closed ------------------------------------------
def test_I6_mcp_whitelist_only_survives_in_legacy():
    """The spoofable 'mcp' owner-whitelist is bypassed whenever a principal
    exists; it survives ONLY for legacy callers with no principal at all."""
    # With a third_party principal, 'mcp' does not grant owner_raw.
    assert resolve_disclosure_tier(
        requester_id="mcp", owner_id="owner",
        principal=Principal(cls=THIRD_PARTY, channel="local_http"),
    ) == "default_disclosure"
    # Legacy path (no principal) keeps it, by necessity (migration invariant).
    assert resolve_disclosure_tier(requester_id="mcp", owner_id="owner") == "owner_raw"


def test_I6_require_api_key_rejects_tpk_tokens():
    """A per-client token is not a general engine key: the REST surface stays
    closed to it, so an enrolled client's reach is the MCP tool set only."""
    import sqlite3 as _sq

    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from topos.auth import require_api_key

    c = _sq.connect(":memory:")
    tok = mint_client_token(c, client_id="claude-desktop")["token"]
    # require_api_key does not consult the registry — a tpk is simply not a key.
    with pytest.raises(HTTPException):
        require_api_key(HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok))
    c.close()


def test_I6_owner_app_is_owner_without_any_payload_identity(conn):
    """The F3 fix, pinned: owner_app is owner by CHANNEL, needing no owner_id
    and surviving the historic 'mcp' requester default."""
    info = effective_packet_resolution(
        conn, requester_id="mcp", owner_id="", disclosure_tier="owner_raw",
        principal=Principal(cls=OWNER_APP, channel="local_http"),
    )
    assert info["effective"] == "facts_all" and info["reason"] == "active"


# ---- I7: the golden cross-repo stamp contract (twin of the CP's C1) --------
# This byte string is asserted verbatim by the CONTROL PLANE's
# tests/control_plane/test_fabric_invariants.py against its own
# canonical_signing_payload. If the two ever disagree, relay stamps fail
# verification and the system falls back to legacy SILENTLY — so both sides
# freeze the same bytes, and a change on either side goes red.
_GOLDEN_STAMP = {
    "v": 1, "cls": "third_party", "client_id": "chatgpt",
    "acting_user": "owner-uuid", "iat": 1700000000.0, "exp": 1700000120.0,
}
_GOLDEN_BYTES = (
    b'{"acting_user":"owner-uuid","client_id":"chatgpt","cls":"third_party",'
    b'"exp":1700000120.0,"iat":1700000000.0,"msg_id":"msg-abc",'
    b'"msg_type":"query","v":1}'
)


def test_I7_canonical_payload_is_frozen():
    from topos.relay_stamp import canonical_signing_payload

    got = canonical_signing_payload(_GOLDEN_STAMP, msg_id="msg-abc", msg_type="query")
    assert got == _GOLDEN_BYTES, (
        "canonical_signing_payload changed shape — this SILENTLY breaks stamp "
        "verification against the CP (fail-open to legacy). Change BOTH repos "
        "and update the golden in both test files in lockstep."
    )
