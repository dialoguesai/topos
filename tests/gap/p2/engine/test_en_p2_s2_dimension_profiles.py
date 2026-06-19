"""
Gap: Profiles — empty → 5 dimensions updated after derive
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


def test_dimension_profiles_upsert_five(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "prof.db"))
    ensure_migrations_applied(conn)
    bundle = AdapterFactory.create("local_database", conn=conn)
    written = DimensionProfileUpdater(bundle, conn).upsert_all()
    assert written == 5
    rows = conn.execute("SELECT COUNT(*) FROM signal_dimension_profiles").fetchone()
    assert int(rows[0]) >= 5
