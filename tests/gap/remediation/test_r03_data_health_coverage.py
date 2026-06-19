"""Gap: Data Health coverage — PRD_03"""
import pytest
from topos.features.signal.data_health import DataHealthComputer
from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import sqlite_conn
pytestmark = pytest.mark.gap

def test_memory_coverage_nonzero_after_signal_write() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    write_signal_records("topics", [{"message_id":"m1","record_id":"m1","topic":"work","source_id":"chatgpt_file_ingestion","model":"t","provider":"p"}], adapters=bundle, conn=conn, provenance={"job_id":"topics"})
    profiles = DataHealthComputer(bundle).compute()
    assert profiles["memory"]["coverage_score"] > 0
