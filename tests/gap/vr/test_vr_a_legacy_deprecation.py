"""Gap tests for deprecated message_embeddings writes (Phase A)."""

from __future__ import annotations

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager
from remediation_helpers import sqlite_conn

pytestmark = pytest.mark.gap


def test_message_embeddings_write_deprecated() -> None:
    conn = sqlite_conn()
    tables = DerivedTablesManager(conn=conn)
    written = tables.write_enrichment_batch(
        [{"record_id": "m1", "vector": [0.1], "model": "test"}],
        "message_embeddings",
    )
    assert written == 0
