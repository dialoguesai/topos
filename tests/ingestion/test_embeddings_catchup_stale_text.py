"""Rows whose text changed after they were embedded are re-embedded once, in pages, on the catch-up lane.

A stored chunk's ``content_hash`` is the embeddings job's sha256 of the record's embeddable text. Since lane 194 a
chunk whose hash matches none of the row's current renderings is refused below the owner's tier and re-embedded by
every share index rebuild. This lane walks each source's embedded canonical records, re-embeds the stale ones
through the ordinary embeddings job, prunes the leftover chunks, and leaves every current row alone. Every line
here is invented.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from topos.ingestion import stale_text_catchup as lane
from topos.ingestion.stale_text_catchup import KIND, SWITCH, audit, enqueue_catchup, execute_catchup, scan_page
from topos.pipeline.job_store import claim_next_job, enqueue_job, get_job
from topos.query.retrieval import _index_current_texts
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations

EXPORT = "chatgpt_file_ingestion"
VISITS = "browser_visits"
MODEL = "test-model"
OLD = hashlib.sha256(b"a line this row no longer holds").hexdigest()


def _hashes(row: dict) -> list[str]:
    return [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in _index_current_texts(row)]


def _chat(c, rid, text, *, excluded=0):
    c.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id, "
        "share_excluded) VALUES (?, 'chat-1', 'human', '2026-09-01T12:00:00Z', ?, ?, ?)", (rid, text, EXPORT, excluded))
    return {"message_id": rid, "content": text}


def _visit(c, rid, title):
    c.execute(
        "INSERT INTO activity_events (event_id, activity_type, url, title, occurred_at, source_id) "
        "VALUES (?, 'browser_visit', ?, ?, '2026-09-02T09:00:00Z', ?)", (rid, f"https://example.test/{rid}", title, VISITS))
    return {"event_id": rid, "activity_type": "browser_visit", "url": f"https://example.test/{rid}", "title": title}


def _vector(c, rid, source, record_type, content_hash, chunk=0):
    c.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, model, dims, vector_blob, chunk_index, "
        "content_hash, record_type, text_preview) VALUES (?, ?, ?, ?, 3, X'00', ?, ?, ?, 'synthetic stored text')",
        (f"emb-{rid}-{chunk}", rid, source, MODEL, chunk, content_hash, record_type))


def vectors(c, rid):
    return {r[0]: r[1] for r in c.execute(
        "SELECT chunk_index, content_hash FROM signal_embeddings WHERE record_id=? ORDER BY chunk_index", (rid,))}


def stored(c, rid):
    return [tuple(r) for r in c.execute(
        "SELECT embedding_id, created_at, content_hash FROM signal_embeddings WHERE record_id=?", (rid,))]


def embedded_ids(c):
    return {r[0] for r in c.execute("SELECT DISTINCT record_id FROM signal_embeddings WHERE vector_blob IS NOT NULL")}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """The synthetic store; one connection per thread, as the node has (the job's writes and the worker's progress
    updates run on different threads), the main thread's handed to the test."""
    path = str(tmp_path / "stale.db")
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    _vector(c, "edited", EXPORT, "ai_chat_message", OLD)                       # text rewritten since
    _chat(c, "edited", "synthetic current words of an edited line")
    same = _chat(c, "same", "synthetic unchanged line")                         # current
    _vector(c, "same", EXPORT, "ai_chat_message", _hashes(same)[0])
    _chat(c, "blank", "")                                                       # nothing to embed
    _vector(c, "blank", EXPORT, "ai_chat_message", OLD)
    _chat(c, "kept", "synthetic line the owner keeps from every share", excluded=1)
    _vector(c, "kept", EXPORT, "ai_chat_message", OLD)
    left = _chat(c, "leftover", "synthetic line whose second chunk outlived it")
    _vector(c, "leftover", EXPORT, "ai_chat_message", _hashes(left)[0], chunk=0)
    _vector(c, "leftover", EXPORT, "ai_chat_message", OLD, chunk=1)
    bare = _visit(c, "visit-bare", "Synthetic page one")                        # hash of the bare row
    _vector(c, "visit-bare", VISITS, "activity_event", _hashes(bare)[0])
    prepared = _visit(c, "visit-prepared", "Synthetic page two")                # hash of the prepared copy
    _vector(c, "visit-prepared", VISITS, "activity_event", _hashes(prepared)[1])
    _visit(c, "visit-edited", "Synthetic page three, retitled")
    _vector(c, "visit-edited", VISITS, "activity_event", OLD)
    _vector(c, "derived-1", "topos_derivation", "derived_goal", OLD)          # not a canonical row: never read
    _chat(c, "unsealed", "synthetic line whose chunk was stored without a hash")
    _vector(c, "unsealed", EXPORT, "ai_chat_message", None)                     # no seal: refused like a stale one
    c.commit()
    local = threading.local()
    local.conn = c

    def connection() -> sqlite3.Connection:
        own = getattr(local, "conn", None)
        if own is None:
            own = local.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        return own

    monkeypatch.setattr("topos.core.state.get_db_connection", connection)
    monkeypatch.delenv(SWITCH, raising=False)
    yield c
    c.close()


def factory() -> sqlite3.Connection:
    """The worker's connection factory: the fixture's thread-local one."""
    from topos.core.state import get_db_connection

    return get_db_connection()


@pytest.fixture
def model_calls(monkeypatch):
    calls = []

    async def run(_engine, *, input_payload, record_ids, **_kw):
        calls.append(list(record_ids))
        return SimpleNamespace(status="completed",
                               output={"vectors": [[0.1, 0.2, 0.3] for _ in input_payload["texts"]], "dims": 3,
                                       "model": MODEL})

    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", run)
    return calls


def payload(conn, job_id):
    return json.loads(conn.execute("SELECT payload_json FROM pipeline_jobs WHERE job_id=?", (job_id,)).fetchone()[0])


def detail(conn, job_id):
    return json.loads(conn.execute("SELECT detail_json FROM pipeline_jobs WHERE job_id=?", (job_id,)).fetchone()[0])


async def run_pass(conn, source_id=None):
    """Queue and run pages until nothing is queued; each page finishes through the production worker's completion
    (``process_job``), which queues the next. Returns the finished jobs' details in order."""
    from topos.pipeline.job_runner import process_job

    details, ended = [], set()
    enqueue_catchup(conn, source_id)
    while True:
        claimed = claim_next_job(conn, lease_owner="worker-c", kinds=[KIND])
        if claimed is None:
            return details
        body = payload(conn, claimed["job_id"])
        if body["source_id"] in ended:
            # A page of the pass after this one (the test's clock makes it due at once): not this run's.
            conn.execute("DELETE FROM pipeline_jobs WHERE job_id=?", (claimed["job_id"],))
            conn.commit()
            continue
        await process_job(factory, claimed)
        done = detail(conn, claimed["job_id"])
        if done.get("end"):
            ended.add(body["source_id"])
        details.append((body, get_job(conn, claimed["job_id"])["status"], done))


@pytest.mark.asyncio
async def test_a_stale_row_is_re_embedded_exactly_once_and_a_current_row_is_not_touched(conn, model_calls, monkeypatch):
    before = stored(conn, "same")
    details = await run_pass(conn)
    assert all(status == "done" for _, status, _ in details), details
    assert sorted(rid for call in model_calls for rid in call) == ["edited", "unsealed", "visit-edited"]
    for rid in ("edited", "unsealed"):
        row = dict(zip(["message_id", "content"], conn.execute(
            "SELECT message_id, content FROM ai_chat_messages WHERE message_id=?", (rid,)).fetchone()))
        assert list(vectors(conn, rid).values()) == [_hashes(row)[0]]
    assert stored(conn, "same") == before
    # The next pass, a day later, finds the rows current and calls no model.
    tomorrow(monkeypatch)
    details = await run_pass(conn)
    assert details and all(d["reembedded"] == 0 and d["stale"] == 0 for _, _, d in details), details
    assert sorted(rid for call in model_calls for rid in call) == ["edited", "unsealed", "visit-edited"]


@pytest.mark.asyncio
async def test_a_row_with_no_renderable_text_is_skipped(conn, model_calls):
    details = await run_pass(conn, EXPORT)
    assert vectors(conn, "blank") == {0: OLD}
    assert sum(d["no_text"] for _, _, d in details) == 1
    assert "blank" not in {rid for call in model_calls for rid in call}


@pytest.mark.asyncio
async def test_a_row_the_owner_keeps_from_every_share_is_not_read(conn, model_calls):
    details = await run_pass(conn, EXPORT)
    assert vectors(conn, "kept") == {0: OLD}
    assert sum(d["share_excluded"] for _, _, d in details) == 1
    assert "kept" not in {rid for call in model_calls for rid in call}


@pytest.mark.asyncio
async def test_either_rendering_of_a_row_without_content_is_current(conn, model_calls):
    await run_pass(conn, VISITS)
    assert vectors(conn, "visit-bare") and vectors(conn, "visit-prepared")
    assert sorted(rid for call in model_calls for rid in call) == ["visit-edited"]


@pytest.mark.asyncio
async def test_a_leftover_chunk_beside_a_current_one_is_pruned_without_the_model(conn, model_calls):
    details = await run_pass(conn, EXPORT)
    assert list(vectors(conn, "leftover")) == [0]
    assert "leftover" not in {rid for call in model_calls for rid in call}
    assert sum(d["pruned_rows"] for _, _, d in details) >= 1


@pytest.mark.asyncio
async def test_the_owner_tier_is_unaffected(conn, model_calls):
    """No record loses its vector, the re-embedded ones keep theirs under a current hash, and the gate below which
    lane 194 drops stale hits still passes every hit at the owner's tier (the lane changes no retrieval code)."""
    from topos.query.manifest import ScopeResolutionManifest
    from topos.query.retrieval import _index_hits_inside_grant

    before = embedded_ids(conn)
    await run_pass(conn)
    assert embedded_ids(conn) == before
    hits = [{"record_id": rid, "record_type": "ai_chat_message", "content_hash": h}
            for rid in ("edited", "same") for h in vectors(conn, rid).values()]
    manifest = ScopeResolutionManifest(scope_id="ai_conversations:read", primary_dimensions=[],
                                       canonical_tables=["ai_chat_messages"])
    assert _index_hits_inside_grant(hits, conn=conn, manifest=manifest, disclosure_tier="owner_raw") == hits
    # And below the owner's tier the re-embedded row is back.
    kept = _index_hits_inside_grant(hits, conn=conn, manifest=manifest, disclosure_tier="default_disclosure")
    assert sorted(h["record_id"] for h in kept) == ["edited", "same"]


@pytest.mark.asyncio
async def test_a_record_the_job_writes_nothing_for_keeps_no_stale_chunk(conn, model_calls):
    """A record the owner excluded from derivation is skipped by the job; its stale chunks go, and the next pass does
    not find it again."""
    conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                 "VALUES ('x1', 'record', 'edited')")
    conn.commit()
    await run_pass(conn, EXPORT)
    assert vectors(conn, "edited") == {}
    assert "edited" not in {rid for call in model_calls for rid in call}
    assert audit(conn, EXPORT)[EXPORT]["stale"] == 0


@pytest.mark.asyncio
async def test_pages_carry_the_cursor_and_a_pass_ends_at_the_last_record(conn, model_calls, monkeypatch):
    monkeypatch.setattr(lane, "PAGE_SIZE", 2)
    details = await run_pass(conn, EXPORT)
    pages = [(p["after"], d["last_id"], d["end"]) for p, _, d in details]
    assert pages == [("", "edited", False), ("edited", "leftover", False), ("leftover", "unsealed", False),
                     ("unsealed", "unsealed", True)]
    assert len({p["pass_id"] for p, _, _ in details}) == 1
    assert enqueue_catchup(conn, EXPORT) == []                  # the pass ended; the next is not due
    tomorrow(monkeypatch)
    queued = enqueue_catchup(conn, EXPORT)
    assert len(queued) == 1 and payload(conn, queued[0])["after"] == ""


@pytest.mark.asyncio
async def test_a_finished_ingest_resumes_a_pass_a_stopped_node_left(conn, model_calls, monkeypatch):
    from topos.pipeline.job_runner import process_job

    monkeypatch.setattr(lane, "PAGE_SIZE", 2)
    first = enqueue_catchup(conn, EXPORT)[0]
    claimed = claim_next_job(conn, lease_owner="worker-c", kinds=[KIND])
    await process_job(factory, claimed)
    # The page it queued is lost with the node (deleted here); an import of the source finishes later.
    conn.execute("DELETE FROM pipeline_jobs WHERE kind=? AND status='queued'", (KIND,))
    conn.commit()
    enqueue_job(conn, kind="file_ingestion", source_id=EXPORT, payload={}, job_id="import-1")
    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": _ok}):
        await process_job(factory, claim_next_job(conn, lease_owner="w", kinds=["file_ingestion"]))
    queued = conn.execute("SELECT payload_json FROM pipeline_jobs WHERE kind=? AND status='queued'", (KIND,)).fetchall()
    assert len(queued) == 1 and json.loads(queued[0][0])["after"] == detail(conn, first)["last_id"]


async def _ok(_payload):
    return {"status": "ok"}


def tomorrow(monkeypatch):
    """The lane's clock, a day and a minute on: the next pass is due."""
    from datetime import timedelta

    real_now = lane._now.__wrapped__ if hasattr(lane._now, "__wrapped__") else lane._now
    monkeypatch.setattr(lane, "_now", lambda: real_now() + timedelta(seconds=lane.PASS_INTERVAL_SECONDS + 60))


@pytest.mark.asyncio
async def test_a_failed_page_prunes_nothing_and_is_queued_again_as_it_was(conn, monkeypatch):
    from topos.pipeline.job_runner import process_job

    async def failed(_engine, **_kw):
        return SimpleNamespace(status="failed", output={})

    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", failed)
    first = enqueue_catchup(conn, EXPORT)[0]
    await process_job(factory, claim_next_job(conn, lease_owner="worker-c", kinds=[KIND]))
    assert get_job(conn, first)["status"] == "failed"
    assert vectors(conn, "edited") == {0: OLD} and vectors(conn, "leftover") == {0: vectors(conn, "leftover")[0], 1: OLD}
    assert enqueue_catchup(conn, EXPORT) == [first]           # the same page, queued again
    assert get_job(conn, first)["status"] == "queued"


def test_an_ingest_in_flight_goes_first(conn):
    enqueue_job(conn, kind="inbox_deferred_enrichment", source_id=EXPORT, payload={})
    queued = enqueue_catchup(conn)
    assert {payload(conn, j)["source_id"] for j in queued} == {VISITS}
    assert enqueue_catchup(conn) == []                        # one page in flight per source


@pytest.mark.asyncio
async def test_the_switch_and_a_detached_source_stop_discovery_and_a_queued_page(conn, model_calls, monkeypatch):
    from topos.enrichment.source_overrides import set_source_enrichment_override

    body = payload(conn, enqueue_catchup(conn, EXPORT)[0])
    monkeypatch.setenv(SWITCH, "off")
    assert enqueue_catchup(conn, VISITS) == []
    assert (await execute_catchup(body))["skipped"] == f"{SWITCH} is off"
    monkeypatch.delenv(SWITCH)
    set_source_enrichment_override(EXPORT, "embeddings", False, conn=conn)
    assert "skipped" in await execute_catchup(body)
    assert model_calls == []
    assert vectors(conn, "edited") == {0: OLD}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"source_id": "", "after": "", "page": 10},
                                 {"source_id": EXPORT, "after": None, "page": 10},
                                 {"source_id": EXPORT, "after": "", "page": 0},
                                 {"source_id": EXPORT, "after": "", "page": lane.MAX_PAGE_SIZE + 1}])
async def test_refuses_a_payload_it_did_not_write(conn, model_calls, bad):
    assert (await execute_catchup(bad))["status"] == "error"
    assert model_calls == []


def test_a_store_without_the_columns_or_without_embedded_rows_queues_nothing(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    apply_all_migrations(bare)
    assert enqueue_catchup(bare) == [] and audit(bare) == {}  # migrated, nothing embedded
    bare.close()
    old = sqlite3.connect(str(tmp_path / "old.db"))
    apply_all_migrations(old)
    old.execute("DROP TABLE signal_embeddings")
    old.execute("CREATE TABLE signal_embeddings (embedding_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT, "
                "vector_blob BLOB)")
    old.execute("INSERT INTO signal_embeddings VALUES ('e', 'r', ?, X'00')", (EXPORT,))
    old.commit()
    assert enqueue_catchup(old) == [] and audit(old) == {}
    old.close()


def test_scan_sorts_the_page_and_names_no_text(conn):
    page = scan_page(conn, EXPORT, "", 100)
    assert {k: page[k] for k in ("scanned", "stale", "unreadable", "share_excluded", "no_text", "end")} == {
        "scanned": 6, "stale": 3, "unreadable": 0, "share_excluded": 1, "no_text": 1, "end": True}
    assert [r["message_id"] for r in page["reembed"]] == ["edited", "unsealed"]
    assert set(page["current_hashes"]) == {"edited", "leftover", "unsealed"}
    assert "synthetic" not in json.dumps({k: v for k, v in page.items() if k != "reembed"}, default=sorted)


def test_audit_is_counts_only(conn):
    counts = audit(conn)
    assert counts[EXPORT] == {"scanned": 6, "stale": 3, "needs_model": 2, "leftover_only": 1, "unreadable": 0,
                              "share_excluded": 1, "no_text": 1}
    assert counts[VISITS] == {"scanned": 3, "stale": 1, "needs_model": 1, "leftover_only": 0, "unreadable": 0,
                              "share_excluded": 0, "no_text": 0}
    assert "topos_derivation" not in counts
    assert "synthetic" not in json.dumps(counts)


@pytest.mark.asyncio
async def test_payloads_and_results_hold_ids_and_counts_only(conn, model_calls):
    await run_pass(conn)
    rows = conn.execute("SELECT payload_json, detail_json FROM pipeline_jobs WHERE kind=?", (KIND,)).fetchall()
    assert rows and "synthetic" not in json.dumps([tuple(r) for r in rows])


def test_the_worker_runs_this_kind_on_the_catchup_lane():
    from topos.pipeline.job_runner import EXECUTORS, _catchup_kinds, _executable_kinds

    assert KIND in EXECUTORS and KIND in _catchup_kinds() and KIND not in _executable_kinds()


def test_the_release_notes_declare_the_lane_and_ask_for_no_step():
    from pathlib import Path

    import topos

    data = json.loads((Path(topos.__file__).parent / "upgrades" / "manifests.json").read_text())
    unreleased = next(r for r in data["releases"] if r["version"] == "unreleased")
    assert any(KIND in note for note in unreleased.get("notes", []))
    assert not [s for s in unreleased.get("steps", []) if KIND in json.dumps(s)]
