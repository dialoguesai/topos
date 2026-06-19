"""
Gap: Stage audit — log-only → structured queryable audit rows
Sprint: EN-P1-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.pipeline.audit import SQLiteIngestAuditStore, stage_context
from topos.pipeline.stages import PipelineStage
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def test_ingest_audit_queryable_by_sync_batch_id() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    audit = SQLiteIngestAuditStore(conn)
    batch_id = "audit-batch-1"

    with stage_context(
        audit,
        stage=PipelineStage.RAW_WRITE,
        sync_batch_id=batch_id,
        source_id="chatgpt_file_ingestion",
        records_in=3,
    ):
        pass

    with stage_context(
        audit,
        stage=PipelineStage.CANONICAL_MAP,
        sync_batch_id=batch_id,
        source_id="chatgpt_file_ingestion",
        records_in=3,
    ):
        pass

    stages = audit.query_by_batch(batch_id)
    stage_names = [row["stage"] for row in stages]
    assert PipelineStage.RAW_WRITE.value in stage_names
    assert PipelineStage.CANONICAL_MAP.value in stage_names
    assert any(row["status"] == "completed" for row in stages)
