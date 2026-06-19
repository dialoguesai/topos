"""Gap: hosted signal write — PRD_07"""
import pytest
from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import sqlite_conn
pytestmark = pytest.mark.gap

def test_hosted_bundle_accepts_signal_writes() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("hosted_database", conn=conn)
    written = write_signal_records("sentiment", [{"message_id":"m1","record_id":"m1","label":"pos","score":0.5,"source_id":"chatgpt_file_ingestion"}], adapters=bundle, conn=conn, provenance={"job_id":"sentiment"})
    assert written >= 1
