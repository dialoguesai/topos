"""A node whose ledger has recorded a canonical floor must come back after a restart.

`NodePolicyProtocol` refuses to start when its ledger has recorded a floor and
none is attached: a node that lost its floor must never reach the point of
signing. `load_runtime` is the only production constructor, and it attached no
floor; the runtime attached one lazily, on the first identity or review call. So
the first process on a node recorded the floor, and every later process refused
at startup with `canonical_floor_unavailable`. That took down every signed
permissions route after one restart. The lab showed it the first time its engine
restarted after the floor rule landed.

These run the real runtime twice on one private node config, as a restart does.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401 (fixtures)
    SELF_ENTITY, attest_selves, corpus, lane, paired_runtime, projection_runtime, protocol_call, _next)
from tests.permissions_v2.test_ingest_snapshot_work_canary import CP_ISSUER
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.protocol import StatusRequestBody, sign_status_request


async def signed_status(lane):
    identity = lane.runtime.protocol.ledger.identity
    now = int(time.time())
    binding = {**identity.model_dump(), "actor_id": "recipient-restart", "client_id": "recipient-client",
               "grant_id": "grant-restart", "assignment_id": "assignment-restart"}
    return await protocol_call(lane, "status", sign_status_request(StatusRequestBody.parse({
        "version": "topos-policy-status-request/v2", "kid": "cp-key", "issuer_id": CP_ISSUER,
        "audience_id": identity.node_id, "request_id": _next(lane, "status"), "binding": binding,
        "command_id": None, "command_hash": None, "issued_at": now, "expires_at": now + 100}), lane.cp_key))


def recorded_floor(lane):
    with lane.runtime.protocol.ledger._transaction() as conn:
        return conn.execute("SELECT * FROM p2a_canonical_floor WHERE singleton=1").fetchone()


def restart(lane, monkeypatch):
    """Close the running process's runtime and load it again from the same private config."""
    from topos.permissions_v2 import runtime as runtime_module
    from topos.permissions_v2.runtime import load_runtime
    config_path, database = lane.runtime.config_path, lane.runtime.protocol.canonical_database
    lane.runtime.close()
    reopened = load_runtime(config_path, active_database=database)
    monkeypatch.setattr(runtime_module, "_runtime", reopened)
    lane.runtime = reopened
    return reopened


@pytest.mark.asyncio
async def test_a_node_that_recorded_its_floor_serves_again_after_a_restart(lane, monkeypatch):
    await attest_selves(lane, [SELF_ENTITY])
    await signed_status(lane)
    assert recorded_floor(lane) is not None
    reopened = restart(lane, monkeypatch)
    assert reopened.protocol.canonical_floor is not None
    assert (await signed_status(lane)).state is not None
    state = await attest_selves(lane, [])
    assert {item.entity_id: item.entry_state for item in state.subjects}[SELF_ENTITY] == "active"
    assert reopened.canonical_floor() is reopened.protocol.canonical_floor  # one floor store per process


@pytest.mark.asyncio
async def test_a_restarted_node_whose_floor_file_is_gone_still_refuses(lane, monkeypatch):
    await attest_selves(lane, [SELF_ENTITY])
    await signed_status(lane)
    floor_file = lane.runtime.protocol.canonical_database.parent / "permissions-v2" / "canonical-floor.json"
    assert floor_file.is_file()
    floor_file.unlink()
    from topos.permissions_v2.runtime import load_runtime
    config_path, database = lane.runtime.config_path, lane.runtime.protocol.canonical_database
    lane.runtime.close()
    with pytest.raises(PolicyError, match="canonical_floor_unavailable"):
        load_runtime(config_path, active_database=database)


@pytest.mark.asyncio
async def test_a_node_that_never_had_a_floor_still_starts_without_one(lane, monkeypatch):
    reopened = restart(lane, monkeypatch)
    assert reopened.protocol.canonical_floor is None
    assert (await signed_status(lane)).state is not None
