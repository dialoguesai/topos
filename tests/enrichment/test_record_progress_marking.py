"""Both enrichment lanes must record that a job ran, not just what it produced.

``enrichment_record_progress`` witnesses the RUN; the coverage tables witness the
OUTPUT. The distinction is the point: a record that ran and legitimately produced
nothing writes no coverage row, so a coverage-only "already done" check re-scans
it forever. Measured 2026-08-25 on imessage/entities, a backfill of 2,400 records
reported 1,288 processed and left 1,903 of the same window still counting as
missing, because three in five messages ("ok", "haha", an emoji) carry no named
entity.

Only ONE lane wrote markers. The manual ``/enrichment`` backfill did; the
automatic ingest lane — the one that runs on every sync — did not, because the
helpers lived in ``api/`` and the orchestrator could not import them without
inverting the layering. Measured 2026-08-27: **0 rows on a node holding 38,838
derived facts.** Nothing was ever skippable, so every re-sync re-derived and
appended, which is what multiplies derived rows 2x–4.3x and why a fabricated row
is never one row to retract but N.

The helpers now live in ``enrichment/record_progress.py`` and both lanes call
them, so the two cannot drift into two definitions of "processed".
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.record_progress import (
    mark_records_processed,
    processed_record_ids,
    record_identifier,
)

SOURCE = "grow_journal"
JOB = "entities"


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "progress.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _msgs(n=5):
    return [{"record_id": f"r{i}", "source_id": SOURCE} for i in range(n)]


# --------------------------------------------------------------- the semantics


def test_marking_records_the_run(conn):
    assert processed_record_ids(conn, SOURCE, JOB, 0) == set()

    assert mark_records_processed(conn, SOURCE, JOB, _msgs(), 1) == 5
    conn.commit()

    assert len(processed_record_ids(conn, SOURCE, JOB, 0)) == 5


def test_a_record_that_produced_nothing_is_still_marked(conn):
    """The whole reason this table exists.

    Coverage witnesses output. A message carrying no named entity produces no
    coverage row and would otherwise be re-scanned by every future backfill,
    indefinitely.
    """
    mark_records_processed(conn, SOURCE, JOB, [{"record_id": "empty-1", "source_id": SOURCE}], 1)
    conn.commit()

    assert "empty-1" in processed_record_ids(conn, SOURCE, JOB, 0)


def test_marking_is_idempotent(conn):
    mark_records_processed(conn, SOURCE, JOB, _msgs(), 1)
    conn.commit()
    mark_records_processed(conn, SOURCE, JOB, _msgs(), 1)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM enrichment_record_progress").fetchone()[0] == 5


def test_a_spec_bump_invalidates_the_markers(conn):
    """A genuine re-derivation must reprocess everything."""
    mark_records_processed(conn, SOURCE, JOB, _msgs(), 1)
    conn.commit()

    assert len(processed_record_ids(conn, SOURCE, JOB, 1)) == 5
    assert processed_record_ids(conn, SOURCE, JOB, 2) == set()


def test_markers_are_scoped_per_job_and_source(conn):
    mark_records_processed(conn, SOURCE, JOB, _msgs(), 1)
    conn.commit()

    assert processed_record_ids(conn, SOURCE, "topics", 0) == set()
    assert processed_record_ids(conn, "imessage", JOB, 0) == set()


@pytest.mark.parametrize(
    "record,expected",
    [
        ({"message_id": "m1"}, "m1"),
        ({"record_id": "r1"}, "r1"),
        ({"entry_id": "e1"}, "e1"),
        ({"event_id": "ev1"}, "ev1"),
        ({"message_id": "m1", "record_id": "r1"}, "m1"),
        ({}, None),
    ],
)
def test_the_record_identifier_covers_every_canonical_id_shape(record, expected):
    assert record_identifier(record) == expected


def test_a_failure_to_mark_never_raises(conn):
    """The enrichment is already committed; a marker failure costs a re-scan.

    Making this fatal would turn a bookkeeping problem into lost enrichment.
    """
    conn.execute("DROP TABLE enrichment_record_progress")
    conn.commit()

    assert mark_records_processed(conn, SOURCE, JOB, _msgs(), 1) == 0
    assert processed_record_ids(conn, SOURCE, JOB, 0) == set()


# ------------------------------------------------- both lanes share one definition


def test_the_api_lane_uses_the_shared_helpers():
    """They used to be defined in ``api/``, which is why only that lane marked.

    If a copy reappears there, the two lanes can drift into two definitions of
    "processed" — the failure this workstream keeps finding.
    """
    from topos.api import enrichment as api_enrichment
    from topos.enrichment import record_progress

    assert api_enrichment._mark_records_processed is record_progress._mark_records_processed
    assert api_enrichment._processed_record_ids is record_progress._processed_record_ids
    assert api_enrichment._record_identifier is record_progress._record_identifier


def test_the_automatic_lane_marks_every_job_it_runs(conn, monkeypatch):
    """The lane that runs on every sync, and never wrote a marker."""
    from topos.enrichment.orchestrator import EnrichmentOrchestrator

    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    orch = EnrichmentOrchestrator.__new__(EnrichmentOrchestrator)
    orch._mark_processed(JOB, _msgs(3))
    conn.commit()

    assert len(processed_record_ids(conn, SOURCE, JOB, 0)) == 3


def test_the_automatic_lane_groups_by_source(conn, monkeypatch):
    """One batch can carry rows from more than one source."""
    from topos.enrichment.orchestrator import EnrichmentOrchestrator

    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    orch = EnrichmentOrchestrator.__new__(EnrichmentOrchestrator)
    orch._mark_processed(
        JOB,
        [
            {"record_id": "a", "source_id": "grow_journal"},
            {"record_id": "b", "source_id": "imessage"},
        ],
    )
    conn.commit()

    assert processed_record_ids(conn, "grow_journal", JOB, 0) == {"a"}
    assert processed_record_ids(conn, "imessage", JOB, 0) == {"b"}

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['failed_write', 'deferred', 'empty_success'])
async def test_completion_is_marked_only_after_successful_persistence(monkeypatch, mode):
    from types import SimpleNamespace
    from topos.enrichment.orchestrator import EnrichmentOrchestrator

    async def enrich(*a, **kw):
        if mode == 'deferred':
            return [{'_deferred': True, 'error': 'provider offline'}]
        return [] if mode == 'empty_success' else [{'record_id': 'm1'}]

    def write(*a, **kw):
        raise RuntimeError('synthetic disk write failure')

    job = SimpleNamespace(get_job_name=lambda: 'synthetic', should_run=lambda rows: True,
                          get_derived_table=lambda: 'message_entities', enrich=enrich)
    orch = EnrichmentOrchestrator()
    orch.canonical_jobs = [job]
    monkeypatch.setattr(orch, '_tables_manager_for_worker',
                        lambda: SimpleNamespace(write_enrichment_batch=write))
    marks = []
    monkeypatch.setattr(orch, '_mark_processed', lambda *a: marks.append(a))
    result = await orch.run_canonical([{'message_id': 'm1', 'source_id': 'imessage', 'content': 'synthetic'}])
    assert bool(marks) == (mode == 'empty_success')
    assert bool(result['errors']) == (mode != 'empty_success')
