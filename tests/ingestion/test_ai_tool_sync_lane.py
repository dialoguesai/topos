"""The AI-coding-tool sync runner (CONTRACTS.md §2.8): what it refuses, what it writes, what it keeps out.

Guards from SPRINTS S5: the sync is refused unless the source is enabled in the active profile;
the caller's dataset is ignored (identity decides); rows carry ``writer_class='owner_app'``;
records the reader flagged ``share_excluded`` land with ``share_excluded=1`` and nothing derived
references them; the project facet never reaches ``metadata_json``; a backfill below the disk
floor refuses with the plain sentence; the result carries the reader's ledger. Synthetic node
databases and invented records only; the reader, parser and mapper are fakes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.ingestion.ai_tool_fixtures import (
    DATASET,
    OTHER_DATASET,
    OWNER,
    TOPOS,
    FakeReader,
    arm,
    build_node,
    chat_rows,
    enable,
    record,
)

SOURCE = "claude_code_sessions"
SCHEMA = "claude_code.transcript.v1"
SESSION = "sess-0001"


def _batch():
    return [
        record("claude_code", "u1", SESSION, "user", "make the lantern glow", turn_index=0, project_key="glowworks"),
        record("claude_code", "u2", SESSION, "assistant", "I will wire the glow", turn_index=1,
               when="2026-10-14T10:00:01.000Z", project_key="glowworks"),
        record("claude_code", "u3", SESSION, "user", "now ask my topos about it", turn_index=2,
               when="2026-10-14T10:00:02.000Z", project_key="glowworks"),
        record("claude_code", "u4", SESSION, "assistant", "your topos says the glow is amber", turn_index=3,
               when="2026-10-14T10:00:03.000Z", share_excluded=True, project_key="glowworks"),
    ]


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = build_node(tmp_path / "node.db")
    yield conn
    conn.close()


def _run(dataset_id: str = DATASET, **kw):
    from topos.ingestion.ai_tool_sync import run_claude_code_sync

    return run_claude_code_sync(dataset_id, **kw)


# --- refusals ------------------------------------------------------------------


def test_a_sync_is_refused_unless_the_source_is_enabled_in_the_active_profile(node, monkeypatch) -> None:
    reader = arm(monkeypatch, node, reader=FakeReader(_batch()))
    result = _run()
    assert result["status"] == "error" and result["code"] == "source_not_enabled", result
    assert result["records_processed"] == 0
    assert reader.calls == [], "nothing may be read before the enabled check passes"
    assert chat_rows(node) == []


def test_an_enabled_flag_alone_is_not_enough_without_the_install_row(node, monkeypatch) -> None:
    """``PUT settings {enabled: true}`` is writable over TCP; the install row needs the owner socket."""
    from topos.storage.source_settings import put_source_settings

    arm(monkeypatch, node, reader=FakeReader(_batch()))
    put_source_settings(node, DATASET, SOURCE, enabled=True)
    result = _run()
    assert result["status"] == "error" and result["code"] == "source_not_enabled"


def test_a_paused_source_refuses_the_sync(node, monkeypatch) -> None:
    from topos.ingestion.ai_tool_sync import set_source_state

    reader = arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    set_source_state(node, SOURCE, paused=True)
    result = _run()
    assert result["status"] == "error" and result["code"] == "source_paused", result
    assert reader.calls == []


def test_an_unpaired_node_refuses_with_not_paired(tmp_path, monkeypatch) -> None:
    conn = build_node(tmp_path / "unpaired.db", topos_id=None)
    arm(monkeypatch, conn, reader=FakeReader(_batch()))
    result = _run()
    assert result["status"] == "error" and result["code"] == "not_paired"


def test_refusals_write_no_receipt_row(node, monkeypatch) -> None:
    """A refused run must not INSERT a user_ingestion_sources row (the row defaults to enabled=1)."""
    from topos.ingestion.local_sync_jobs import no_receipt_codes

    assert {"source_not_enabled", "source_paused", "not_paired", "sync_in_progress", "dataset_not_enrolled"} <= set(
        no_receipt_codes()
    )


def test_a_parser_that_is_not_bundled_is_refused(node, monkeypatch) -> None:
    """A runtime install builds a parser for any schema it cannot find; it may never read a transcript."""
    import topos.ingestion.ai_tool_sync as lane

    reader = arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    monkeypatch.setattr(lane, "_parser_for", lambda schema_id: None)
    result = _run()
    assert result["status"] == "error" and result["code"] == "parser_unavailable"
    assert reader.calls == [] and chat_rows(node) == []


# --- identity ------------------------------------------------------------------


def test_the_callers_dataset_is_ignored_and_the_bound_identity_decides(node, monkeypatch) -> None:
    arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    result = _run(OTHER_DATASET)
    assert result["status"] == "ok", result
    assert result["dataset_id"] == DATASET
    rows = chat_rows(node)
    assert rows and {r["writer_dataset_id"] for r in rows} == {DATASET}
    checkpoints = node.execute("SELECT dataset_id, schema_id FROM ingestion_checkpoints").fetchall()
    assert [tuple(c) for c in checkpoints] == [(DATASET, SCHEMA)]
    assert OTHER_DATASET not in json.dumps([dict(r) for r in rows])


def test_the_scope_comes_from_the_stored_topos_id_when_nothing_is_bound(node, monkeypatch) -> None:
    from topos.ingestion.ai_tool_sync import resolve_owner_scope

    arm(monkeypatch, node)
    assert resolve_owner_scope(node) == {
        "user_id": OWNER, "topos_id": TOPOS, "dataset_id": DATASET, "identity_source": "stored",
    }


# --- what lands ----------------------------------------------------------------


def test_rows_carry_the_owner_app_writer_class(node, monkeypatch) -> None:
    arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    result = _run()
    assert result["status"] == "ok" and result["records_processed"] == 4, result
    rows = chat_rows(node)
    assert len(rows) == 4
    assert {r["writer_class"] for r in rows} == {"owner_app"}
    assert {r["source_id"] for r in rows} == {SOURCE}
    assert {r["conversation_id"] for r in rows} == {f"claude_code:{SESSION}"}
    title = node.execute("SELECT title FROM ai_chat_conversations").fetchone()[0]
    assert title == "Build the lantern"


def test_flagged_records_land_with_share_excluded_1_and_the_rest_with_0(node, monkeypatch) -> None:
    arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    result = _run()
    assert result["status"] == "ok"
    flags = {r["message_id"]: r["share_excluded"] for r in chat_rows(node)}
    assert flags == {"claude_code:u1": 0, "claude_code:u2": 0, "claude_code:u3": 0, "claude_code:u4": 1}
    assert result["records_share_excluded"] == 1


def test_a_flagged_row_is_excluded_the_moment_it_is_written(node, monkeypatch) -> None:
    """The flag must travel WITH the canonical write (CONTRACTS §2.6), never arrive by a later UPDATE: a sync
    that dies between the two writes (here: the post-write mark raises) must still leave the tainted row
    withheld, with no timeline projection of it, and a later run must not treat the row as an already-stored
    duplicate it may leave shareable."""
    import topos.ingestion.ai_tool_sync as lane

    # The same batch twice: the second run re-reads rows the first one already stored.
    arm(monkeypatch, node, reader=FakeReader(_batch(), _batch()))
    enable(node, SOURCE)

    def _killed(*_a, **_k):
        raise RuntimeError("killed between the canonical write and the post-write mark")

    monkeypatch.setattr(lane, "_mark_share_excluded", _killed)
    result = _run()
    assert result["status"] == "error", result
    flags = {r["message_id"]: r["share_excluded"] for r in chat_rows(node)}
    assert flags["claude_code:u4"] == 1, flags
    assert flags["claude_code:u1"] == 0 and flags["claude_code:u2"] == 0 and flags["claude_code:u3"] == 0
    assert node.execute("SELECT count(*) FROM timeline WHERE record_id = 'claude_code:u4'").fetchone()[0] == 0
    assert node.execute("SELECT count(*) FROM timeline WHERE record_id = 'claude_code:u2'").fetchone()[0] == 1

    # The rows exist now (first writer wins); the next run re-reads the same batch and must leave u4 withheld.
    monkeypatch.setattr(lane, "_mark_share_excluded", lambda *_a, **_k: 0)
    again = _run()
    assert again["status"] == "ok", again
    flags = {r["message_id"]: r["share_excluded"] for r in chat_rows(node)}
    assert flags["claude_code:u4"] == 1, flags


def test_nothing_derived_references_a_share_excluded_row(node, monkeypatch) -> None:
    """The jobs run on this database through the real post-canonical pipeline, with the model work faked
    into rows: whatever the orchestrators are handed gets an entity, a topic, an embedding and a fact."""
    from topos.enrichment import orchestrator as orch
    from topos.ingestion import canonical_pipeline

    handed: dict = {"canonical": [], "signal": []}

    def _write_derived(conn, records, tag):
        for rec in records:
            rid = rec["message_id"]
            conn.execute("INSERT OR REPLACE INTO message_entities (entity_id, record_id, source_id, payload_json) VALUES (?, ?, ?, '{}')",
                         (f"ent-{tag}-{rid}", rid, rec.get("source_id")))
            conn.execute("INSERT OR REPLACE INTO message_topics (topic_id, record_id, source_id, payload_json) VALUES (?, ?, ?, '{}')",
                         (f"top-{tag}-{rid}", rid, rec.get("source_id")))
            conn.execute("INSERT OR REPLACE INTO signal_embeddings (embedding_id, record_id, source_id) VALUES (?, ?, ?)",
                         (f"emb-{tag}-{rid}", rid, rec.get("source_id")))
            conn.execute("INSERT OR REPLACE INTO signal_facts (fact_id, record_id, source_id, payload_json) VALUES (?, ?, ?, '{}')",
                         (f"fact-{tag}-{rid}", rid, rec.get("source_id")))
            conn.execute("INSERT OR REPLACE INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json) VALUES (?, ?, ?, ?, '{}')",
                         (f"goal-{tag}-{rid}", rid, rec.get("source_id"), "glow"))
        conn.commit()

    async def fake_canonical(self, records, job_names=None, progress_callback=None):
        handed["canonical"].extend(r["message_id"] for r in records)
        _write_derived(node, records, "c")
        return {"jobs_run": len(job_names or [])}

    async def fake_signal(self, records, *, source_id=None, sync_batch_id=None, job_names=None, progress_callback=None):
        handed["signal"].extend(r["message_id"] for r in records)
        _write_derived(node, records, "s")
        return {"jobs_run": 1}

    async def fake_privacy(conn, records, *, source_group=None, progress_callback=None):
        return {"rows": len(records)}

    monkeypatch.setattr(orch.EnrichmentOrchestrator, "run_canonical", fake_canonical)
    monkeypatch.setattr(orch.SignalDerivationOrchestrator, "run_signal_derivation", fake_signal)
    monkeypatch.setattr("topos.disclosure.privacy_layer.run_privacy_disclosure_layer", fake_privacy)
    monkeypatch.setattr("topos.features.entities.graph_refresh.refresh_now_if_dirty", lambda: {"skipped": "test"})
    monkeypatch.setattr(canonical_pipeline, "_record_episode", lambda **kw: None)

    arm(monkeypatch, node, reader=FakeReader(_batch()), post_canonical=None)
    enable(node, SOURCE)
    result = _run()
    assert result["status"] == "ok", result
    # Positive control: the jobs ran and derived from the kept rows.
    assert set(handed["canonical"]) == {"claude_code:u1", "claude_code:u2", "claude_code:u3"}
    assert set(handed["signal"]) == {"claude_code:u1", "claude_code:u2", "claude_code:u3"}
    for table in ("message_entities", "message_topics", "signal_embeddings", "signal_facts", "user_goals", "entity_mentions", "timeline"):
        refs = [r[0] for r in node.execute(f"SELECT record_id FROM {table} WHERE record_id = ?", ("claude_code:u4",))]
        assert refs == [], f"{table} references the share_excluded row"
    assert node.execute("SELECT COUNT(*) FROM message_entities WHERE record_id='claude_code:u1'").fetchone()[0] >= 1


def test_the_project_facet_is_stored_owner_only_and_never_in_metadata_json(node, monkeypatch) -> None:
    arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    assert _run()["status"] == "ok"
    for row in chat_rows(node):
        assert "project_key" not in (row["metadata_json"] or "")
        assert "glowworks" not in (row["metadata_json"] or "")
        assert "_facets" not in (row["metadata_json"] or "")
    facets = node.execute("SELECT message_id, tool, project_key FROM ai_chat_message_facets ORDER BY message_id").fetchall()
    assert [tuple(f) for f in facets] == [
        ("claude_code:u1", "claude_code", "glowworks"),
        ("claude_code:u2", "claude_code", "glowworks"),
        ("claude_code:u3", "claude_code", "glowworks"),
        ("claude_code:u4", "claude_code", "glowworks"),
    ]
    raw = node.execute("SELECT payload_json FROM raw_chat_messages_claude_code").fetchall()
    assert raw and all("glowworks" not in r[0] and "_facets" not in r[0] for r in raw)


def test_the_result_carries_the_ledger_and_the_checkpoint_is_the_newest_created_at(node, monkeypatch) -> None:
    reader = arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    result = _run()
    assert result["status"] == "ok" and result["outcome"] == "imported"
    assert result["ledger"] == {
        "turns_emitted": 4, "dropped": {"tool_use": 3}, "dropped_total": 3, "scrubbed": {}, "tainted_turns": 1,
        "secret_sightings": {}, "sessions_with_secrets": 0,
    }
    assert result["last_record_id"] == "2026-10-14T10:00:03.000Z"
    row = node.execute("SELECT last_record_id FROM ingestion_checkpoints WHERE dataset_id=? AND schema_id=?",
                       (DATASET, SCHEMA)).fetchone()
    assert row[0] == "2026-10-14T10:00:03.000Z"
    # The next since-last run tells the reader where it stopped.
    reader.batches = [[]]
    again = _run()
    assert again["status"] == "ok" and again["outcome"] == "up_to_date"
    assert reader.calls[-1]["since_unix"] == pytest.approx(1791972003.0)
    assert reader.calls[0]["since_unix"] is None


def test_a_session_path_reaches_the_reader_and_a_sync_is_held_per_dataset(node, monkeypatch) -> None:
    from topos.ingestion.local_sync import exclusive_sync

    reader = arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    result = _run(session_path="/invented/session.jsonl", sync_options={"mode": "since_last", "trigger": "hook"})
    assert result["status"] == "ok"
    assert reader.calls[0]["session_path"] == "/invented/session.jsonl"
    with exclusive_sync(SOURCE, DATASET) as held:
        assert held
        busy = _run()
    assert busy["status"] == "error" and busy["code"] == "sync_in_progress"


def test_the_skip_list_from_enable_reaches_the_reader(node, monkeypatch) -> None:
    reader = arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE, skip=["secret-client", "scratch"])
    assert _run()["status"] == "ok"
    assert reader.calls[0]["skip_folders"] == ("secret-client", "scratch")


def test_a_backfill_below_the_disk_floor_refuses_with_the_plain_sentence(node, monkeypatch) -> None:
    import topos.engine.disk_space as disk

    reader = arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    monkeypatch.setattr(disk, "free_bytes", lambda path=None: 2_000_000_000)
    monkeypatch.setattr(disk, "min_free_bytes", lambda conn=None: 10_000_000_000)
    result = _run(sync_options={"mode": "full_history"})
    assert result["status"] == "error" and result["code"] == "disk_floor"
    assert result["error"] == (
        "There isn't enough free disk space to bring in your coding sessions: 2.0 GB is free and Topos keeps "
        "10.0 GB free. Free up space, then try again."
    )
    assert reader.calls == [] and chat_rows(node) == []


def test_a_fork_copy_of_the_same_record_is_a_no_op(node, monkeypatch) -> None:
    """First writer wins (§2.1): a resume/fork copy of the same uuid neither rewrites nor duplicates."""
    first = _batch()
    forked = [dict(r, content="rewritten by a fork") for r in _batch()]
    for r in forked:
        r["_metadata"] = dict(r["_metadata"])
    arm(monkeypatch, node, reader=FakeReader(first, forked))
    enable(node, SOURCE)
    result = _run(batch_size=4)
    assert result["status"] == "ok"
    contents = {r["message_id"]: r["content"] for r in chat_rows(node)}
    assert contents["claude_code:u1"] == "make the lantern glow"
    assert len(contents) == 4


def test_the_full_text_lands_untruncated_in_raw_and_canonical_rows(node, monkeypatch) -> None:
    """Spike B: the privacy filter truncates at 8,000 characters (dropped, not redacted), so the reader's
    deterministic scrub on the FULL text is the only pass that sees every character; this lane must hand
    that full text on, never a prefix."""
    long_text = "lantern " * 1125  # 9,000 characters
    rows = [record("claude_code", "long1", SESSION, "user", long_text, project_key="glowworks")]
    arm(monkeypatch, node, reader=FakeReader(rows))
    enable(node, SOURCE)
    assert _run()["status"] == "ok"
    stored = node.execute("SELECT content FROM ai_chat_messages WHERE message_id='claude_code:long1'").fetchone()[0]
    assert len(stored) == 9000 and stored == long_text
    raw = node.execute("SELECT payload_json FROM raw_chat_messages_claude_code WHERE source_record_id='claude_code:long1'").fetchone()[0]
    assert json.loads(raw)["content"] == long_text


def test_secret_sightings_are_persisted_as_a_count_and_never_a_value(node, monkeypatch) -> None:
    """L3's doctor reads ``secret_sightings`` off the source's status: session files in which the node's own
    secret was scrubbed. The number lands; the secret itself appears nowhere on disk or in the result."""
    import topos.ingestion.ai_tool_sync as lane
    from topos.core.state import get_engine_config_value
    from topos.ingestion.ai_tool_sync import describe_ai_tool_source

    secret = "invented-owner-secret-value-7f3a"
    monkeypatch.setattr(lane, "node_secret_values", lambda conn=None: (secret,))
    reader = arm(monkeypatch, node, reader=FakeReader(_batch(), secret_sightings=2))
    enable(node, SOURCE)
    result = _run()
    assert result["status"] == "ok"
    assert reader.calls[0]["secrets"] == (secret,), "the reader is handed the exact values to scrub"
    assert result["ledger"]["sessions_with_secrets"] == 2
    status = describe_ai_tool_source(node, SOURCE)
    assert status["secret_sightings"] == 2
    assert status["last_sync_ledger"]["sessions_with_secrets"] == 2 and status["last_sync_outcome"] == "imported"
    stored = get_engine_config_value(node, "ai_tool_sources") or ""
    assert '"secret_sightings": 2' in stored
    assert secret not in stored and secret not in json.dumps(result, default=str) and secret not in json.dumps(status, default=str)


def test_a_facets_sibling_block_is_lifted_too(node, monkeypatch) -> None:
    """The other spelling of the facet (a `_facets` block beside the record) leaves the row the same way."""
    rows = _batch()
    for row in rows:
        row["_facets"] = {"project_key": row.pop("project_key")}
    arm(monkeypatch, node, reader=FakeReader(rows))
    enable(node, SOURCE)
    assert _run()["status"] == "ok"
    assert all("glowworks" not in (r["metadata_json"] or "") and "_facets" not in (r["metadata_json"] or "") for r in chat_rows(node))
    assert node.execute("SELECT COUNT(*) FROM ai_chat_message_facets WHERE project_key='glowworks'").fetchone()[0] == 4


def test_reader_receipt_fields_pass_through_to_the_result_and_the_job_summary(node, monkeypatch) -> None:
    from topos.ingestion.ai_tool_sync import ai_tool_post_sync

    reader = FakeReader(_batch(), receipt={"imported_sessions": 3, "composers_jsonl_preferred": 2, "composers_no_rewind": 1})
    arm(monkeypatch, node, reader=reader)
    enable(node, SOURCE)
    result = _run()
    assert result["status"] == "ok"
    assert result["reader_receipt"] == {"imported_sessions": 3, "composers_jsonl_preferred": 2, "composers_no_rewind": 1}
    summary = ai_tool_post_sync(node, DATASET, result, {"trigger": "hook"})["sync"]
    assert summary["reader_receipt"] == result["reader_receipt"] and summary["trigger"] == "hook"


def test_cursor_passes_the_composer_id_and_the_state_db_path_through(node, monkeypatch) -> None:
    """Without transcripts, Cursor's hook has only a conversation id: it travels as session_path, untouched."""
    from topos.ingestion.ai_tool_sync import run_cursor_sync

    reader = arm(monkeypatch, node, reader=FakeReader([
        record("cursor", "c1", "composer-0abc", "user", "ask the cursor", project_key="glowworks"),
    ]))
    enable(node, "cursor_sessions")
    result = run_cursor_sync(DATASET, session_path="composer-0abc", state_db_path="/invented/state.vscdb")
    assert result["status"] == "ok", result
    assert reader.calls[0]["session_path"] == "composer-0abc"
    assert reader.calls[0]["state_db_path"] == "/invented/state.vscdb"
    # The other tools never receive a Cursor-only keyword.
    claude = arm(monkeypatch, node, reader=FakeReader(_batch()))
    enable(node, SOURCE)
    assert _run(sync_options={"state_db_path": "/invented/state.vscdb"})["status"] == "ok"
    assert "state_db_path" not in claude.calls[0]


def test_the_loop_runs_until_the_reader_touched_no_file(node, monkeypatch) -> None:
    """A batch short of batch_size still has files behind it (drops, a small file); only files_touched == 0 ends it."""
    first = _batch()[:2]
    second = [record("claude_code", "u9", "sess-0002", "user", "a second session", when="2026-10-14T11:00:00.000Z",
                     project_key="glowworks")]
    reader = arm(monkeypatch, node, reader=FakeReader(first, second))
    enable(node, SOURCE)
    result = _run()
    assert result["status"] == "ok" and result["records_processed"] == 3
    assert len(reader.calls) == 3, "two batches with files behind them, then the empty one that ends the run"
    assert result["files_touched"] == 2
