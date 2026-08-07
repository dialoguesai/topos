"""clear_derivation_retry must never leave the connection mid-transaction.

Regression for 2026-08-06: the 0-row UPDATE in ``clear_derivation_retry``
opened an implicit transaction that the old conditional commit skipped, so the
shared connection stayed in-transaction and every later ``BEGIN IMMEDIATE``
(topic_clusters recompute, job claims) failed — killing topic derivation on
every single batch.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.derivation_recovery import (
    clear_derivation_retry,
    record_failed_derivation,
)
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "derive.db"))
    apply_pipeline_jobs_v1_up(db)
    yield db
    db.close()


def test_clear_with_no_matching_debt_leaves_no_open_transaction(
    conn: sqlite3.Connection,
) -> None:
    assert (
        clear_derivation_retry(conn, sync_batch_id="b-none", job_name="topic_clusters")
        is False
    )
    assert not conn.in_transaction
    # The exact statement that used to die on the leaked transaction.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("ROLLBACK")


def test_record_then_clear_roundtrip(conn: sqlite3.Connection) -> None:
    job_id = record_failed_derivation(
        conn,
        source_id="browser_visits",
        sync_batch_id="b1",
        job_name="topic_clusters",
        error="cannot start a transaction within a transaction",
        record_ids=["r1"],
        record_count=1,
    )
    assert job_id
    assert not conn.in_transaction
    assert (
        clear_derivation_retry(conn, sync_batch_id="b1", job_name="topic_clusters")
        is True
    )
    assert not conn.in_transaction
