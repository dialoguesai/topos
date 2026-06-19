"""
Gap: Topics stub
PRD: PRD_03
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager
from remediation_helpers import sqlite_conn

pytestmark = pytest.mark.gap


def test_topics_batch_writes_rows() -> None:
    conn = sqlite_conn()
    tables = DerivedTablesManager(conn=conn)
    written = tables.write_enrichment_batch([
        {"message_id": "m1", "record_id": "m1", "topic": "investor meetings", "source_id": "chatgpt_file_ingestion"},
    ], "message_topics")
    assert written >= 1
