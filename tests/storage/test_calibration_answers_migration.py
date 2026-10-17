import sqlite3

from topos.storage.db.migrations.calibration_answers_v1 import (
    MIGRATION_ID, apply_calibration_answers_v1_up, create_calibration_answers,
)
from topos.storage.db.migrations.registry import MIGRATIONS


def test_registered_migration_preserves_answers_created_before_upgrade():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    create_calibration_answers(conn)
    expected = ("icebreakers-v1", "drink", "tea", 3, "fact-1", "2026-10-17")
    conn.execute("INSERT INTO calibration_answers VALUES (?,?,?,?,?,?)", expected)
    apply_calibration_answers_v1_up(conn)
    apply_calibration_answers_v1_up(conn)
    assert conn.execute("SELECT * FROM calibration_answers").fetchone() == expected
    assert conn.execute("SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)).fetchone()[0] == 1
    spec = next(m for m in MIGRATIONS if m.id == MIGRATION_ID)
    assert spec.order == 87 and not spec.always_run
