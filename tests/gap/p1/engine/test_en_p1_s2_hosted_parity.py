"""
Gap: Hosted — NotImplemented → Postgres ingest + table list parity
Sprint: EN-P1-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import inspect

import pytest

from topos.storage.canonical.postgres import PostgresCanonicalStore

pytestmark = pytest.mark.gap


def test_postgres_canonical_store_upsert_implemented() -> None:
    assert "upsert" in dir(PostgresCanonicalStore)
    source = inspect.getsource(PostgresCanonicalStore.upsert)
    assert "NotImplementedError" not in source
    assert "ai_chat_messages" in PostgresCanonicalStore.MVP_TABLES
    assert "activity_events" in PostgresCanonicalStore.MVP_TABLES


@pytest.mark.skipif(
    not pytest.importorskip("psycopg2", reason="psycopg2 not installed"),
    reason="requires psycopg2",
)
def test_postgres_canonical_store_live_roundtrip() -> None:
    pytest.skip("Postgres container not configured in CI — manual matrix in runbook §5")
