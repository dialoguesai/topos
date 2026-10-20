"""The repair for coding-session conversations filed under another source's prefix (upgrade step, 20 Oct 2026).

On the owner's node 1.5.13 filed Claude Code messages under ``codex_sessions:<session>`` and the next build under
``claude_code_sessions:<session>``, so one session could be two ``ai_chat_conversations`` rows. The repair renames
every coding-source conversation to ``<tool>:<session>`` (``coding_tool_mapper.canonical_coding_conversation_id``),
merges the rows that then share an id, renumbers the merged chats' messages, and carries the new id into the derived
rows that copied it. Synthetic node databases and invented rows only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.ingestion.ai_tool_fixtures import OWNER, build_node

S1 = "5d9cabcd-ef01-4abc-8def-abcdefabcde1"   # a Claude Code session split across the switch
S2 = "5d9cabcd-ef01-4abc-8def-abcdefabcde2"   # a Claude Code session filed under codex only
S3 = "5d9cabcd-ef01-4abc-8def-abcdefabcde3"   # a Codex session


@pytest.fixture
def node(tmp_path: Path) -> sqlite3.Connection:
    conn = build_node(tmp_path / "node.db")
    _seed(conn)
    yield conn
    conn.close()


def _message(conn, message_id, conversation_id, source_id, event_at, sequence):
    conn.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, metadata_json,"
        " sequence, source_id, writer_class) VALUES (?, ?, 'human', ?, 'invented text', '{}', ?, ?, 'owner_app')",
        (message_id, conversation_id, event_at, sequence, source_id),
    )


def _conversation(conn, conversation_id, source_id, created_at, updated_at, title, latest_title, *, owner=OWNER,
                  ingested_at="2026-10-20T00:00:00Z", sync_batch_id="batch-a"):
    conn.execute(
        "INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, latest_title, source_id, created_at,"
        " updated_at, source_record_id, ingested_at, sync_batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conversation_id, owner, title, latest_title, source_id, created_at, updated_at, conversation_id, ingested_at,
         sync_batch_id),
    )


def _seed(conn: sqlite3.Connection) -> None:
    # S1: two messages under 1.5.13's codex prefix, one under the next build's source-id prefix.
    _message(conn, "claude_code:a1", f"codex_sessions:{S1}", "claude_code_sessions", "2026-10-20T10:00:00Z", 0)
    _message(conn, "claude_code:a2", f"codex_sessions:{S1}", "claude_code_sessions", "2026-10-20T10:00:05Z", 1)
    _message(conn, "claude_code:a3", f"claude_code_sessions:{S1}", "claude_code_sessions", "2026-10-20T16:00:00Z", 0)
    _conversation(conn, f"codex_sessions:{S1}", "claude_code_sessions", "2026-10-20T10:00:00Z",
                  "2026-10-20T10:00:05Z", "Lantern wiring", "Lantern wiring",
                  ingested_at="2026-10-20T10:01:00Z", sync_batch_id="claude_code_sessions:old")
    _conversation(conn, f"claude_code_sessions:{S1}", "claude_code_sessions", "2026-10-20T16:00:00Z",
                  "2026-10-20T16:00:00Z", "Lantern wiring, part two", "Lantern wiring, part two",
                  ingested_at="2026-10-20T16:01:00Z", sync_batch_id="claude_code_sessions:new")
    # S2: one conversation, renamed only.
    _message(conn, "claude_code:b1", f"codex_sessions:{S2}", "claude_code_sessions", "2026-10-19T09:00:00Z", 0)
    _conversation(conn, f"codex_sessions:{S2}", "claude_code_sessions", "2026-10-19T09:00:00Z",
                  "2026-10-19T09:00:00Z", "Uploader retry", None)
    # S3: a Codex session under its source-id prefix.
    _message(conn, "codex:c1", f"codex_sessions:{S3}", "codex_sessions", "2026-10-18T09:00:00Z", 0)
    _conversation(conn, f"codex_sessions:{S3}", "codex_sessions", "2026-10-18T09:00:00Z", "2026-10-18T09:00:00Z",
                  "Glow shader", None)
    # Not a coding source: never touched.
    _message(conn, "chatgpt:m1", "chatgpt:conv-1", "chatgpt_export", "2026-10-01T09:00:00Z", 0)
    _conversation(conn, "chatgpt:conv-1", "chatgpt_export", "2026-10-01T09:00:00Z", "2026-10-01T09:00:00Z",
                  "Trip plan", None)

    # Derived copies of the conversation id.
    for embedding_id, record_id, source_id, conversation_id in (
        ("e1", "claude_code:a1", "claude_code_sessions", f"codex_sessions:{S1}"),
        ("e3", "claude_code:a3", "claude_code_sessions", f"claude_code_sessions:{S1}"),
        ("e9", "chatgpt:m1", "chatgpt_export", "chatgpt:conv-1"),
    ):
        provenance = {"record_id": record_id, "source_id": source_id, "conversation_id": conversation_id}
        conn.execute(
            "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, model, provenance_json,"
            " conversation_id) VALUES (?, ?, ?, 'memory', 'm', ?, ?)",
            (embedding_id, record_id, source_id, json.dumps(provenance), conversation_id),
        )
        conn.execute(
            "INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id, metadata_json)"
            " VALUES (?, 'k1', ?, ?, ?)",
            (f"tm-{embedding_id}", record_id, source_id, json.dumps(provenance)),
        )
    for old in (f"codex_sessions:{S1}", f"claude_code_sessions:{S1}"):
        node_id = f"conversation:{old}"
        conn.execute(
            "INSERT INTO graph_nodes (node_id, node_type, label, metadata_json, source_id)"
            " VALUES (?, 'conversation', ?, ?, 'claude_code_sessions')",
            (node_id, old[:48], json.dumps({"node_id": node_id, "label": old[:48]})),
        )
        conn.execute(
            "INSERT INTO graph_edges (edge_id, src_node_id, dst_node_id, edge_type, weight, metadata_json, source_id)"
            " VALUES (?, 'contact:human:5d9c', ?, 'message_frequency', 1.0, ?, 'claude_code_sessions')",
            (f"edge-{old}", node_id, json.dumps({"src_node_id": "contact:human:5d9c", "dst_node_id": node_id})),
        )

    from topos.pipeline.job_store import ensure_pipeline_jobs_schema

    ensure_pipeline_jobs_schema(conn)
    for job_id, status in (("j-queued", "queued"), ("j-done", "done")):
        payload = {"source_id": "claude_code_sessions", "canonical_records": [
            {"message_id": "claude_code:a3", "conversation_id": f"claude_code_sessions:{S1}", "source_id": "claude_code_sessions"},
        ]}
        conn.execute(
            "INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, source_id, created_at, updated_at)"
            " VALUES (?, 'inbox_deferred_enrichment', ?, ?, 'claude_code_sessions', '2026-10-20', '2026-10-20')",
            (job_id, status, json.dumps(payload)),
        )
    conn.commit()


def _repair(conn, **kw):
    from topos.storage.canonical.ai_chat.coding_conversation_repair import repair_coding_conversation_ids

    return repair_coding_conversation_ids(conn, **kw)


def _messages(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT message_id, conversation_id, sequence FROM ai_chat_messages ORDER BY message_id").fetchall()]


def _conversations(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT conversation_id, source_id, owner_user_id, title, latest_title, created_at, updated_at,"
        " source_record_id, ingested_at, sync_batch_id FROM ai_chat_conversations ORDER BY conversation_id").fetchall()]


def _dump(conn):
    tables = ("ai_chat_messages", "ai_chat_conversations", "signal_embeddings", "topic_cluster_members",
              "graph_nodes", "graph_edges", "pipeline_jobs")
    return {t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()) for t in tables}


def test_the_repair_files_every_coding_message_under_tool_colon_session(node) -> None:
    result = _repair(node)

    assert result["status"] == "ok", result
    assert _messages(node) == [
        ("chatgpt:m1", "chatgpt:conv-1", 0),
        ("claude_code:a1", f"claude_code:{S1}", 0),
        ("claude_code:a2", f"claude_code:{S1}", 1),
        ("claude_code:a3", f"claude_code:{S1}", 2),
        ("claude_code:b1", f"claude_code:{S2}", 0),
        ("codex:c1", f"codex:{S3}", 0),
    ]
    assert result["messages_rewritten"] == 5
    assert result["conversations_renamed"] == 3
    assert result["conversations_merged"] == 1


def test_a_split_session_merges_into_one_conversation_row(node) -> None:
    _repair(node)

    assert _conversations(node) == [
        ("chatgpt:conv-1", "chatgpt_export", OWNER, "Trip plan", None, "2026-10-01T09:00:00Z", "2026-10-01T09:00:00Z",
         "chatgpt:conv-1", "2026-10-20T00:00:00Z", "batch-a"),
        # Earliest created_at, latest updated_at; title is the first row's (it never moves), latest_title the newest
        # row's; the bookkeeping is the newest row's, as the upsert would have left one row.
        (f"claude_code:{S1}", "claude_code_sessions", OWNER, "Lantern wiring", "Lantern wiring, part two",
         "2026-10-20T10:00:00Z", "2026-10-20T16:00:00Z", f"claude_code:{S1}", "2026-10-20T16:01:00Z",
         "claude_code_sessions:new"),
        (f"claude_code:{S2}", "claude_code_sessions", OWNER, "Uploader retry", None, "2026-10-19T09:00:00Z",
         "2026-10-19T09:00:00Z", f"claude_code:{S2}", "2026-10-20T00:00:00Z", "batch-a"),
        (f"codex:{S3}", "codex_sessions", OWNER, "Glow shader", None, "2026-10-18T09:00:00Z", "2026-10-18T09:00:00Z",
         f"codex:{S3}", "2026-10-20T00:00:00Z", "batch-a"),
    ]


def test_the_repair_carries_the_new_id_into_the_derived_copies(node) -> None:
    result = _repair(node)

    new = f"claude_code:{S1}"
    assert [tuple(r) for r in node.execute(
        "SELECT embedding_id, conversation_id, json_extract(provenance_json, '$.conversation_id')"
        " FROM signal_embeddings ORDER BY embedding_id").fetchall()] == [
        ("e1", new, new), ("e3", new, new), ("e9", "chatgpt:conv-1", "chatgpt:conv-1"),
    ]
    assert [tuple(r) for r in node.execute(
        "SELECT member_id, json_extract(metadata_json, '$.conversation_id') FROM topic_cluster_members"
        " ORDER BY member_id").fetchall()] == [("tm-e1", new), ("tm-e3", new), ("tm-e9", "chatgpt:conv-1")]
    # The two nodes of the split session become one; both edges now point at it.
    assert [tuple(r) for r in node.execute(
        "SELECT node_id, label, json_extract(metadata_json, '$.node_id') FROM graph_nodes").fetchall()] == [
        (f"conversation:{new}", new, f"conversation:{new}"),
    ]
    assert sorted(tuple(r) for r in node.execute(
        "SELECT edge_id, dst_node_id, json_extract(metadata_json, '$.dst_node_id') FROM graph_edges").fetchall()) == [
        (f"edge-claude_code_sessions:{S1}", f"conversation:{new}", f"conversation:{new}"),
        (f"edge-codex_sessions:{S1}", f"conversation:{new}", f"conversation:{new}"),
    ]
    # A job still owed rewrites under the new id; a finished job's record of what it did is left alone.
    jobs = {r[0]: json.loads(r[1]) for r in node.execute("SELECT job_id, payload_json FROM pipeline_jobs")}
    assert jobs["j-queued"]["canonical_records"][0]["conversation_id"] == new
    assert jobs["j-done"]["canonical_records"][0]["conversation_id"] == f"claude_code_sessions:{S1}"
    assert result["derived"] == {"signal_embeddings": 2, "topic_cluster_members": 2, "graph_nodes": 2,
                                 "graph_edges": 2, "pipeline_jobs": 1}


def test_a_second_run_changes_nothing(node) -> None:
    _repair(node)
    before = _dump(node)

    again = _repair(node)

    assert again["status"] == "ok"
    assert again["messages_rewritten"] == 0
    assert again["conversations_renamed"] == 0
    assert again["conversations_merged"] == 0
    assert _dump(node) == before


def test_a_dry_run_counts_and_writes_nothing(node) -> None:
    before = _dump(node)

    result = _repair(node, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["messages_rewritten"] == 5
    assert result["conversations_renamed"] == 3
    assert result["conversations_merged"] == 1
    assert _dump(node) == before


def test_rows_that_name_two_owners_are_left_alone_and_counted(node) -> None:
    node.execute("UPDATE ai_chat_conversations SET owner_user_id='someone-else' WHERE conversation_id=?",
                 (f"claude_code_sessions:{S1}",))
    node.commit()

    result = _repair(node)

    assert result["owner_conflicts"] == 1
    held = [r[0] for r in node.execute(
        "SELECT conversation_id FROM ai_chat_messages WHERE message_id LIKE 'claude_code:a%' ORDER BY message_id")]
    assert held == [f"codex_sessions:{S1}", f"codex_sessions:{S1}", f"claude_code_sessions:{S1}"]
    assert node.execute("SELECT count(*) FROM ai_chat_conversations WHERE conversation_id LIKE ?",
                        (f"%{S1}",)).fetchone()[0] == 2
    # Its derived copies stay with it.
    assert node.execute("SELECT conversation_id FROM signal_embeddings WHERE embedding_id='e1'").fetchone()[0] \
        == f"codex_sessions:{S1}"
    # The other sessions are still repaired.
    assert node.execute("SELECT conversation_id FROM ai_chat_messages WHERE message_id='claude_code:b1'").fetchone()[0] \
        == f"claude_code:{S2}"


def test_a_node_without_the_ai_chat_tables_is_skipped(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    try:
        assert _repair(conn)["status"] == "skipped"
    finally:
        conn.close()


def test_the_upgrade_step_runs_the_repair() -> None:
    """The unreleased manifest declares the step, and the runner's engine-endpoint dispatch reaches the repair."""
    import topos.upgrades as upgrades

    manifest = json.loads((Path(upgrades.__file__).parent / "manifests.json").read_text())
    unreleased = next(r for r in manifest["releases"] if r["version"] == "unreleased")
    step = next(s for s in unreleased["steps"] if s["id"] == "repair-coding-session-conversation-ids")
    assert step["kind"] == "engine_endpoint"
    assert step["params"]["path"] == "/v1/canonical/ai-chat/repair-coding-conversation-ids"
    assert step["consent"] == "auto"


def test_the_engine_endpoint_dispatch_repairs_the_node(node) -> None:
    from topos.upgrades.runner import _exec_engine_endpoint

    detail = _exec_engine_endpoint(
        {"params": {"method": "POST", "path": "/v1/canonical/ai-chat/repair-coding-conversation-ids"}}, node,
    )

    assert detail["messages_rewritten"] == 5
    assert node.execute("SELECT count(*) FROM ai_chat_messages WHERE conversation_id LIKE '%_sessions:%'"
                        ).fetchone()[0] == 0


# --- review round (verifier, 20 Oct): states the first fixture did not have --------------------------------


def _path(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def test_a_lane_write_already_under_the_canonical_id_merges_with_the_old_rows(node) -> None:
    """The step runs after the UI grace window, so the fixed lane may already have filed new turns under the new id."""
    _message(node, "claude_code:a4", f"claude_code:{S1}", "claude_code_sessions", "2026-10-20T17:00:00Z", 0)
    _conversation(node, f"claude_code:{S1}", "claude_code_sessions", "2026-10-20T17:00:00Z", "2026-10-20T17:00:00Z",
                  "Lantern wiring, part three", "Lantern wiring, part three",
                  ingested_at="2026-10-20T17:01:00Z", sync_batch_id="claude_code_sessions:fixed")
    node.commit()

    result = _repair(node)

    assert result["conversations_merged"] == 1
    assert [r for r in _messages(node) if r[0].startswith("claude_code:a")] == [
        ("claude_code:a1", f"claude_code:{S1}", 0),
        ("claude_code:a2", f"claude_code:{S1}", 1),
        ("claude_code:a3", f"claude_code:{S1}", 2),
        ("claude_code:a4", f"claude_code:{S1}", 3),
    ]
    assert [r for r in _conversations(node) if r[0] == f"claude_code:{S1}"] == [
        (f"claude_code:{S1}", "claude_code_sessions", OWNER, "Lantern wiring", "Lantern wiring, part three",
         "2026-10-20T10:00:00Z", "2026-10-20T17:00:00Z", f"claude_code:{S1}", "2026-10-20T17:01:00Z",
         "claude_code_sessions:fixed"),
    ]


def test_the_repair_is_committed_for_other_connections(node) -> None:
    _repair(node)

    other = sqlite3.connect(_path(node))
    try:
        assert other.execute("SELECT count(*) FROM ai_chat_messages WHERE conversation_id LIKE '%_sessions:%'"
                             ).fetchone()[0] == 0
        assert other.execute("SELECT sequence FROM ai_chat_messages WHERE message_id='claude_code:a3'").fetchone()[0] == 2
    finally:
        other.close()


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_a_failure_part_way_leaves_every_row_as_it_was(node, monkeypatch, error) -> None:
    from topos.storage.canonical.ai_chat import coding_conversation_repair as repair

    before = _dump(node)

    def boom(*args, **kwargs):
        raise error("synthetic failure after the canonical rename")

    monkeypatch.setattr(repair, "_repair_derived", boom)
    with pytest.raises(error):
        _repair(node)

    assert node.in_transaction is False
    assert _dump(node) == before
    other = sqlite3.connect(_path(node))
    try:
        assert other.execute("SELECT count(*) FROM ai_chat_messages WHERE conversation_id LIKE '%_sessions:%'"
                             ).fetchone()[0] == 5
    finally:
        other.close()


def test_failed_and_running_jobs_are_owed_and_rewritten(node) -> None:
    for job_id, status in (("j-failed", "failed"), ("j-running", "running")):
        node.execute(
            "INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, source_id, created_at, updated_at)"
            " VALUES (?, 'inbox_deferred_enrichment', ?, ?, 'claude_code_sessions', '2026-10-20', '2026-10-20')",
            (job_id, status, json.dumps({"canonical_records": [{"conversation_id": f"codex_sessions:{S1}"}]})),
        )
    node.commit()

    _repair(node)

    jobs = {r[0]: json.loads(r[1]) for r in node.execute("SELECT job_id, payload_json FROM pipeline_jobs")}
    assert jobs["j-failed"]["canonical_records"][0]["conversation_id"] == f"claude_code:{S1}"
    assert jobs["j-running"]["canonical_records"][0]["conversation_id"] == f"claude_code:{S1}"


def test_unknown_shapes_and_other_sources_are_left_alone_and_counted(node) -> None:
    _message(node, "claude_code:x9", "chatgpt:x", "claude_code_sessions", "2026-10-20T09:00:00Z", 0)
    # A non-coding row whose id merely looks like a coding one.
    _message(node, "chatgpt:m2", "codex_sessions:not-a-session", "chatgpt_export", "2026-10-02T09:00:00Z", 0)
    node.commit()

    result = _repair(node)

    held = dict(node.execute("SELECT message_id, conversation_id FROM ai_chat_messages"
                             " WHERE message_id IN ('claude_code:x9', 'chatgpt:m2')").fetchall())
    assert held == {"claude_code:x9": "chatgpt:x", "chatgpt:m2": "codex_sessions:not-a-session"}
    assert result["left_alone"] == 1
    assert result["by_source"] == {"claude_code_sessions": 2, "codex_sessions": 1, "cursor_sessions": 0}


def test_the_connection_row_factory_is_put_back(node) -> None:
    node.row_factory = None

    _repair(node)

    assert node.row_factory is None


def test_derived_rows_written_with_an_old_id_after_the_repair_are_fixed_by_the_next_run(node) -> None:
    """A deferred job already running when the repair committed holds its payload in memory and writes the old id."""
    _repair(node)
    old = f"codex_sessions:{S1}"
    node.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, model, provenance_json,"
        " conversation_id) VALUES ('e2', 'claude_code:a2', 'claude_code_sessions', 'memory', 'm', ?, ?)",
        (json.dumps({"conversation_id": old}), old),
    )
    node.execute(
        "INSERT INTO graph_nodes (node_id, node_type, label, metadata_json, source_id)"
        " VALUES (?, 'conversation', ?, '{}', 'claude_code_sessions')", (f"conversation:{old}", old[:48]),
    )
    node.execute(
        "INSERT INTO graph_edges (edge_id, src_node_id, dst_node_id, edge_type, weight, metadata_json, source_id)"
        " VALUES ('edge-late', 'contact:human:5d9c', ?, 'message_frequency', 1.0, '{}', 'claude_code_sessions')",
        (f"conversation:{old}",),
    )
    node.commit()

    again = _repair(node)

    new = f"claude_code:{S1}"
    assert again["messages_rewritten"] == 0
    assert again["derived"]["signal_embeddings"] == 1
    assert tuple(node.execute("SELECT conversation_id, json_extract(provenance_json, '$.conversation_id')"
                              " FROM signal_embeddings WHERE embedding_id='e2'").fetchone()) == (new, new)
    assert [r[0] for r in node.execute("SELECT node_id FROM graph_nodes")] == [f"conversation:{new}"]
    assert node.execute("SELECT dst_node_id FROM graph_edges WHERE edge_id='edge-late'").fetchone()[0] \
        == f"conversation:{new}"


def test_two_sources_sharing_one_stored_id_each_get_their_own_tool(node) -> None:
    """The shim's no-thread fallback named the dataset, so a Claude Code and a Codex row could share one id."""
    shared = "codex_sessions:owner:topos:t1"
    _message(node, "claude_code:d1", shared, "claude_code_sessions", "2026-10-17T09:00:00Z", 0)
    _message(node, "codex:d2", shared, "codex_sessions", "2026-10-17T09:00:01Z", 0)
    for embedding_id, record_id, source_id in (("ed1", "claude_code:d1", "claude_code_sessions"),
                                               ("ed2", "codex:d2", "codex_sessions")):
        node.execute(
            "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, model,"
            " provenance_json, conversation_id) VALUES (?, ?, ?, 'memory', 'm', ?, ?)",
            (embedding_id, record_id, source_id, json.dumps({"conversation_id": shared}), shared),
        )
    node.commit()

    _repair(node)

    assert dict(node.execute("SELECT message_id, conversation_id FROM ai_chat_messages"
                             " WHERE message_id IN ('claude_code:d1', 'codex:d2')").fetchall()) == {
        "claude_code:d1": "claude_code:owner:topos:t1", "codex:d2": "codex:owner:topos:t1",
    }
    assert dict(node.execute("SELECT embedding_id, conversation_id FROM signal_embeddings"
                             " WHERE embedding_id IN ('ed1', 'ed2')").fetchall()) == {
        "ed1": "claude_code:owner:topos:t1", "ed2": "codex:owner:topos:t1",
    }


def test_a_column_a_later_migration_adds_survives_a_merge(node) -> None:
    node.execute("ALTER TABLE ai_chat_conversations ADD COLUMN later_column TEXT")
    node.execute("UPDATE ai_chat_conversations SET later_column='kept' WHERE conversation_id=?",
                 (f"claude_code_sessions:{S1}",))
    node.commit()

    _repair(node)

    assert node.execute("SELECT later_column FROM ai_chat_conversations WHERE conversation_id=?",
                        (f"claude_code:{S1}",)).fetchone()[0] == "kept"
