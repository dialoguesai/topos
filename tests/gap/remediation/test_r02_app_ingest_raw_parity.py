"""
Gap: App ingest raw parity
PRD: PRD_02
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import inspect

import pytest

from topos.ingestion import ingest_helpers

pytestmark = pytest.mark.gap


def test_app_ingest_helpers_write_raw() -> None:
    src = inspect.getsource(ingest_helpers)
    assert "write_raw_record" in src
