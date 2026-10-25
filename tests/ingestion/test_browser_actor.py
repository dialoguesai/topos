"""Browser actor provenance through the real ingest door, flat/canonical storage and replay."""
import json

import pytest

from test_activity_writer_class import (  # noqa: F401
    conn, captured_jobs, _owner_capture, _rows, _writer, _visit, PLUGIN, DATASET,
)
from topos.features.provenance.browser_actor import browser_actor, conversation_activity, personal_browser_activity
from topos.features.provenance.roles import is_engaged

AGENT = {"version": 1, "kind": "agent", "method": "manual", "name": "Codex", "provider": "openai",
         "conversation_source": "codex_sessions", "conversation_id": "codex:synthetic-parent", "session_id": "session-1"}


@pytest.mark.asyncio
async def test_agent_visit_keeps_actor_writer_and_parent_on_every_hop(conn, captured_jobs):
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY
    record = _visit(capture_id="stable-capture", actor=AGENT)
    for i in range(2):
        assert (await _owner_capture(f"req-{i}", record))["status"] == "ok"
    (row,) = _rows(conn)
    assert row["event_id"] == "browser:stable-capture"
    assert _writer(row) == ("owner_app", PLUGIN, DATASET)
    assert browser_actor(row)["conversation_id"] == AGENT["conversation_id"]
    assert not personal_browser_activity(row)
    (flat,) = _rows(conn, "browser_visits")
    assert flat["actor_kind"] == "agent"
    assert flat["parent_conversation_id"] == AGENT["conversation_id"]
    (reloaded,) = load_canonical_records_for_signal(conn, REGISTRY["browser_visits"])
    assert browser_actor(reloaded)["provider"] == "openai"
    assert len(conversation_activity(conn, source_id="codex_sessions", conversation_id=AGENT["conversation_id"])) == 1
    assert conversation_activity(conn, source_id="another-source", conversation_id=AGENT["conversation_id"]) == []


@pytest.mark.parametrize("kind,method,expected", [("human", "manual", True), ("human", "unresolved", False),
    ("agent", "manual", False), ("automation", "webdriver", False), ("unknown", "unresolved", False)])
def test_highlighting_is_personal_only_with_explicit_human_actor(kind, method, expected):
    row = {"activity_type": "browser_highlight", "metadata_json": {"highlight": "A page author's words", "browser_actor": {**AGENT, "kind": kind, "method": method}}}
    assert is_engaged(row) is expected
    assert personal_browser_activity(row) is expected
    assert is_engaged({"activity_type": "browser_highlight"})  # no attribution evidence: the owner's (ruling 24 Oct)


def test_invalid_and_incomplete_attribution_never_becomes_human():
    assert browser_actor({"actor": {"kind": [], "method": {}}})["kind"] == "unknown"
    assert browser_actor({"actor": {"kind": "agent", "method": "manual", "name": "bot"}})["kind"] == "automation"
    assert browser_actor({"actor": {"kind": "human", "method": "manual", "signals": {"webdriver": True}}})["kind"] == "automation"


@pytest.mark.parametrize("event_type", ["click", "highlight", "VIDEO_PLAY", "star_page"])
def test_event_actor_survives_parser_mapper_and_flat_write(conn, event_type):
    from topos.ingestion.parsers.browser_parser import BrowserEventsParser
    from topos.ingestion.sources.base import RawRecord
    from topos.canonicalization.mappers.browser_activity_mapper import BrowserActivityCanonicalMapper
    from topos.storage.raw.browser_flat_tables import write_browser_event
    normalized = BrowserEventsParser(DATASET).parse(RawRecord(record_id="r", payload={
        **_visit(actor=AGENT, capture_id=event_type), "event_type": event_type, "content": {"selectedText": "Some text"}}))
    mapped = BrowserActivityCanonicalMapper().map(normalized).payload
    assert browser_actor(mapped)["session_id"] == "session-1"
    write_browser_event(conn, normalized.payload)
    row = dict(conn.execute("SELECT * FROM browser_events WHERE record_id=?", (event_type,)).fetchone())
    assert browser_actor(row)["conversation_source"] == "codex_sessions"


@pytest.mark.asyncio
async def test_internal_replay_cannot_erase_stored_actor(conn, captured_jobs):
    from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
    from topos.sources.registry import REGISTRY
    assert (await _owner_capture("original", _visit(capture_id="replay", actor=AGENT)))["status"] == "ok"
    out = canonicalize_normalized_batch(conn, REGISTRY["browser_visits"], [{**_visit(), "record_id": "replay"}], dataset_id=DATASET, sync_batch_id="replay")
    assert browser_actor(_rows(conn)[0])["kind"] == "agent"
    assert browser_actor(out.canonical_records[0])["kind"] == "agent"


@pytest.mark.asyncio
async def test_owner_recall_and_vector_hits_retain_actor_without_promoting_interests(conn, captured_jobs):
    from topos.query.retrieval import _annotate_index_message_roles
    from types import SimpleNamespace
    assert (await _owner_capture("original", _visit(capture_id="recall", actor=AGENT)))["status"] == "ok"
    items = [{"record_id": "browser:recall", "retrieval_source": "vector", "similarity": 1}]
    out = _annotate_index_message_roles(items, conn, SimpleNamespace(canonical_tables=[]), disclosure_tier="owner_raw")
    assert out[0]["owner_authored"] is False
    assert out[0]["personal_interest_eligible"] is False
    assert out[0]["browser_actor"]["conversation_id"] == AGENT["conversation_id"]


@pytest.mark.asyncio
async def test_timeline_filter_counts_and_paginates_actors_before_returning_page(conn, captured_jobs):
    from topos.features.facts.reads import list_timeline
    from topos.features.timeline_projection import project_timeline_rows
    actors = [AGENT, {"kind": "human", "method": "manual"},
              {"kind": "automation", "method": "webdriver"}, None]
    for i, actor in enumerate(actors):
        assert (await _owner_capture(f"timeline-{i}", _visit(capture_id=f"timeline-{i}", actor=actor)))["status"] == "ok"
    project_timeline_rows(conn, [{**r, "_table": "activity_events"} for r in _rows(conn)])
    first = list_timeline(conn, actor_kind="ai", limit=1)
    second = list_timeline(conn, actor_kind="ai", limit=1, offset=1)
    assert first["actor_filter"] == second["actor_filter"] == "ai"
    assert first["total"] == second["total"] == 2
    assert first["items"][0]["record_id"] != second["items"][0]["record_id"]
    assert {first["items"][0]["browser_actor"]["kind"], second["items"][0]["browser_actor"]["kind"]} == {"agent", "automation"}
    assert list_timeline(conn, actor_kind="human")["total"] == 1
    assert list_timeline(conn, actor_kind="unknown")["total"] == 1
    assert list_timeline(conn)["total"] == 4
    with pytest.raises(ValueError):
        list_timeline(conn, actor_kind="anything")


def test_every_personal_enrichment_job_excludes_ai_but_recall_and_timeline_keep_it():
    from topos.enrichment.authored_only import rows_for_job
    agent = {"source_id": "browser_visits", "actor": AGENT}
    human = {"source_id": "browser_visits", "actor": {"kind": "human", "method": "manual"}}
    legacy = {"source_id": "browser_visits"}
    unrelated = {"source_id": "github", "activity_type": "star", "actor": "a-github-user"}
    records = [agent, human, legacy, unrelated]
    for job in ("entities", "topics", "facts", "goal_extraction", "stats", "sentiment"):
        # Owner ruling 24 Oct 2026: browsing with no attribution evidence (here, from before attribution) is the
        # owner's; only positive agent/automation evidence keeps a row out of personal enrichment.
        assert rows_for_job(job, records) == [human, legacy, unrelated]
    for job in ("embeddings", "timeline", "pii_redaction"):
        assert rows_for_job(job, records) == records


@pytest.mark.asyncio
async def test_a_duplicate_without_actor_cannot_erase_capture_provenance(conn, captured_jobs):
    assert (await _owner_capture("first", _visit(capture_id="same", actor=AGENT)))["status"] == "ok"
    assert (await _owner_capture("retry", _visit(capture_id="same")))["status"] == "ok"
    assert browser_actor(_rows(conn)[0])["kind"] == "agent"
    assert browser_actor(_rows(conn, "browser_visits")[0])["kind"] == "agent"
