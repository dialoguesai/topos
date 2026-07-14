"""Tests for repair_derivation script."""

from __future__ import annotations

import sqlite3

from topos.features.timeline_projection import project_canonical_timeline, timeline_coverage_for_source
from topos.storage.db.migrations import apply_all_migrations


def test_repair_derivation_dry_run_reports_timeline_gaps(tmp_path) -> None:
    db_path = tmp_path / "repair.db"
    conn = sqlite3.connect(str(db_path))
    apply_all_migrations(conn)
    conn.execute(
        """
        INSERT INTO activity_events (event_id, source_id, occurred_at, url, title)
        VALUES ('gap-event', 'browser_visits', '2026-07-13T12:00:00Z', 'https://example.com', 'Example')
        """
    )
    project_canonical_timeline(conn, source_id="browser_visits", missing_only=True)
    conn.execute("DELETE FROM timeline WHERE record_id='gap-event'")
    conn.commit()

    stats = timeline_coverage_for_source(conn, "browser_visits")
    assert stats["missing_records"] >= 1

    repaired = project_canonical_timeline(
        conn,
        source_id="browser_visits",
        missing_only=True,
        dry_run=False,
    )
    assert repaired["totals"]["written"] >= 1
