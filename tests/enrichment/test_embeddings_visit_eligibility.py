"""Embeddings-job eligibility + dedup policy for browser activity rows.

August 2026: 0/1,034 live visit rows reached signal_embeddings. The lane was
gated off by a stale runtime install (see
tests/features/test_runtime_install_lane_policy.py); these tests pin the
row-level contract so the job itself can never silently drop the family:

  * title-only activity rows (browser visits carry no content column) ARE
    embed-eligible;
  * reloaded activity batches without a table stamp still classify as
    activity_event (backfills go through load_canonical_records_for_signal,
    which strips the stamp);
  * ambient activity rows embed ONE vector per distinct text — a page
    revisited 30 times must not contribute 30 near-identical ANN neighbors;
  * message-family rows are NEVER deduped — each message must stay
    individually reachable through vector search.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from topos.enrichment.job_writer import write_signal_records
from topos.enrichment.jobs.canonical.embeddings_job import EmbeddingsJob
from topos.storage.adapters.factory import AdapterFactory


class _FakeResult:
    def __init__(self, vectors: List[List[float]]) -> None:
        self.status = "completed"
        self.output = {
            "vectors": vectors,
            "dims": 3,
            "provider": "huggingface",
            "model": "fake-embed",
            "normalized": True,
        }


class _FakeEngine:
    def __init__(self) -> None:
        self.embedded_texts: List[str] = []

    def run(self, task: Any) -> _FakeResult:
        texts = list(task.input.get("texts") or [])
        self.embedded_texts.extend(texts)
        return _FakeResult([[0.1, 0.2, 0.3] for _ in texts])


def _visit_row(event_id: str, title: str, url: str, **extra: Any) -> Dict[str, Any]:
    # Steady-state shape from canonicalize_normalized_batch: no content column,
    # text lives in title/url, table stamp present.
    return {
        "event_id": event_id,
        "record_id": event_id,
        "activity_type": "visit",
        "title": title,
        "url": url,
        "occurred_at": "2026-08-01T00:00:00Z",
        "source_id": "browser_visits",
        "_table": "activity_events",
        **extra,
    }


def _enrich(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], _FakeEngine]:
    engine = _FakeEngine()
    job = EmbeddingsJob(engine=engine)
    return asyncio.run(job.enrich(rows)), engine


def test_title_only_visit_rows_are_embedded() -> None:
    results, engine = _enrich(
        [
            _visit_row("ev1", "Dune review — Ars Technica", "https://arstechnica.com/dune"),
            _visit_row("ev2", "SQLite WAL mode", "https://sqlite.org/wal.html"),
        ]
    )
    assert len(results) == 2
    assert {r["record_id"] for r in results} == {"ev1", "ev2"}
    assert all(r["record_type"] == "activity_event" for r in results)
    assert len(engine.embedded_texts) == 2


def test_reloaded_visit_rows_without_table_stamp_stay_activity() -> None:
    # load_canonical_records_for_signal round-trips activity rows WITHOUT the
    # _table stamp; activity_type is the marker that survives. Without this
    # fallback, backfilled rows carry record_type=None and the cross-record
    # dedup in the write path never fires for them.
    row = _visit_row("ev1", "Dune review — Ars Technica", "https://arstechnica.com/dune")
    row.pop("_table")
    results, _ = _enrich([row])
    assert len(results) == 1
    assert results[0]["record_type"] == "activity_event"


def test_duplicate_visit_titles_embed_once_per_batch() -> None:
    rows = [
        _visit_row(f"ev{i}", "Feed | LinkedIn", "https://www.linkedin.com/feed/")
        for i in range(3)
    ]
    results, engine = _enrich(rows)
    assert len(results) == 1
    assert len(engine.embedded_texts) == 1


def test_identical_messages_are_not_deduped() -> None:
    rows = [
        {
            "message_id": mid,
            "content": "sounds good, see you at 6",
            "source_id": "imessage",
            "_table": "conversation_messages",
            "ts": "2026-08-01T00:00:00Z",
        }
        for mid in ("m1", "m2")
    ]
    results, engine = _enrich(rows)
    assert len(results) == 2
    assert len(engine.embedded_texts) == 2


# ---------------------------------------------------------------- write path


def _embedding_record(
    record_id: str,
    content_hash: str,
    *,
    record_type: str = "activity_event",
) -> Dict[str, Any]:
    return {
        "message_id": record_id,
        "record_id": record_id,
        "source_id": "browser_visits",
        "vector": [0.1, 0.2, 0.3],
        "dims": 3,
        "model": "fake-embed",
        "provider": "huggingface",
        "signal_dimension": "interests",
        "content_hash": content_hash,
        "chunk_index": 0,
        "record_type": record_type,
    }


def _written_record_ids(bundle: Any) -> List[str]:
    page = bundle.vector.list_metadata(limit=100)
    return sorted(str(item.get("record_id")) for item in page.items)


def _write(bundle: Any, records: List[Dict[str, Any]]) -> None:
    write_signal_records(
        "embeddings",
        records,
        adapters=bundle,
        provenance={"provider": "huggingface", "model": "fake-embed"},
    )


def test_cross_record_dedup_skips_repeat_activity_text() -> None:
    bundle = AdapterFactory.create("memory")
    _write(bundle, [_embedding_record("v1", "hashA")])
    # Same page text arriving later under a new event_id: skipped.
    _write(bundle, [_embedding_record("v2", "hashA")])
    # Distinct page text: written.
    _write(bundle, [_embedding_record("v3", "hashB")])
    assert _written_record_ids(bundle) == ["v1", "v3"]


def test_cross_record_dedup_leaves_message_rows_alone() -> None:
    bundle = AdapterFactory.create("memory")
    _write(bundle, [_embedding_record("m1", "hashC", record_type="conversation_message")])
    _write(bundle, [_embedding_record("m2", "hashC", record_type="conversation_message")])
    assert _written_record_ids(bundle) == ["m1", "m2"]


def test_same_record_refresh_is_not_a_duplicate() -> None:
    bundle = AdapterFactory.create("memory")
    _write(bundle, [_embedding_record("v1", "hashA")])
    # Content changed on the SAME record: the new hash must be written, not
    # treated as someone else's duplicate.
    _write(bundle, [_embedding_record("v1", "hashA2")])
    page = bundle.vector.list_metadata(limit=100)
    assert [str(i.get("record_id")) for i in page.items] == ["v1"]
    assert str(page.items[0].get("content_hash")) == "hashA2"
