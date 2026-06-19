"""Gap: deferred jobs surface — PRD_03"""
import pytest
from topos.features.signal.data_health import DataHealthComputer
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import sqlite_conn
pytestmark = pytest.mark.gap

def test_deferred_jobs_add_provider_failures() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    profiles = DataHealthComputer(bundle).compute(deferred_jobs=["dimension_summary"])
    assert "ollama_unreachable" in profiles["memory"]["provider_failures"]
