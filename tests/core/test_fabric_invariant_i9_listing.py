"""I9, listing half, rewritten for the legacy-inspection floor (merge-time edit).

This file is the exact replacement for
``tests/core/test_fabric_invariants.py::test_I9_non_owner_listing_omits_secret_tables``
on the beta lineage, carried on the main-based floor branch because that invariant
file does not exist on main. At the merge of the floor onto the lineage, replace the
block at lines 329–341 of ``test_fabric_invariants.py`` with the function below (same
name, same parametrisation), or keep this file beside it; either way the invariant keeps
its name and intent.

Why it changes: the lineage's I9 asserted the old contract, that a third party still
gets a ``list_database_tables`` listing with the secret-bearing tables merely omitted.
Decision 3 of 22 Sep 2026 makes the stricter contract: the listing is a metadata tool
under the floor, so a THIRD_PARTY principal is refused outright, on every channel, with
the one uniform refusal. Measured by the merge rehearsal: all 30 I9 cases pass on the
lineage before the floor, the two ``third_party`` parametrisations fail after it, and
nothing else in I9 moves. The relay deferral (``cp_relay``) and the routine lane
(``owner_automation``) still get a listing, and it still omits ``pipeline_jobs`` and
``mcp_clients``: that is what keeps the sharing card's row counts.

On main this file is collected and skipped: the owner-only table hiding
(``topos.data_explorer_tables.OWNER_ONLY_TABLES``) exists only on the lineage, so the
"omitted" half of the contract cannot be asserted here. The refusal half is already
pinned on main by ``tests/core/test_legacy_inspection_gate.py``.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.principal import CP_RELAY, THIRD_PARTY, Principal

try:  # lineage only: the module exists on main but without the owner-only set
    from topos.data_explorer_tables import OWNER_ONLY_TABLES
except ImportError:  # pragma: no cover - the main-branch shape
    OWNER_ONLY_TABLES = None

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        OWNER_ONLY_TABLES is None,
        reason="owner-only table hiding lives on the beta lineage; on main the refusal half is "
        "pinned by test_legacy_inspection_gate.py",
    ),
]

# ---- verbatim from test_fabric_invariants.py (I9) so this file runs alone --------------
_JOB_CANARY = "cd" * 32  # stands in for a Signal SQLCipher key / engine key


@pytest.fixture()
def secrets_db(monkeypatch):
    """A synthetic node DB with a secret in each owner-only table: one job row
    written the pre-fix way (secrets inline in payload_json, which an upgraded
    node keeps on disk until the startup scrub runs) and one enrolled MCP client
    (its token verifier). Returns {table: marker that must not leak}."""
    import json

    import topos.core.handlers as hub
    from topos.mcp_clients import _hash_token, mint_client_token
    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_pipeline_jobs_v1_up(c)
    c.execute(
        "INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, created_at, updated_at)"
        " VALUES ('job-canary', 'local_sync', 'done', ?, datetime('now'), datetime('now'))",
        (json.dumps({"progress_api_key": _JOB_CANARY, "sync_options": {"signal_hex_key": _JOB_CANARY}}),),
    )
    c.commit()
    token = mint_client_token(c, client_id="client-canary")["token"]
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield {"pipeline_jobs": _JOB_CANARY, "mcp_clients": _hash_token(token)}
    c.close()


_NON_OWNERS = [
    Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="chatgpt"),  # stamped third party
    Principal(cls=CP_RELAY, channel="cp_relay"),                          # unstamped relay
    Principal(cls=THIRD_PARTY, channel="local_http"),                     # shared key over TCP
    Principal(cls="owner_automation", channel="cp_relay"),                # routine lane
]
_SECRET_TABLES = ["pipeline_jobs", "mcp_clients"]


# ---- the replacement block -------------------------------------------------------------
@pytest.mark.parametrize("principal", _NON_OWNERS, ids=lambda p: f"{p.cls}@{p.channel}")
async def test_I9_non_owner_listing_omits_secret_tables(secrets_db, principal):
    """The listing is a metadata tool under the legacy-inspection floor (decision 3,
    22 Sep 2026): a THIRD_PARTY principal is refused outright on every channel, with
    the one uniform refusal; the relay deferral and the routine lane still get a
    listing, and that listing still omits the secret-bearing tables."""
    import topos.core.handlers as hub

    out = await hub.handle_control_plane_request(
        {"id": "x", "type": "list_database_tables", "payload": {}}, principal=principal,
    )
    if principal.cls == THIRD_PARTY:
        assert out == {"id": "x", "status": "error", "code": 403, "error": "owner_mode_required"}, out
        return
    assert out["status"] == "ok", out
    listed = {t["name"] for group in out["payload"]["tables"].values() for t in group}
    assert listed, out  # the listing itself still works for the relay and the routine lane
    assert not listed & set(_SECRET_TABLES)
