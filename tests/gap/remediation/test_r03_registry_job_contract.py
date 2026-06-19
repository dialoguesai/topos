"""Gap: registry job contract — PRD_03"""
import pytest
from topos.enrichment.registry_contract import validate_registry_job_contract
pytestmark = pytest.mark.gap

def test_registry_jobs_have_writers_or_deferral() -> None:
    assert validate_registry_job_contract() == []
