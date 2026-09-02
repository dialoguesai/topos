"""SUITE-P hermetic leg: every aggregate case answers with its exact numbers.

protects: counting-class questions get exact answers — the aggregate verb,
driven through real dispatch on the constructed corpus, returns the numbers
the seed built, and the person dimension folds the alias-trap pair at scale.

The necessity leg (the same questions through the OLD inference lane, whose
failure rate justifies the verb) is a live-model measurement and runs via
`scripts/run_query_eval.py --aggregate`, not here.
"""

from __future__ import annotations

import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, Principal
from topos.storage.db.migrations import apply_all_migrations

from query_eval_cases import AGGREGATE_CASES, evaluate_aggregate_result
from tests.fixtures.query_eval_seed.apply_aggregate_seed import apply_aggregate_seed

pytestmark = pytest.mark.asyncio

OWNER = Principal(cls=OWNER_APP, channel="cp_relay")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> sqlite3.Connection:
    db = tmp_path_factory.mktemp("suitep") / "suitep.db"
    conn = sqlite3.connect(str(db))
    apply_all_migrations(conn)
    apply_aggregate_seed(conn)
    return conn


@pytest.mark.parametrize("case", AGGREGATE_CASES, ids=[c.id for c in AGGREGATE_CASES])
async def test_aggregate_case_exact(case, seeded, monkeypatch):
    monkeypatch.setattr(hub, "get_db_connection", lambda: seeded)
    resp = await handle_control_plane_request(
        {
            "id": f"suitep-{case.id}",
            "type": "aggregate",
            "payload": {**case.payload, "dataset_id": "u1:default"},
            "caller": {"mcp_source": "topos_home_chat"},
        },
        principal=OWNER,
    )
    assert resp is not None and resp.get("status") == "ok", resp
    payload = resp["payload"]
    assert payload["turn_outcome"] == "live_query", payload.get("deny_reason")
    ok, reason = evaluate_aggregate_result(case, payload["public_result"] or {})
    assert ok, f"[{case.id}] {reason}"
