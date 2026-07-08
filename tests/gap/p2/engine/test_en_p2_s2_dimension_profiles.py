"""
Gap: Profiles — only measured dimensions get health rows after derive
Sprint: EN-P2-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.features.signal.dimension_profiles import DimensionProfileUpdater
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


def test_dimension_profiles_empty_db_writes_nothing(tmp_path) -> None:
    """Honesty rule: no real signal → no health rows (not fake zeros)."""
    conn = sqlite3.connect(str(tmp_path / "prof.db"))
    ensure_migrations_applied(conn)
    bundle = AdapterFactory.create("local_database", conn=conn)
    written = DimensionProfileUpdater(bundle, conn).upsert_all()
    assert written == 0
    rows = conn.execute("SELECT COUNT(*) FROM data_health_dimension").fetchone()
    assert int(rows[0]) == 0


def test_dimension_profiles_upsert_measured_dimensions(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "prof.db"))
    ensure_migrations_applied(conn)
    bundle = AdapterFactory.create("local_database", conn=conn)
    bundle.signal.put_fact(
        {"dimension": "memory", "source_id": "src", "record_id": "m1", "topic": "work"}
    )
    written = DimensionProfileUpdater(bundle, conn).upsert_all()
    assert written == 1
    rows = conn.execute(
        "SELECT signal_dimension, score FROM data_health_dimension"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "memory"
    assert 0.0 < float(rows[0][1]) < 1.0  # continuous, not a 0/100 cliff
