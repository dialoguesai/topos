"""
Gap: Sentiment stub
PRD: PRD_03
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager
from remediation_helpers import sqlite_conn

pytestmark = pytest.mark.gap


def test_sentiment_batch_writes_rows() -> None:
    conn = sqlite_conn()
    tables = DerivedTablesManager(conn=conn)
    written = tables.write_enrichment_batch([
        {"message_id": "m1", "record_id": "m1", "label": "positive", "score": 0.9, "source_id": "chatgpt_file_ingestion"},
    ], "message_sentiment")
    assert written >= 1
