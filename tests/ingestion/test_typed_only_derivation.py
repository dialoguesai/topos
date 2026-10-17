"""Coding-session history: only what the owner typed feeds entity, topic and goal extraction (owner ruling, 16 Oct 2026).

"The AI's replies are still stored and searchable, so answers can use them, but they don't create entities or
goals." The three coding-session sources name ``entities``, ``topics`` and ``goal_extraction`` in
``authored_only_jobs``; ``enrichment/authored_only.py`` applies it where a job is handed its rows. Proven on every
path that hands rows to a job: the sync lane's post-canonical pipeline, the manual-process door and its catch-up
read, the per-job backfill, reprocess, and the derivation-debt retry. The ChatGPT shadow on the same ``ai_messages``
lane is unchanged; a ``share_excluded`` row still reaches no job at all, embeddings included.

The jobs are recording stand-ins (they remember the rows they were handed and derive nothing); the node is a SQLite
file under ``tmp_path``; every record is invented.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from tests.ingestion.ai_tool_fixtures import DATASET, FakeReader, arm, build_node, chat_rows, enable, record
from topos.enrichment.jobs.base import BaseEnrichmentJob

CODING = ("claude_code_sessions", "codex_sessions", "cursor_sessions")
TYPED_ONLY = {"entities", "topics", "goal_extraction"}
SOURCE = "claude_code_sessions"
SESSION = "sess-0002"
TYPED = {"claude_code:t1", "claude_code:t3"}
REPLIES = {"claude_code:t2"}
TAINTED = "claude_code:t4"
EVERY_ROW = TYPED | REPLIES
CANONICAL_JOB_NAMES = ("entities", "topics", "sentiment", "embeddings", "emo_27")
SIGNAL_JOB_NAMES = (
    "embeddings", "entities", "topics", "goal_extraction", "dimension_summary", "topic_clusters",
    "relationship_edges", "statistics", "facts", "timeline", "attention_triage", "derived_object_embeddings",
    "emo_27", "sentiment",
)


class RecordingJob(BaseEnrichmentJob):
    """Stands in for one job: remembers which rows it was handed, derives nothing."""

    def __init__(self, job_name: str, seen: Dict[str, Set[str]]) -> None:
        super().__init__()
        self._job_name = job_name
        self._seen = seen

    def get_job_name(self) -> str:
        return self._job_name

    def get_derived_table(self) -> str:
        return ""

    async def enrich(self, canonical_messages, progress_callback=None, **_: Any):
        self._seen.setdefault(self._job_name, set()).update(str(m.get("message_id")) for m in canonical_messages)
        return []


class _NoProfiles:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def upsert_all(self, **kwargs: Any) -> None:
        return None


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    # Synthetic source enrollment must not depend on the host's free space.
    monkeypatch.setattr("topos.engine.disk_space.free_bytes", lambda path=None: 1_000_000_000_000)
    conn = build_node(tmp_path / "typed-only.db")
    for target in ("topos.core.state.get_db_connection", "topos.api.enrichment.get_db_connection",
                   "topos.ingestion.reprocess.get_db_connection"):
        monkeypatch.setattr(target, lambda: conn)
    yield conn
    conn.close()


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Dict[str, Set[str]]]:
    """Both lanes' jobs replaced by recorders; the model-bound steps around them stubbed."""
    from topos.ingestion import canonical_pipeline
    from topos.storage.adapters.factory import AdapterFactory

    handed: Dict[str, Dict[str, Set[str]]] = {"canonical": {}, "signal": {}}
    monkeypatch.setattr("topos.enrichment.orchestrator.CANONICAL_JOBS",
                        [RecordingJob(name, handed["canonical"]) for name in CANONICAL_JOB_NAMES])
    monkeypatch.setattr("topos.enrichment.jobs.SIGNAL_JOB_REGISTRY",
                        {name: RecordingJob(name, handed["signal"]) for name in SIGNAL_JOB_NAMES})
    monkeypatch.setattr(AdapterFactory, "from_runtime", staticmethod(lambda *a, **k: object()))
    monkeypatch.setattr("topos.features.signal.dimension_profiles.DimensionProfileUpdater", _NoProfiles)

    async def fake_privacy(conn, records, *, source_group=None, progress_callback=None):
        return {"rows": len(records)}

    monkeypatch.setattr("topos.disclosure.privacy_layer.run_privacy_disclosure_layer", fake_privacy)
    monkeypatch.setattr("topos.features.entities.graph_refresh.refresh_now_if_dirty", lambda: {"skipped": "test"})
    monkeypatch.setattr("topos.features.entities.graph_refresh.mark_graph_dirty", lambda: None)
    monkeypatch.setattr("topos.features.entities.graph_refresh.mark_gc_due", lambda: None)
    monkeypatch.setattr(canonical_pipeline, "_record_episode", lambda **kw: None)
    return handed


def _batch():
    return [
        record("claude_code", "t1", SESSION, "user", "make the lantern glow amber", turn_index=0),
        record("claude_code", "t2", SESSION, "assistant", "I will add a glowLantern helper in lantern_glow.py",
               turn_index=1, when="2026-10-14T10:00:01.000Z", origin="assistant"),
        record("claude_code", "t3", SESSION, "user", "ship it before the fair", turn_index=2,
               when="2026-10-14T10:00:02.000Z"),
        record("claude_code", "t4", SESSION, "assistant", "your topos says the fair is on Saturday", turn_index=3,
               when="2026-10-14T10:00:03.000Z", share_excluded=True, origin="assistant"),
    ]


def _seed(conn: sqlite3.Connection) -> None:
    """The same four rows, stored as a finished sync left them (no raw rows: reprocess reloads these)."""
    conn.execute(
        "INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, updated_at)"
        " VALUES ('claude_code:sess-0002', 'owner-0000-1111', 'Lantern', ?, '2026-10-14T10:00:00Z',"
        " '2026-10-14T10:00:00Z')",
        (SOURCE,),
    )
    rows = [
        ("claude_code:t1", "human", "make the lantern glow amber", 0),
        ("claude_code:t2", "assistant", "I will add a glowLantern helper in lantern_glow.py", 0),
        ("claude_code:t3", "human", "ship it before the fair", 0),
        (TAINTED, "assistant", "your topos says the fair is on Saturday", 1),
    ]
    for n, (message_id, sender, text, excluded) in enumerate(rows):
        conn.execute(
            "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id,"
            " share_excluded, writer_class, content_disclosure) VALUES (?, 'claude_code:sess-0002', ?, ?, ?, ?, ?,"
            " 'owner_app', ?)",
            (message_id, sender, f"2026-10-14T10:00:0{n}Z", text, SOURCE, excluded, text),
        )
    conn.commit()


def _assert_typed_only(handed: Dict[str, Dict[str, Set[str]]], lane: str, jobs=TYPED_ONLY) -> None:
    for name in jobs:
        assert handed[lane].get(name) == TYPED, (lane, name, handed[lane].get(name))
    assert handed[lane].get("embeddings") == EVERY_ROW, (lane, handed[lane].get("embeddings"))


def _assert_tainted_reached_nothing(handed: Dict[str, Dict[str, Set[str]]]) -> None:
    for lane, jobs in handed.items():
        for name, ids in jobs.items():
            assert TAINTED not in ids, f"{lane}/{name} was handed the share_excluded row"


# --- the definitions -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source_id", CODING)
def test_each_coding_source_keeps_entities_topics_and_goals_to_typed_rows(source_id: str) -> None:
    from topos.sources.registry import BUNDLED_REGISTRY

    definition = BUNDLED_REGISTRY[source_id]
    assert set(definition.authored_only_jobs) == TYPED_ONLY
    # Embeddings run on every row: that is what keeps the replies searchable.
    assert "embeddings" in definition.canonical_enrichment_jobs
    assert "embeddings" not in definition.authored_only_jobs
    assert definition.to_dict()["authored_only_jobs"] == list(definition.authored_only_jobs)


def test_no_other_bundled_source_names_any_and_its_serialized_definition_is_unchanged() -> None:
    from topos.sources.registry import BUNDLED_REGISTRY

    for source_id, definition in BUNDLED_REGISTRY.items():
        if source_id in CODING:
            continue
        assert definition.authored_only_jobs == [], source_id
        assert "authored_only_jobs" not in definition.to_dict(), source_id


def test_an_install_row_taken_before_the_rule_or_edited_to_drop_it_still_gets_it() -> None:
    from topos.sources.registry import BUNDLED_REGISTRY
    from topos.sources.runtime_install import _build_source_definition

    payload = json.loads(json.dumps(BUNDLED_REGISTRY[SOURCE].to_dict()))
    payload.pop("authored_only_jobs")
    assert set(_build_source_definition(payload).authored_only_jobs) == TYPED_ONLY
    payload["authored_only_jobs"] = []
    assert set(_build_source_definition(payload).authored_only_jobs) == TYPED_ONLY


def test_the_install_door_takes_the_bundled_definition_and_refuses_a_copy_without_the_rule() -> None:
    from topos.owner_door_routes import transcript_install_body
    from topos.sources.registry import BUNDLED_REGISTRY

    bundled = json.loads(json.dumps(BUNDLED_REGISTRY[SOURCE].to_dict()))
    assert transcript_install_body({"source_definition_json": bundled})
    edited = dict(bundled)
    edited.pop("authored_only_jobs")
    assert not transcript_install_body({"source_definition_json": edited})


def test_a_definition_refuses_anything_but_a_list_of_job_names() -> None:
    from topos.sources.definitions import DataSourceDefinition

    base = dict(source_id="lantern_notes", display_name="Lantern notes", source_type="file",
                schema_id="lantern.v1", parser_id="lantern.v1")
    assert DataSourceDefinition(**base).authored_only_jobs == []
    for bad in ([""], [7], None, "entities"):
        with pytest.raises(ValueError):
            DataSourceDefinition(**base, authored_only_jobs=bad)


def test_rows_for_job_reads_the_role_off_the_row() -> None:
    from topos.enrichment.authored_only import rows_for_job

    typed = {"message_id": "claude_code:t1", "sender_type": "human", "source_id": SOURCE, "writer_class": "owner_app"}
    reply = {"message_id": "claude_code:t2", "sender_type": "assistant", "source_id": SOURCE, "writer_class": "owner_app"}
    # A row no owner door wrote is not the owner's typing, whatever its sender says.
    relayed = {"message_id": "claude_code:t5", "sender_type": "human", "source_id": SOURCE, "writer_class": "third_party"}
    shadow = {"message_id": "chatgpt:a1", "sender_type": "assistant", "source_id": "chatgpt_ui_conversation",
              "writer_class": "owner_app", "_table": "ai_chat_messages"}
    batch = [typed, reply, relayed, shadow]

    def ids(rows):
        return [r["message_id"] for r in rows]

    # No _table on the coding rows (a reload carries none): the source's canonical table stands in.
    assert ids(rows_for_job("entities", batch)) == ["claude_code:t1", "chatgpt:a1"]
    assert ids(rows_for_job("goal_extraction", batch)) == ["claude_code:t1", "chatgpt:a1"]
    assert ids(rows_for_job("embeddings", batch)) == ids(batch)
    assert ids(rows_for_job("statistics", batch)) == ids(batch)
    # A record without its own source id takes the batch's.
    bare = [{k: v for k, v in row.items() if k != "source_id"} for row in (typed, reply)]
    assert ids(rows_for_job("topics", bare, source_id=SOURCE)) == ["claude_code:t1"]
    assert ids(rows_for_job("topics", bare)) == ["claude_code:t1", "claude_code:t2"]


# --- path 1: the sync lane's own post-canonical pipeline ----------------------------------------------------------


def test_the_sync_hands_entities_topics_and_goals_only_the_typed_rows(node, seen, monkeypatch) -> None:
    from topos.ingestion.ai_tool_sync import run_claude_code_sync
    from topos.ingestion.ingest_helpers import run_inbox_deferred_enrichment

    arm(monkeypatch, node, reader=FakeReader(_batch()), post_canonical=None)
    enable(node, SOURCE)
    result = run_claude_code_sync(DATASET)
    assert result["status"] == "ok", result

    # Collection queues processing durably. Exercise the worker before checking
    # which records its real pipeline hands to each recording job.
    queued = node.execute(
        "SELECT payload_json FROM pipeline_jobs WHERE kind='inbox_deferred_enrichment' AND source_id=?",
        (SOURCE,),
    ).fetchall()
    assert queued and seen == {"canonical": {}, "signal": {}}
    for (payload,) in queued:
        asyncio.run(run_inbox_deferred_enrichment(json.loads(payload)))

    _assert_typed_only(seen, "canonical", jobs={"entities", "topics"})
    _assert_typed_only(seen, "signal")
    # Every other job still reads the replies.
    for name in ("statistics", "timeline", "facts", "dimension_summary", "relationship_edges"):
        assert seen["signal"].get(name) == EVERY_ROW, name
    _assert_tainted_reached_nothing(seen)
    # The replies are stored as before.
    stored = {row["message_id"]: row for row in chat_rows(node)}
    assert EVERY_ROW <= set(stored)
    assert stored["claude_code:t2"]["share_excluded"] == 0
    # The batch is done for entities and topics, replies included, so no catch-up reads them again.
    for job in ("entities", "topics"):
        marked = {row[0] for row in node.execute(
            "SELECT record_id FROM enrichment_record_progress WHERE source_id = ? AND job_id = ?", (SOURCE, job))}
        assert EVERY_ROW <= marked, (job, marked)


def test_a_batch_of_replies_only_is_still_marked_done_for_the_typed_only_jobs(node, seen) -> None:
    from topos.enrichment.orchestrator import EnrichmentOrchestrator

    replies = [{"message_id": "claude_code:t2", "sender_type": "assistant", "content": "a helper", "source_id": SOURCE,
                "writer_class": "owner_app", "_table": "ai_chat_messages"}]
    asyncio.run(EnrichmentOrchestrator(tables_manager=object()).run_canonical(replies, job_names=["entities", "embeddings"]))
    assert "entities" not in seen["canonical"]
    assert seen["canonical"]["embeddings"] == REPLIES
    marked = {row[0] for row in node.execute(
        "SELECT record_id FROM enrichment_record_progress WHERE source_id = ? AND job_id = 'entities'", (SOURCE,))}
    assert marked == REPLIES


# --- path 2: the ChatGPT shadow, unchanged -------------------------------------------------------------------------


def test_the_chatgpt_shadow_still_hands_every_job_every_row(node, seen) -> None:
    from topos.ingestion.canonical_pipeline import run_post_canonical_pipeline
    from topos.sources.registry import REGISTRY

    common = {"conversation_id": "chatgpt:c1", "event_at": "2026-10-14T10:00:00Z",
              "source_id": "chatgpt_ui_conversation", "writer_class": "owner_app", "_table": "ai_chat_messages"}
    records = [
        {**common, "message_id": "chatgpt:h1", "sender_type": "human", "content": "plan the amber lantern"},
        {**common, "message_id": "chatgpt:a1", "sender_type": "assistant", "content": "start with the wick"},
    ]
    asyncio.run(run_post_canonical_pipeline(source_def=REGISTRY["chatgpt_ui_conversation"],
                                            canonical_records=records, sync_batch_id="shadow-1"))
    both = {"chatgpt:h1", "chatgpt:a1"}
    for name in ("entities", "topics", "embeddings", "sentiment", "emo_27"):
        assert seen["canonical"].get(name) == both, name
    for name in ("entities", "topics", "goal_extraction", "embeddings", "statistics"):
        assert seen["signal"].get(name) == both, name


# --- path 3: the manual-process door and its catch-up read ----------------------------------------------------------


def test_manual_processing_hands_entities_topics_and_goals_only_the_typed_rows(node, seen) -> None:
    from topos.api.enrichment import _process_enrichment_core

    _seed(node)
    result = asyncio.run(_process_enrichment_core(SOURCE, force_reprocess=True))
    assert result["status"] == "ok", result
    _assert_typed_only(seen, "canonical", jobs={"entities", "topics"})
    _assert_typed_only(seen, "signal")
    _assert_tainted_reached_nothing(seen)


def test_the_catch_up_read_counts_a_reply_done_for_the_typed_only_jobs(node, monkeypatch) -> None:
    from topos.api import enrichment as door

    _seed(node)
    output = {"message_entities": set(TYPED), "message_topics": set(TYPED), "message_embeddings": set(EVERY_ROW)}
    monkeypatch.setattr(door, "_get_enriched_message_ids", lambda table, conn, **kw: set(output.get(table, set())))
    assert asyncio.run(door._find_unprocessed_messages(SOURCE)) == []
    # Control: a typed row without its entities is still owed; the reply never is.
    output["message_entities"] = {"claude_code:t1"}
    owed = asyncio.run(door._find_unprocessed_messages(SOURCE))
    assert [row["message_id"] for row in owed] == ["claude_code:t3"]
    # And a reply without its embedding is owed that.
    output["message_entities"] = set(TYPED)
    output["message_embeddings"] = set(TYPED)
    owed = asyncio.run(door._find_unprocessed_messages(SOURCE))
    assert [row["message_id"] for row in owed] == ["claude_code:t2"]


# --- path 4: the per-job backfill ------------------------------------------------------------------------------------


@pytest.mark.parametrize("job_name, expected", [
    ("entities", TYPED), ("topics", TYPED), ("goal_extraction", TYPED), ("embeddings", EVERY_ROW),
])
def test_the_backfill_of_one_job_honours_the_rule(node, seen, job_name, expected) -> None:
    from topos.api.enrichment import _generic_backfill_core
    from topos.sources.registry import REGISTRY

    _seed(node)
    result = asyncio.run(_generic_backfill_core(source_def=REGISTRY[SOURCE], job_name=job_name))
    handed = seen["signal"].get(job_name, set()) | seen["canonical"].get(job_name, set())
    assert handed == expected, (job_name, handed)
    _assert_tainted_reached_nothing(seen)
    # Every row it loaded is marked run, so the next backfill does not load the replies again.
    assert result["rows_marked"] == len(EVERY_ROW), result


# --- path 5: reprocess --------------------------------------------------------------------------------------------


def test_reprocess_hands_entities_topics_and_goals_only_the_typed_rows(node, seen) -> None:
    from topos.ingestion.reprocess import reprocess_source

    _seed(node)
    result = asyncio.run(reprocess_source(source_id=SOURCE, dataset_id=None, from_stage="canonical"))
    assert result["status"] == "accepted", result
    _assert_typed_only(seen, "canonical", jobs={"entities", "topics"})
    _assert_typed_only(seen, "signal")
    _assert_tainted_reached_nothing(seen)


# --- path 6: the derivation-debt retry -----------------------------------------------------------------------------


@pytest.mark.parametrize("job_name, expected", [("entities", TYPED), ("goal_extraction", TYPED), ("embeddings", EVERY_ROW)])
def test_the_debt_retry_honours_the_rule_on_rows_it_reloads(node, seen, job_name, expected) -> None:
    """The retry reloads rows with no table stamp: the typed rows must still read as typed, the reply not."""
    from topos.enrichment.derivation_recovery import retry_single_derivation

    _seed(node)
    outcome = asyncio.run(retry_single_derivation(node, source_id=SOURCE, sync_batch_id="batch-typed-1",
                                                  job_name=job_name))
    assert outcome["outcome"] == "recovered", outcome
    assert seen["signal"].get(job_name) == expected, (job_name, seen["signal"].get(job_name))
    _assert_tainted_reached_nothing(seen)
