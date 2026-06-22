"""
Gap: Embeddings stub
PRD: PRD_03
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import sqlite_conn

pytestmark = pytest.mark.gap


def test_embeddings_persist_to_vector_adapter() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    records = [{
        "message_id": "m1",
        "record_id": "m1",
        "source_id": "chatgpt_file_ingestion",
        "model": "test-model",
        "provider": "test",
        "vector": [0.1, 0.2],
        "dims": 2,
    }]
    adapter_written = write_signal_records(
        "embeddings",
        records,
        adapters=bundle,
        provenance={"job_id": "embeddings", "sync_batch_id": "b1"},
    )
    assert adapter_written >= 1
    page = bundle.vector.list_metadata(limit=10, offset=0)
    assert page.total >= 1
