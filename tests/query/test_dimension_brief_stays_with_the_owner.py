"""A dimension brief is the owner's: no grantee reads one until a brief is written from disclosed text.

`features/signal/dimension_briefs.py` asks a model to write each dimension's brief
from the raw text of every table in that dimension (`brief_canonical_loader`:
message and journal content, contact names and identifiers, place names), with no
disclosure tier, no NSFW check and no table ceiling, and stores no disclosure
marker on the result. The summary lane served it to any grantee whose scope names
the dimension. Every other lane a grantee reads serves disclosed, unflagged rows of
the granted tables; a brief can be shown to be none of those, so below the owner's
tier the lane is now empty, as the graph and journal-event lanes are.

Driven through the real `query` door as the control plane forwards a grantee's
`shared_query_scope`. The brief and the entry are invented.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, Principal

BRIEF = "BRIEFBODY quillfeather wellbeing, written from the raw rows of every wellbeing table"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "brief.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO signal_dimension_briefs (brief_id, signal_dimension, head_revision_id, structured_json,"
        " markdown_body, revision_number, updated_at, updated_by)"
        " VALUES ('brief-1', 'wellbeing', 'rev-1', '{}', ?, 1, '2026-07-01T00:00:00Z', 'dimension_briefs')",
        (BRIEF,),
    )
    c.execute(
        "INSERT INTO journal_entries (entry_id, content, content_disclosure, content_nsfw, source_id, entry_at)"
        " VALUES ('je-1', 'quillfeather raw entry', 'quillfeather disclosed entry', 0, 'demo_journal_file',"
        " '2026-07-01T00:00:00Z')"
    )
    c.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


async def _ask(*, grantee: bool) -> dict:
    payload = {
        "scope_id": "health:read",
        "access_mode": "summary",
        "intent": "quillfeather",
        "query": "quillfeather",
        "query_session_id": f"brief-{uuid.uuid4().hex[:8]}",
    }
    if grantee:
        payload.update(
            is_grantee_request=True,
            disclosure_tier="default_disclosure",
            disclosure_ceiling="default",
            filter_manifest={"access_mode_ceiling": "summary"},
            owner_user_id="owner-a",
            owner_id="owner-a",
            requester_id="grantee-a",
        )
        principal = RELAY_PRINCIPAL
    else:
        principal = Principal(cls=OWNER_APP, channel="uds")
    out = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": "query", "payload": payload}, principal=principal
    )
    assert out.get("status") == "ok", out
    return out["payload"]


@pytest.mark.asyncio
async def test_a_grantee_reads_no_dimension_brief(conn):
    payload = await _ask(grantee=True)
    wire = json.dumps(payload, default=str)
    assert "quillfeather disclosed entry" in wire, "the scope's own disclosed rows still answer"
    assert "BRIEFBODY" not in wire, "a brief written from raw rows reached a grantee"


@pytest.mark.asyncio
async def test_the_owner_still_reads_the_brief(conn):
    assert "BRIEFBODY" in json.dumps(await _ask(grantee=False), default=str)
