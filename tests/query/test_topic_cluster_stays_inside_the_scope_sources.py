"""A grantee reads a topic cluster only when every member came from a source its scope covers.

Clusters reach a grantee's summary packet chosen by dimension label alone
(`_load_ranked_clusters`). A dimension spans tables the scope does not: `memory`
holds AI chats, coding sessions, messages and the journal. So a `messages:read`
grantee was handed the label and size of a cluster built only from ChatGPT and
Claude Code sessions, and an AI-chats grantee one built only from iMessage. The
quoted centre text was already dropped; the label, written from the members' words,
was not. A cluster's `source_mix` says where its members came from, and it is now
served below the owner's tier only when that mix sits inside the scope's sources.

Driven through the real `query` door as the control plane forwards a grantee's
`shared_query_scope`. Every cluster and entry is invented.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

LABEL = "quillfeather plans"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "clusters.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    import topos.core.state as state

    monkeypatch.setattr(state, "get_db_connection", lambda: c)
    yield c
    c.close()


def _cluster(conn, source_mix: dict) -> None:
    conn.execute(
        "INSERT INTO topic_clusters (cluster_id, label, dimension, member_count, source_mix_json,"
        " label_terms_json, centroid_preview) VALUES ('cl-1', ?, 'memory', 12, ?, '[\"quillfeather\"]',"
        " 'quillfeather centre line')",
        (LABEL, json.dumps(source_mix)),
    )
    conn.commit()


async def _ask(scope: str, *, grantee: bool) -> str:
    payload = {
        "scope_id": scope,
        "access_mode": "summary",
        "intent": "quillfeather",
        "query": "quillfeather",
        "query_session_id": f"cluster-{uuid.uuid4().hex[:8]}",
    }
    if grantee:
        payload.update(
            disclosure_tier="default_disclosure",
            disclosure_ceiling="default",
            filter_manifest={"access_mode_ceiling": "summary"},
            owner_user_id="owner-a",
            owner_id="owner-a",
            requester_id="owner-a",
        )
        principal = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="outside-client", acting_user="owner-a")
    else:
        principal = Principal(cls=OWNER_APP, channel="uds")
    out = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": "query", "payload": payload}, principal=principal
    )
    assert out.get("status") == "ok", out
    assert (out.get("payload") or {}).get("turn_outcome") == "live_query", out
    return json.dumps(out["payload"], default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope, source_mix",
    [
        # The live node's shape: every memory cluster is AI chats and coding sessions.
        ("messages:read", {"chatgpt_file_ingestion": 9, "claude_code_sessions": 3}),
        ("ai_conversations:read", {"imessage": 12}),
        # One member outside the scope is enough to keep the whole cluster back.
        ("messages:read", {"imessage": 11, "chatgpt_file_ingestion": 1}),
        # No recorded mix: nothing shows the cluster is inside the grant.
        ("messages:read", {}),
    ],
)
async def test_a_grantee_reads_no_cluster_from_outside_its_scope(conn, scope, source_mix):
    _cluster(conn, source_mix)
    assert LABEL not in await _ask(scope, grantee=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope, source_mix",
    [
        ("messages:read", {"imessage": 12}),
        ("ai_conversations:read", {"chatgpt_file_ingestion": 9, "chatgpt_ui_conversation": 3}),
    ],
)
async def test_a_cluster_inside_the_scope_still_answers_a_grantee(conn, scope, source_mix):
    _cluster(conn, source_mix)
    assert LABEL in await _ask(scope, grantee=True)


@pytest.mark.asyncio
async def test_the_owner_still_reads_every_cluster(conn):
    _cluster(conn, {"chatgpt_file_ingestion": 9, "claude_code_sessions": 3})
    assert LABEL in await _ask("messages:read", grantee=False)
