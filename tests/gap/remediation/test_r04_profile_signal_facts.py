"""Gap: profile signal facts — PRD_04"""
import pytest
from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import sqlite_conn
pytestmark = pytest.mark.gap

def test_profile_facts_present() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    for fact_type, text in [("based_in", "San Francisco"), ("employer", "Acme"), ("role", "Engineer")]:
        bundle.signal.put_fact({"dimension":"profile","fact_type":fact_type,"summary_text":text,"source_id":"chatgpt_file_ingestion"})
    page = bundle.signal.get_by_dimension("profile", limit=10, offset=0)
    assert page.total >= 3
