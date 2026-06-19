"""
Gap: Missing RAW_RETENTION audit stage
PRD: PRD_02
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.pipeline.audit import SQLiteIngestAuditStore
from topos.pipeline.stages import PipelineStage
from remediation_helpers import sqlite_conn

pytestmark = pytest.mark.gap


def test_raw_retention_stage_exists() -> None:
    assert PipelineStage.RAW_RETENTION.value == "raw_retention"
    conn = sqlite_conn()
    audit = SQLiteIngestAuditStore(conn)
    from topos.pipeline.audit import StageAuditRow

    audit.append_stage(
        StageAuditRow(
            sync_batch_id="batch-audit",
            source_id="chatgpt_file_ingestion",
            stage=PipelineStage.RAW_RETENTION,
            status="completed",
            records_in=1,
            records_out=1,
        )
    )
    rows = audit.query_by_batch("batch-audit")
    assert any(r["stage"] == PipelineStage.RAW_RETENTION.value for r in rows)
