"""NR189 (review note 1): an AI-chat row the owner keeps from every share never reaches a topic cluster's label.

`ai_chat_messages.share_excluded` can be set on a row that was already stored and embedded: the AI-tool sync
re-applies the flag to rows it wrote before, and a metadata rewrite re-fires the trigger. The flagging pass removes the
row's timeline projection, not its embedding. Topic clustering read every embedding of its sources, a cluster's label is
minted from its members' text (term counts over the previews, and the LLM labeler over the hydrated canonical text),
and an interest's label is its cluster's label. So the flagged row's words could become a label a share releases.

The recompute's loader (`load_embedding_records`) and the relabel's (`load_clusters_with_members`) now leave out any
record whose AI-chat row is flagged; a database without the column clusters exactly as before.

Every word, id and vector here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.signal.topic_clustering import (load_embedding_records, recompute_topic_clusters,
                                                     relabel_existing_clusters)
from topos.storage.canonical.ai_chat import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations

SOURCE = "chatgpt_file_ingestion"
WORD = "zephyrwick"
PLAIN = {"p1": "lantern repair on the porch", "p2": "lantern wiring and repair"}
FLAGGED = {"f1": f"{WORD} {WORD} lantern notes", "f2": f"{WORD} lantern {WORD} draft"}
BROWSING = {"b1": "tide tables for the coast", "b2": "coast tide chart"}


@pytest.fixture(autouse=True)
def _term_labels(monkeypatch):
    """The deterministic term labeler for the recompute (no model is asked); the relabel test passes its own."""
    monkeypatch.setenv("TOPOS_CLUSTER_LLM_LABELS", "off")


def _node(tmp_path, *, column: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "clusters.db"))
    conn.row_factory = sqlite3.Row
    CanonicalTablesManager(conn)          # the AI-chat tables, as the first import makes them
    apply_all_migrations(conn)            # migration 85 adds share_excluded to them
    if not column:
        from topos.storage.db.migrations.ai_coding_sessions_v1 import TRIGGER_ON_INSERT, TRIGGER_ON_METADATA
        for trigger in (TRIGGER_ON_INSERT, TRIGGER_ON_METADATA):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP INDEX IF EXISTS idx_ai_chat_messages_share_excluded")
        conn.execute("ALTER TABLE ai_chat_messages DROP COLUMN share_excluded")
    conn.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, "
                 "updated_at) VALUES ('chat-1', 'owner-1', 'Porch', ?, '2026-09-01T09:00:00Z', '2026-09-01T09:00:00Z')",
                 (SOURCE,))
    rows = [(rid, "user", text) for rid, text in PLAIN.items()] + [(rid, "assistant", text) for rid, text in FLAGGED.items()]
    for sequence, (rid, sender, text) in enumerate(rows):
        conn.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, "
                     "sequence, source_id) VALUES (?, 'chat-1', ?, '2026-09-01T10:00:00Z', ?, ?, ?)",
                     (rid, sender, text, sequence, SOURCE))
    vectors = {"p1": (1.0, 0.0), "p2": (0.97, 0.03), "f1": (0.99, 0.01), "f2": (0.98, 0.02),
               "b1": (0.0, 1.0), "b2": (0.03, 0.97)}
    texts = {**PLAIN, **FLAGGED, **BROWSING}
    for index, (rid, vector) in enumerate(vectors.items()):
        conn.execute("INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, model, "
                     "provider, dims, text_preview, provenance_json, vector_blob) VALUES (?, ?, ?, 'memory', 'test', "
                     "'test', 2, ?, '{}', ?)",
                     (f"emb-{index}", rid, "browser_visits" if rid.startswith("b") else SOURCE, texts[rid],
                      json.dumps(list(vector)).encode("utf-8")))
    conn.commit()
    return conn


def _flag(conn) -> None:
    conn.executemany("UPDATE ai_chat_messages SET share_excluded = 1 WHERE message_id = ?", [(rid,) for rid in FLAGGED])
    conn.commit()


def _stored_text(conn) -> str:
    """Every text a stored cluster carries: its label, terms, preview and metadata, and its members' previews."""
    parts = []
    for row in conn.execute("SELECT * FROM topic_clusters"):
        parts.extend(str(value) for value in dict(row).values() if value is not None)
    parts.extend(str(row[0]) for row in conn.execute("SELECT text_preview FROM topic_cluster_members"))
    return " ".join(parts).lower()


@pytest.mark.parametrize("column", [True, False], ids=["flagged", "no_column"])
def test_a_recompute_never_clusters_a_flagged_row(tmp_path, column):
    conn = _node(tmp_path, column=column)
    if column:
        _flag(conn)
    loaded = {record["record_id"] for record in load_embedding_records(conn, source_ids=[SOURCE, "browser_visits"])}
    assert loaded == ({"p1", "p2", "b1", "b2"} if column else {"p1", "p2", "f1", "f2", "b1", "b2"})
    result = recompute_topic_clusters(conn, source_ids=[SOURCE, "browser_visits"], min_records=2, k=2)
    members = {row[0] for row in conn.execute("SELECT record_id FROM topic_cluster_members")}
    assert result["members_written"] == len(loaded) and members == loaded
    if column:
        assert WORD not in _stored_text(conn)
    else:                                  # as before: the words are there to label with
        assert WORD in _stored_text(conn)
    conn.close()


def test_a_relabel_never_reads_a_member_flagged_after_the_build(tmp_path):
    """Clusters built before the flag keep their stored members (membership is never moved by a relabel); the relabel
    no longer reads a flagged one, neither its stored preview nor its canonical text, so its words reach no prompt and
    no new label."""
    conn = _node(tmp_path)
    recompute_topic_clusters(conn, source_ids=[SOURCE, "browser_visits"], min_records=2, k=2)
    assert {row[0] for row in conn.execute("SELECT record_id FROM topic_cluster_members")} >= set(FLAGGED)
    _flag(conn)
    prompts = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return "Porch Lantern Work" if "lantern" in prompt.lower() else "Coastal Tides"

    outcome = relabel_existing_clusters(conn, complete=complete, mode="on")
    assert outcome["status"] == "completed" and prompts
    assert not [prompt for prompt in prompts if WORD in prompt.lower()]
    labels = " ".join(str(row[0]) for row in conn.execute("SELECT label FROM topic_clusters")).lower()
    assert WORD not in labels
    # The stored membership is the relabel's to keep: the flagged members are still on disk until the next recompute.
    assert {row[0] for row in conn.execute("SELECT record_id FROM topic_cluster_members")} >= set(FLAGGED)
    conn.close()


def test_the_record_filter_keeps_other_kinds_and_leaves_out_a_null_too():
    """The filter both loaders use: a record with no AI-chat row (a visit, a journal entry) is kept; an AI-chat row is
    kept only when its value is 0, so a NULL is left out, as the SQL `share_excluded = 0` leaves it out."""
    from topos.storage.share_excluded import share_excluded_record_filter
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, share_excluded INTEGER)")   # nullable
    conn.execute("INSERT INTO ai_chat_messages VALUES ('a', 0), ('n', NULL), ('f', 1)")
    conn.execute("CREATE TABLE records (record_id TEXT)")
    conn.executemany("INSERT INTO records VALUES (?)", [("a",), ("n",), ("f",), ("visit-1",)])
    kept = share_excluded_record_filter(conn, record_column="records.record_id")
    assert sorted(row[0] for row in conn.execute(f"SELECT record_id FROM records WHERE 1{kept}")) == ["a", "visit-1"]
    bare = sqlite3.connect(":memory:")
    assert share_excluded_record_filter(bare, record_column="records.record_id") == ""     # no AI-chat table: as before
    conn.close()
    bare.close()
