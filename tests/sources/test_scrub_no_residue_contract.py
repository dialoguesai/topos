"""Contract guard: a full scrub leaves no attributable residue.

This is the executable form of the "sample data can be completely removed"
guarantee. It runs the REAL derived-intelligence purge (not the stubbed
recompute the e2e tests use) and then asserts, generically over every user
table that carries a source column, that zero rows remain for the scrubbed
source. If a future feature adds a derived table keyed by ``source_id`` /
``source_system`` and forgets to wire it into the attribution sweep or
``derived_scrub``, this test fails — sample residue can't silently return.

The derived layer is local SQLite on every engine (``get_db_connection`` is
always SQLite; Postgres only holds raw/canonical source tables), so exercising
the SQLite path here matches production for both local and hosted engines.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import patch

import pytest

from topos.sources.scrub_attribution import _list_table_columns, _list_user_tables
from topos.sources.scrub_service import SCRUB_SOURCE_OPTIONS, scrub_source_async
from topos.storage.db.migrations import apply_all_migrations

SCRUBBED = "demo_pack_sample"
KEPT = "user_real_source"
SOURCE_COLUMNS = ("source_id", "source_system")

# Tables that intentionally retain a source reference AFTER a scrub because
# they record the removal itself, not the source's content. Keep this list
# tight: anything new landing here should be a conscious decision, which is
# exactly the review checkpoint this guard exists to force.
AUDIT_TABLES = frozenset({"ingest_audit"})


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "scrub-residue.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    monkeypatch.setattr("topos.sources.scrub_service.get_db_connection", lambda: db)
    return db


def _seed_source(conn: sqlite3.Connection, source_id: str, suffix: str) -> None:
    """Seed canonical + derived rows attributed to a source across the layers
    the scrub must reach: canonical, timeline, embeddings, entity graph."""
    conn.execute(
        "INSERT INTO journal_entries (entry_id, source_id, content, entry_at) VALUES (?, ?, ?, ?)",
        (f"entry-{suffix}", source_id, "sample text", "2026-01-01"),
    )
    conn.execute(
        """
        INSERT INTO timeline (event_at, record_id, source_id, canonical_table, record_type)
        VALUES (?, ?, ?, 'journal_entries', 'journal')
        """,
        ("2026-01-01T00:00:00Z", f"entry-{suffix}", source_id),
    )
    conn.execute(
        """
        INSERT INTO signal_embeddings (
            embedding_id, record_id, source_id, signal_dimension, model, provider,
            dims, text_preview, provenance_json
        ) VALUES (?, ?, ?, 'memory', 'test', 'test', 384, 'sample', '{}')
        """,
        (f"emb-{suffix}", f"entry-{suffix}", source_id),
    )
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, mention_count)
        VALUES (?, 'person', ?, ?, 1)
        """,
        (f"ent-{suffix}", f"Name {suffix}", f"name {suffix}"),
    )
    conn.execute(
        """
        INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, surface_text)
        VALUES (?, ?, ?, ?, 'journal_entries', ?)
        """,
        (f"men-{suffix}", f"ent-{suffix}", f"entry-{suffix}", source_id, f"Name {suffix}"),
    )


def _rows_for_source(conn: sqlite3.Connection, source_id: str) -> dict[str, int]:
    """For every user table with a source column, count rows for source_id.

    Mirrors the attribution sweep's own table/column view, so the assertion
    covers exactly what the sweep is responsible for — no hardcoded list."""
    offenders: dict[str, int] = {}
    for table in _list_user_tables(conn):
        if table in AUDIT_TABLES:
            continue
        columns = set(_list_table_columns(conn, table))
        for column in SOURCE_COLUMNS:
            if column not in columns:
                continue
            count = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?', (source_id,)
            ).fetchone()[0]
            if count:
                offenders[f"{table}.{column}"] = int(count)
    return offenders


def test_full_scrub_leaves_no_attributable_residue(conn: sqlite3.Connection) -> None:
    _seed_source(conn, SCRUBBED, "s")
    _seed_source(conn, KEPT, "k")
    conn.commit()

    # Cluster/brief recompute call embeddings/LLM paths; keep them off so the
    # test stays hermetic while the REAL derived-intelligence purge still runs.
    options = replace(
        SCRUB_SOURCE_OPTIONS,
        recompute_topic_clusters=False,
        refresh_dimension_briefs=False,
    )
    assert options.purge_derived_intelligence is True

    with patch("topos.sources.scrub_service.install_service.uninstall_source") as uninstall:
        uninstall.return_value = {"uninstalled": True}
        result = asyncio.run(scrub_source_async(source_id=SCRUBBED, options=options))

    assert result["scrub_status"] in {"completed", "partial"}

    # 1. Coverage invariant: nothing attributable to the scrubbed source may
    #    survive in ANY table carrying a source column.
    residue = _rows_for_source(conn, SCRUBBED)
    assert residue == {}, f"scrub left attributable residue: {residue}"

    # 2. The purge is targeted, not a wipe — the user's real source survives.
    kept = _rows_for_source(conn, KEPT)
    assert kept, "scrub must not remove other sources' rows"
    assert conn.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE source_id = ?", (KEPT,)
    ).fetchone()[0] == 1

    # 3. The derived-intelligence purge actually executed (guards against a
    #    regression that silently disables it — the layer is SQLite on every
    #    engine, so this must run for local and hosted alike).
    derived = result["report"]["recompute"]["derived_intelligence"]
    assert isinstance(derived, dict)
    assert str(derived.get("status", "")) not in {"skipped", "disabled"}, derived
    assert derived.get("source_id") == SCRUBBED
