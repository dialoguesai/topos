"""Gap: hosted adapter factory — PRD_07"""
import pytest
from topos.storage.adapters.factory import AdapterFactory
pytestmark = pytest.mark.gap

def test_hosted_database_does_not_raise() -> None:
    bundle = AdapterFactory.create("hosted_database")
    assert bundle.backend == "hosted_database"
