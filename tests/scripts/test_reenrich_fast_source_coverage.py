"""reenrich_fast must cover more than the static registry.

The 2026-07-27 facts backfill iterated REGISTRY keys only, so app-ingest lanes
(grow_journal → journal_entries via runtime install) and declared-mapping
writes into a sibling table (github_activity rows in journal_entries) were
silently never re-enriched. collect_work_items enumerates all three source
populations: registry, runtime installs, and orphan source_ids in canonical
tables.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
from topos.sources.registry import REGISTRY
from topos.storage.db.migrations import apply_all_migrations

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "reenrich_fast",
    ROOT / "scripts" / "reenrich_fast.py",
)
assert _SPEC and _SPEC.loader
reenrich_fast = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reenrich_fast)

_GROW_JOURNAL_DEF = {
    "source_id": "grow_journal",
    "display_name": "Grow Journal",
    "source_type": "ui_stream",
    "schema_id": "journal.time_log.v1",
    "parser_id": "journal.time_log.v1",
    "canonical_group_id": "journal",
    "posture": "personal",
}


def _seeded_conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "reenrich.db"))
    apply_all_migrations(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_runtime_installs (
            install_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            source_id TEXT NOT NULL,
            version_id TEXT,
            status TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            source_definition_json TEXT NOT NULL,
            source_version_row_json TEXT,
            failure_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO source_runtime_installs
            (install_id, scope_key, source_id, status, is_active,
             source_definition_json, created_at, updated_at)
        VALUES ('i-grow', 'user:owner', 'grow_journal', 'active', 1, ?, '2026-07-15', '2026-07-15')
        """,
        (json.dumps(_GROW_JOURNAL_DEF),),
    )
    # A retired install must NOT shadow the active one, whatever rowid order.
    conn.execute(
        """
        INSERT INTO source_runtime_installs
            (install_id, scope_key, source_id, status, is_active,
             source_definition_json, created_at, updated_at)
        VALUES ('i-grow-old', 'user:owner', 'grow_journal', 'rolled_back', 0, '{}', '2026-07-01', '2026-07-01')
        """
    )
    # Runtime-installed app-ingest lane rows (missed by the registry-only pass).
    conn.execute(
        "INSERT INTO journal_entries (entry_id, entry_at, content, source_id) "
        "VALUES ('j-grow-1', '2026-07-20T10:00:00Z', 'watered the plants', 'grow_journal')"
    )
    # Same source writing a sibling canonical table (second lane).
    conn.execute(
        "INSERT INTO location_events (event_id, place_name, event_at, source_id) "
        "VALUES ('l-grow-1', 'greenhouse', '2026-07-20T10:00:00Z', 'grow_journal')"
    )
    # Registry source with declared-mapping rows outside its own lane
    # (github_activity's canonical_group_id is activity, not journal).
    conn.execute(
        "INSERT INTO journal_entries (entry_id, entry_at, content, source_id) "
        "VALUES ('j-gh-1', '2026-07-21T09:00:00Z', 'worked on topos', 'github_activity')"
    )
    # Orphan rows: no registry entry, no surviving install row.
    conn.execute(
        "INSERT INTO journal_entries (entry_id, entry_at, content, source_id) "
        "VALUES ('j-orphan-1', '2026-07-22T08:00:00Z', 'hand-injected row', 'orphan_src')"
    )
    conn.commit()
    return conn


def test_collect_work_items_covers_runtime_offlane_and_orphan_sources(tmp_path):
    conn = _seeded_conn(tmp_path)
    work = reenrich_fast.collect_work_items(conn, REGISTRY)
    by_key = {(sid, group): sdef for sid, sdef, group in work}

    assert len(by_key) == len(work), "duplicate (source_id, group) work items"

    # Runtime install on its own lane, with the persisted definition.
    grow_journal = by_key[("grow_journal", "journal")]
    assert grow_journal.schema_id == "journal.time_log.v1"
    assert grow_journal.posture == "personal"

    # Same source's sibling-table rows get a lane-patched copy that keeps the
    # rest of the definition (posture must not fall back to 'mixed').
    grow_places = by_key[("grow_journal", "places")]
    assert grow_places.canonical_group_id == "places"
    assert grow_places.posture == "personal"

    # Registry source: native lane plus the off-lane journal rows.
    assert ("github_activity", "activity") in by_key
    gh_journal = by_key[("github_activity", "journal")]
    assert gh_journal.canonical_group_id == "journal"

    # Orphan source_id gets a synthesized definition.
    orphan = by_key[("orphan_src", "journal")]
    assert orphan.source_id == "orphan_src"

    # The registry pass itself is intact.
    assert ("demo_journal_file", "journal") in by_key


def test_collected_defs_actually_load_the_missed_rows(tmp_path):
    conn = _seeded_conn(tmp_path)
    work = reenrich_fast.collect_work_items(conn, REGISTRY)
    by_key = {(sid, group): sdef for sid, sdef, group in work}

    loaded = {
        key: load_canonical_records_for_signal(conn, sdef)
        for key, sdef in by_key.items()
        if key[0] in ("grow_journal", "github_activity", "orphan_src")
    }
    assert [r["entry_id"] for r in loaded[("grow_journal", "journal")]] == ["j-grow-1"]
    assert [r["event_id"] for r in loaded[("grow_journal", "places")]] == ["l-grow-1"]
    assert [r["entry_id"] for r in loaded[("github_activity", "journal")]] == ["j-gh-1"]
    assert [r["entry_id"] for r in loaded[("orphan_src", "journal")]] == ["j-orphan-1"]
    # Rows keep their real source_id so scope + provenance stay correct.
    assert loaded[("grow_journal", "journal")][0]["source_id"] == "grow_journal"


def test_collect_work_items_honors_requested_sources(tmp_path):
    conn = _seeded_conn(tmp_path)
    work = reenrich_fast.collect_work_items(conn, REGISTRY, requested={"grow_journal"})
    assert {(sid, group) for sid, _sdef, group in work} == {
        ("grow_journal", "journal"),
        ("grow_journal", "places"),
    }
