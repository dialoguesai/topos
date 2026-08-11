"""The 1.3.12 sweep that re-dates journal rows stamped with the import clock.

`SQLiteCanonicalStore._journal_entry_at` fixes new writes; rows already on disk
only move if something sweeps them. The predicate is the corruption signature
itself, so a genuine entry_at must survive untouched and the sweep must be safe
to re-run.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.storage.canonical.journal_repair import repair_ingest_clock_dates
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "j.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


def _row(conn, entry_id, entry_at, starts_at, ingested_at, source_id="grow_journal"):
    conn.execute(
        "INSERT INTO journal_entries (entry_id, entry_at, starts_at, content, source_id, ingested_at) "
        "VALUES (?, ?, ?, 'x', ?, ?)",
        (entry_id, entry_at, starts_at, source_id, ingested_at),
    )


def _entry_at(conn, entry_id):
    return conn.execute(
        "SELECT entry_at FROM journal_entries WHERE entry_id=?", (entry_id,)
    ).fetchone()[0]


def test_repairs_the_live_signature(conn):
    _row(conn, "a", "2026-08-08T03:34:44", "2026-06-28T18:00:00", "2026-08-08T03:34:44.291428+00:00")
    conn.commit()
    out = repair_ingest_clock_dates(conn)
    assert out["repaired"] == 1
    assert _entry_at(conn, "a") == "2026-06-28T18:00:00"


def test_leaves_a_genuine_entry_time_alone(conn):
    _row(conn, "b", "2026-07-04T09:15:00", "2026-07-04T09:00:00", "2026-08-08T03:34:44.291428+00:00")
    conn.commit()
    assert repair_ingest_clock_dates(conn)["repaired"] == 0
    assert _entry_at(conn, "b") == "2026-07-04T09:15:00"


def test_rows_without_a_session_start_are_not_guessed_at(conn):
    _row(conn, "c", "2026-08-08T03:34:44", None, "2026-08-08T03:34:44.291428+00:00")
    conn.commit()
    assert repair_ingest_clock_dates(conn)["repaired"] == 0
    assert _entry_at(conn, "c") == "2026-08-08T03:34:44"


def test_is_idempotent(conn):
    _row(conn, "a", "2026-08-08T03:34:44", "2026-06-28T18:00:00", "2026-08-08T03:34:44.291428+00:00")
    conn.commit()
    assert repair_ingest_clock_dates(conn)["repaired"] == 1
    assert repair_ingest_clock_dates(conn)["repaired"] == 0


def test_dry_run_counts_without_writing(conn):
    _row(conn, "a", "2026-08-08T03:34:44", "2026-06-28T18:00:00", "2026-08-08T03:34:44.291428+00:00")
    conn.commit()
    out = repair_ingest_clock_dates(conn, dry_run=True)
    assert out["candidates"] == 1 and out["repaired"] == 0
    assert _entry_at(conn, "a") == "2026-08-08T03:34:44"


def test_reports_per_source_counts(conn):
    _row(conn, "a", "2026-08-08T03:34:44", "2026-06-28T18:00:00", "2026-08-08T03:34:44+00:00")
    _row(conn, "b", "2026-06-28T23:28:45", "2026-05-01T08:00:00", "2026-06-28T23:28:45+00:00", "grow_data_file")
    conn.commit()
    out = repair_ingest_clock_dates(conn)
    assert out["by_source"] == {"grow_journal": 1, "grow_data_file": 1}


def _declared(step_id: str) -> dict:
    """Find a step across EVERY release, not just `unreleased`.

    cut_release.py stamps the staging entry with the version being cut and
    resets `unreleased` to empty, so a test pinned to `unreleased` goes red the
    moment its step actually ships — which is what happened on the 1.3.9 cut.
    The step's home changes at release time; that it is declared does not.
    """
    from topos.upgrades import load_manifests, load_unreleased

    releases = list(load_manifests())
    staging = load_unreleased()
    if staging:
        releases.append(staging)
    found = [s for r in releases for s in r.get("steps", []) if s.get("id") == step_id]
    assert found, f"no release declares {step_id}"
    assert len(found) == 1, f"{step_id} should be declared once, not re-run every release"
    return found[0]


def test_the_manifest_declares_the_step_and_its_dispatch(conn):
    """A step whose endpoint has no internal dispatch fails the whole upgrade.

    This is the 1.3.9 failure mode in miniature: the manifest declared the step,
    nothing dispatched it, and every node ledgered the repair as failed.
    """
    from topos.upgrades.runner import _exec_engine_endpoint

    step = _declared("repair-journal-ingest-clock-dates")
    assert step["kind"] == "engine_endpoint"
    assert step["consent"] == "auto"

    _row(conn, "a", "2026-08-08T03:34:44", "2026-06-28T18:00:00", "2026-08-08T03:34:44+00:00")
    conn.commit()

    # Routes to the sweep rather than raising "no internal dispatch".
    out = _exec_engine_endpoint(step, conn)
    assert out["repaired"] == 1
    assert _entry_at(conn, "a") == "2026-06-28T18:00:00"


def test_the_rebuild_step_runs_after_the_repair():
    """Rebuilding first would re-derive edges from the uncorrected dates."""
    rebuild = _declared("rebuild-entity-graph-after-date-repair")
    assert "repair-journal-ingest-clock-dates" in (rebuild.get("depends_on") or [])
    assert rebuild["kind"] == "derived_rebuild"


def test_both_steps_reach_a_node_upgrading_into_this_release():
    """Declared-but-unreachable is the failure mode this release exists to fix."""
    from topos.upgrades import steps_between
    from topos.__version__ import __version__

    planned = [s["id"] for s in steps_between("1.3.11", __version__)]
    assert "repair-journal-ingest-clock-dates" in planned
    assert "rebuild-entity-graph-after-date-repair" in planned
    # Order matters: the repair must be planned before the rebuild.
    assert planned.index("repair-journal-ingest-clock-dates") < planned.index(
        "rebuild-entity-graph-after-date-repair"
    )
