"""One iMessage sync per dataset at a time, and only into the enrolled dataset.

The enrolled dataset's EXISTING ``user_ingestion_sources`` row must be the one a
sync updates: the p2c provenance enrollment watches that table with triggers,
and a new row (an INSERT) moves its source clock, which stales the enrollment
until the owner re-proves it. These tests install the enrollment store's own
trigger SQL (``IngestProvenanceService._schema``) on a synthetic node database
and assert the clock does not move.

Synthetic chat.db and node databases only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import topos.ingestion.local_sync as local_sync
import topos.ingestion.sources.imessage_reader as reader
from topos.ingestion.local_sync import (
    DATASET_NOT_ENROLLED,
    OUTCOME_NEEDS_CONFIRMATION,
    SYNC_IN_PROGRESS,
    exclusive_sync,
    run_imessage_sync,
)
from topos.ingestion.local_sync_jobs import enqueue_local_sync_blocking
from topos.permissions_v2.ingest_protocol import IMESSAGE_READER_CONTRACT
from topos.permissions_v2.ingest_provenance import IngestProvenanceService
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up
from topos.storage.source_settings import ensure_table as ensure_source_settings_table
from tests.sources.test_imessage_spam_filter import _add_message, _make_chat_db, mac_ns

ENROLLED = "owner:topos:enrolled"
OTHER = "owner:default:other"
NEW_DAY = datetime(2026, 9, 28, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _synthetic_sources_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMESSAGE_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setattr(local_sync, "_run_local_sync_enrichment_if_enabled", lambda **_kw: None)


def _chat_db(path: Path, rowids=(1, 2, 3)) -> Path:
    chat = _make_chat_db(path)
    for rowid in rowids:
        _add_message(chat, rowid=rowid, chat_id=1, handle_id=1, text=f"m{rowid}", date=mac_ns(NEW_DAY))
    chat.close()
    return path


def _node(tmp_path: Path, *, enrolled: str | None = ENROLLED) -> sqlite3.Connection:
    """A node database with the enrolled source row, an enrollment, and the v2 source clock."""
    conn = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    ensure_source_settings_table(conn)
    conn.execute(
        "INSERT INTO user_ingestion_sources (dataset_id, source_id, enabled, last_sync_at) "
        "VALUES (?, 'imessage', 1, '2026-09-27T02:46:00+00:00')",
        (ENROLLED,),
    )
    conn.commit()
    if enrolled is not None:
        schema = IngestProvenanceService._schema(None, conn, 2)
        for name in ("ingest_provenance_state", "ingest_provenance_enrollments"):
            conn.execute(schema[name])
        for name, sql in schema.items():
            if name.startswith("ingest_provenance_user_ingestion_sources_"):
                conn.execute(sql)
        conn.execute(
            "INSERT INTO ingest_provenance_state VALUES (1, 'store', '{}', 'rev', 0)"
        )
        conn.execute(
            "INSERT INTO ingest_provenance_enrollments VALUES (?, ?, ?, 1, 'active', 0, 'attested', 0, 'uds')",
            ("enr-1", json.dumps({"reader_contract": IMESSAGE_READER_CONTRACT}), enrolled),
        )
        conn.commit()
    return conn


def _clock(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT generation FROM ingest_provenance_state").fetchone()[0])


def _source_rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT dataset_id, source_id, enabled FROM user_ingestion_sources ORDER BY dataset_id"
    ).fetchall()


def _confirmed(conn, dataset_id, chat_db, **options) -> dict:
    """First sync of a dataset: take the plan, then confirm it."""
    plan = run_imessage_sync(dataset_id, db_conn=conn, chat_db_path=chat_db, sync_options={"mode": "since_last", **options})
    assert plan["outcome"] == OUTCOME_NEEDS_CONFIRMATION, plan
    return run_imessage_sync(
        dataset_id,
        db_conn=conn,
        chat_db_path=chat_db,
        sync_options={"mode": "since_last", "confirm_start_rowid": plan["plan"]["start_rowid"], **options},
    )


# --- the source clock never sees an insert ------------------------------------


def test_the_test_clock_is_live() -> None:
    """Without this, 'the clock did not move' below could be a dead trigger."""
    db = sqlite3.connect(":memory:")
    ensure_source_settings_table(db)
    schema = IngestProvenanceService._schema(None, db, 2)
    db.execute(schema["ingest_provenance_state"])
    for name, sql in schema.items():
        if name.startswith("ingest_provenance_user_ingestion_sources_"):
            db.execute(sql)
    db.execute("INSERT INTO ingest_provenance_state VALUES (1, 'store', '{}', 'rev', 0)")
    db.execute("INSERT INTO user_ingestion_sources (dataset_id, source_id, enabled) VALUES ('x', 'imessage', 1)")
    assert _clock(db) == 1, "an INSERT must advance the clock"
    db.execute("UPDATE user_ingestion_sources SET last_sync_at = 'now' WHERE dataset_id = 'x'")
    assert _clock(db) == 1, "a receipt column is not watched under v2"
    db.execute("UPDATE user_ingestion_sources SET enabled = 0 WHERE dataset_id = 'x'")
    assert _clock(db) == 2, "the enable switch is watched"


@pytest.mark.asyncio
async def test_a_sync_of_the_enrolled_dataset_updates_its_existing_row_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor path end to end: the receipt lands on the row that exists."""
    from topos.pipeline import job_runner

    _chat_db(tmp_path / "chat.db")  # read through IMESSAGE_CHAT_DB, as the job does
    conn = _node(tmp_path)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr(
        "topos.analytics.messenger_communities.compute_and_persist_messenger_analytics", lambda **kw: None
    )
    # The dataset already has a checkpoint from its last sync.
    from topos.ingestion.checkpoints.checkpoint_store import IngestionCheckpoint
    from topos.ingestion.checkpoints.sqlite_checkpoint_store import SqliteCheckpointStore

    SqliteCheckpointStore(conn).save_checkpoint(
        IngestionCheckpoint(dataset_id=ENROLLED, schema_id=local_sync.IMESSAGE_SCHEMA_ID,
                            last_record_id="imessage:1", metadata={})
    )
    rows_before, clock_before = _source_rows(conn), _clock(conn)

    result = await job_runner._execute_local_sync(
        {"source_id": "imessage", "dataset_id": ENROLLED, "sync_options": {"mode": "all"}}
    )

    assert result["status"] == "ok", result
    assert result["sync"]["outcome"] == "imported"
    assert result["records_processed"] == 2
    assert _source_rows(conn) == rows_before, "no new source row"
    assert _clock(conn) == clock_before, "the enrollment's source clock did not move"
    last_sync_at = conn.execute(
        "SELECT last_sync_at FROM user_ingestion_sources WHERE dataset_id = ?", (ENROLLED,)
    ).fetchone()[0]
    assert last_sync_at > "2026-09-27T02:46:00+00:00", "the receipt moved on the existing row"
    datasets = {r[0] for r in conn.execute("SELECT DISTINCT dataset_id FROM conversation_messages")}
    assert datasets == {ENROLLED}


def test_a_sync_into_another_dataset_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    chat_db = _chat_db(tmp_path / "chat.db")
    conn = _node(tmp_path)
    rows_before, clock_before = _source_rows(conn), _clock(conn)

    result = run_imessage_sync(OTHER, db_conn=conn, chat_db_path=chat_db, sync_options={"mode": "3m"})

    assert result["status"] == "error"
    assert result["code"] == DATASET_NOT_ENROLLED
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'"
    ).fetchone() is None or conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
    assert _source_rows(conn) == rows_before
    assert _clock(conn) == clock_before


def test_the_button_hears_the_refusal_before_a_job_exists(tmp_path: Path) -> None:
    conn = _node(tmp_path)
    apply_pipeline_jobs_v1_up(conn)

    outcome = enqueue_local_sync_blocking(conn, source_id="imessage", dataset_id=OTHER, sync_options={"mode": "all"})

    assert outcome["status"] == "error"
    assert outcome["code"] == DATASET_NOT_ENROLLED
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_refused_run_writes_no_receipt_so_no_source_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure receipt would INSERT a row for the other dataset and move the clock."""
    from topos.pipeline import job_runner

    _chat_db(tmp_path / "chat.db")
    conn = _node(tmp_path)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    rows_before, clock_before = _source_rows(conn), _clock(conn)

    result = await job_runner._execute_local_sync(
        {"source_id": "imessage", "dataset_id": OTHER, "sync_options": {"mode": "all"}}
    )

    assert result["code"] == DATASET_NOT_ENROLLED
    assert _source_rows(conn) == rows_before
    assert _clock(conn) == clock_before


def test_the_override_is_explicit(tmp_path: Path) -> None:
    chat_db = _chat_db(tmp_path / "chat.db")
    conn = _node(tmp_path)

    result = _confirmed(conn, OTHER, chat_db, allow_unenrolled_dataset=True)

    assert result["status"] == "ok"
    assert result["records_processed"] == 3


def test_a_node_with_no_enrollment_is_not_guarded(tmp_path: Path) -> None:
    chat_db = _chat_db(tmp_path / "chat.db")
    conn = _node(tmp_path, enrolled=None)

    assert _confirmed(conn, OTHER, chat_db)["records_processed"] == 3


def test_a_revoked_or_other_lane_enrollment_does_not_guard(tmp_path: Path) -> None:
    chat_db = _chat_db(tmp_path / "chat.db")
    conn = _node(tmp_path)
    conn.execute("UPDATE ingest_provenance_enrollments SET state = 'revoked'")
    conn.execute(
        "INSERT INTO ingest_provenance_enrollments VALUES ('enr-2', ?, 'owner:chatgpt', 1, 'active', 0, 'a', 0, 'uds')",
        (json.dumps({"reader_contract": "chatgpt-owner-snapshot/v1"}),),
    )
    conn.commit()

    assert _confirmed(conn, OTHER, chat_db)["records_processed"] == 3


# --- one run per dataset --------------------------------------------------------


def test_a_second_run_is_refused_while_the_dataset_is_held(tmp_path: Path) -> None:
    chat_db = _chat_db(tmp_path / "chat.db")
    conn = _node(tmp_path, enrolled=None)

    with exclusive_sync("imessage", ENROLLED) as held:
        assert held
        refused = run_imessage_sync(ENROLLED, db_conn=conn, chat_db_path=chat_db, sync_options={"mode": "all"})
        # Another dataset is an independent run.
        other = run_imessage_sync(OTHER, db_conn=conn, chat_db_path=chat_db, sync_options={"mode": "all"})

    assert refused["status"] == "error" and refused["code"] == SYNC_IN_PROGRESS
    assert other["status"] == "ok"
    assert run_imessage_sync(ENROLLED, db_conn=conn, chat_db_path=chat_db, sync_options={"mode": "all"})["status"] == "ok"


def test_two_concurrent_runs_of_one_dataset_do_not_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape behind the live 'database is locked' receipt: a second run while the first reads."""
    chat_db = _chat_db(tmp_path / "chat.db")
    first_conn = sqlite3.connect(str(tmp_path / "shared.db"), check_same_thread=False)
    second_conn = sqlite3.connect(str(tmp_path / "shared.db"), check_same_thread=False)
    reading, release = threading.Event(), threading.Event()
    real = reader.inspect_imessage_backlog

    def _slow_plan(*args, **kwargs):
        reading.set()
        release.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(reader, "inspect_imessage_backlog", _slow_plan)
    results: dict = {}
    first = threading.Thread(
        target=lambda: results.setdefault(
            "first", run_imessage_sync(ENROLLED, db_conn=first_conn, chat_db_path=chat_db, sync_options={"mode": "all"})
        )
    )
    first.start()
    assert reading.wait(timeout=10)
    results["second"] = run_imessage_sync(ENROLLED, db_conn=second_conn, chat_db_path=chat_db, sync_options={"mode": "all"})
    release.set()
    first.join(timeout=10)

    assert results["second"]["code"] == SYNC_IN_PROGRESS
    assert results["first"]["status"] == "ok"


@pytest.mark.asyncio
async def test_a_refused_second_run_leaves_the_receipt_to_the_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.pipeline import job_runner

    conn = _node(tmp_path, enrolled=None)
    receipts: list = []
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr(
        "topos.storage.source_settings.update_sync_result", lambda *a, **kw: receipts.append(kw)
    )
    with exclusive_sync("imessage", ENROLLED):
        result = await job_runner._execute_local_sync(
            {"source_id": "imessage", "dataset_id": ENROLLED, "sync_options": {"mode": "all"}}
        )

    assert result["code"] == SYNC_IN_PROGRESS
    assert receipts == []


@pytest.mark.asyncio
async def test_a_plan_waiting_for_the_owner_writes_no_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was synced, so last_sync_at must not move."""
    from topos.pipeline import job_runner

    _chat_db(tmp_path / "chat.db")
    conn = _node(tmp_path, enrolled=None)
    receipts: list = []
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr(
        "topos.storage.source_settings.update_sync_result", lambda *a, **kw: receipts.append(kw)
    )

    result = await job_runner._execute_local_sync(
        {"source_id": "imessage", "dataset_id": "brand:new", "sync_options": {"mode": "since_last"}}
    )

    assert result["status"] == "ok"
    assert result["sync"]["outcome"] == OUTCOME_NEEDS_CONFIRMATION
    assert result["sync"]["plan"]["to_import"] == 3
    assert receipts == []


def test_signal_is_held_per_dataset_too() -> None:
    with exclusive_sync("signal", ENROLLED):
        result = local_sync.run_signal_sync(ENROLLED, db_conn=sqlite3.connect(":memory:"), sync_options={"mode": "all"})
    assert result["code"] == SYNC_IN_PROGRESS
    assert local_sync.run_signal_sync(ENROLLED, db_conn=sqlite3.connect(":memory:"), sync_options={"dry_run": True})["status"] == "error"
