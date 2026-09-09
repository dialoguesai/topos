"""The local_sync job lane: partitioning, mutual exclusion, and the receipt.

These pin the three properties that make it safe to run an hours-long iMessage
sync on the shared pipeline queue.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.pipeline.job_runner import (
    EXECUTORS,
    LOCAL_SYNC_KIND,
    _executable_kinds,
    _long_running_kinds,
)
from topos.pipeline.job_store import claim_next_job, enqueue_job, find_active_job
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    yield db


def test_local_sync_is_kept_out_of_the_general_lane() -> None:
    """The general worker must not be able to claim a sync.

    It is strictly serial — one claim, then `await process_job` inline — so a
    multi-hour sync sitting in it would stall inbox_deferred_enrichment,
    file_ingestion, enrichment_process_source, topic_consolidation and
    signal_derive_retry for the whole run. That is the regression this
    partition exists to prevent.
    """
    assert LOCAL_SYNC_KIND in EXECUTORS
    assert LOCAL_SYNC_KIND not in _executable_kinds()
    assert _long_running_kinds() == [LOCAL_SYNC_KIND]


def test_the_two_lanes_together_cover_every_executor() -> None:
    """No kind may fall between the lanes and sit queued forever."""
    assert sorted(set(_executable_kinds()) | set(_long_running_kinds())) == sorted(EXECUTORS)
    assert not set(_executable_kinds()) & set(_long_running_kinds())


def test_the_general_lane_still_holds_exactly_the_pre_existing_kinds() -> None:
    """Adding a kind must not quietly move an existing one off its worker."""
    assert _executable_kinds() == [
        "enrichment_process_source",
        "file_ingestion",
        "inbox_deferred_enrichment",
        "signal_derive_retry",
        "topic_consolidation",
    ]


def test_a_queued_sync_is_invisible_to_a_general_lane_claim(conn: sqlite3.Connection) -> None:
    enqueue_job(
        conn,
        kind=LOCAL_SYNC_KIND,
        payload={"source_id": "imessage", "dataset_id": "ds"},
        job_id="sync-1",
        source_id="imessage",
        idempotency_key=f"{LOCAL_SYNC_KIND}:sync-1",
    )
    assert claim_next_job(conn, lease_owner="general", kinds=_executable_kinds()) is None
    claimed = claim_next_job(conn, lease_owner="long", kinds=_long_running_kinds())
    assert claimed is not None and claimed["job_id"] == "sync-1"


def test_find_active_job_sees_queued_and_running_but_not_finished(conn: sqlite3.Connection) -> None:
    """Mutual exclusion must not outlive the run that needs it.

    The idempotency key cannot serve as the in-flight check: enqueue_job hands
    back a `done` row's id untouched, so a stable key would make the first
    success block every later sync forever. This lookup only sees live rows.
    """
    enqueue_job(
        conn,
        kind=LOCAL_SYNC_KIND,
        payload={"source_id": "imessage", "dataset_id": "ds"},
        job_id="sync-a",
        source_id="imessage",
        idempotency_key=f"{LOCAL_SYNC_KIND}:sync-a",
    )
    found = find_active_job(conn, kind=LOCAL_SYNC_KIND, source_id="imessage", dataset_id="ds")
    assert found is not None and found["job_id"] == "sync-a"

    # running is still active
    claim_next_job(conn, lease_owner="long", kinds=_long_running_kinds())
    assert find_active_job(conn, kind=LOCAL_SYNC_KIND, source_id="imessage", dataset_id="ds")

    # done is not: the next sync must be allowed to start
    conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id='sync-a'")
    conn.commit()
    assert find_active_job(conn, kind=LOCAL_SYNC_KIND, source_id="imessage", dataset_id="ds") is None


def test_find_active_job_separates_datasets(conn: sqlite3.Connection) -> None:
    """Two datasets syncing one source are independent runs, not a collision."""
    enqueue_job(
        conn,
        kind=LOCAL_SYNC_KIND,
        payload={"source_id": "imessage", "dataset_id": "ds-one"},
        job_id="sync-one",
        source_id="imessage",
        idempotency_key=f"{LOCAL_SYNC_KIND}:sync-one",
    )
    assert find_active_job(
        conn, kind=LOCAL_SYNC_KIND, source_id="imessage", dataset_id="ds-one"
    )
    assert (
        find_active_job(conn, kind=LOCAL_SYNC_KIND, source_id="imessage", dataset_id="ds-two")
        is None
    )


@pytest.mark.asyncio
async def test_executor_writes_the_success_receipt(monkeypatch, tmp_path) -> None:
    """The executor owns `last_sync_at`, so it lands whether or not a caller waits.

    This is the owner's actual complaint: the receipt used to be written by the
    websocket handler's coroutine, which was gone by the time an hours-long sync
    finished, so `user_ingestion_sources.last_sync_at` stayed empty forever even
    though the sync had moved tens of thousands of rows.
    """
    from topos.pipeline import job_runner

    db = sqlite3.connect(str(tmp_path / "receipt.db"), check_same_thread=False)
    receipts: list[dict] = []

    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr(
        "topos.ingestion.local_sync.run_imessage_sync",
        lambda dataset_id, **kw: {"status": "ok", "records_processed": 7, "last_record_id": "imessage:7"},
    )
    monkeypatch.setattr(
        "topos.storage.source_settings.update_sync_result",
        lambda conn, dataset_id, source_id, **kw: receipts.append(
            {"dataset_id": dataset_id, "source_id": source_id, **kw}
        ),
    )
    monkeypatch.setattr(
        "topos.analytics.messenger_communities.compute_and_persist_messenger_analytics",
        lambda **kw: None,
    )

    result = await job_runner._execute_local_sync(
        {"source_id": "imessage", "dataset_id": "ds"}
    )

    assert result["status"] == "ok"
    assert len(receipts) == 1 and receipts[0]["success"] is True
    assert receipts[0]["last_sync_at"]
    # _mark_done reads `messages_processed`; the sync speaks `records_processed`.
    assert result["messages_processed"] == 7
    assert result["records_processed"] == 7


@pytest.mark.asyncio
async def test_executor_records_a_failure_receipt_and_never_raises(monkeypatch, tmp_path) -> None:
    from topos.pipeline import job_runner

    db = sqlite3.connect(str(tmp_path / "receipt2.db"), check_same_thread=False)
    receipts: list[dict] = []
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr(
        "topos.ingestion.local_sync.run_imessage_sync",
        lambda dataset_id, **kw: {"status": "error", "error": "chat.db not found"},
    )
    monkeypatch.setattr(
        "topos.storage.source_settings.update_sync_result",
        lambda conn, dataset_id, source_id, **kw: receipts.append(kw),
    )

    result = await job_runner._execute_local_sync({"source_id": "imessage", "dataset_id": "ds"})

    assert result["status"] == "error"
    assert receipts and receipts[0]["success"] is False
    assert "chat.db" in receipts[0]["last_error"]


@pytest.mark.asyncio
async def test_a_good_sync_is_not_failed_by_a_broken_analytics_refresh(monkeypatch, tmp_path) -> None:
    """Analytics is a courtesy after the fact; it must never lose a real sync."""
    from topos.pipeline import job_runner

    db = sqlite3.connect(str(tmp_path / "receipt3.db"), check_same_thread=False)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr(
        "topos.ingestion.local_sync.run_imessage_sync",
        lambda dataset_id, **kw: {"status": "ok", "records_processed": 3},
    )
    monkeypatch.setattr(
        "topos.storage.source_settings.update_sync_result",
        lambda conn, dataset_id, source_id, **kw: None,
    )

    def _boom(**kw):
        raise RuntimeError("analytics exploded")

    monkeypatch.setattr(
        "topos.analytics.messenger_communities.compute_and_persist_messenger_analytics", _boom
    )

    result = await job_runner._execute_local_sync({"source_id": "imessage", "dataset_id": "ds"})
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_executor_rejects_an_unsupported_source() -> None:
    from topos.pipeline import job_runner

    result = await job_runner._execute_local_sync({"source_id": "slack", "dataset_id": "ds"})
    assert result["status"] == "error"
