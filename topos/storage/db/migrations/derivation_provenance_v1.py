"""Provenance columns on ``signal_objects`` for ontology-pack derived facts.

A fact written by the derivation layer has to say WHICH ontology pack asserted it,
at which pack version, and at what epistemic altitude (``stated`` from one record,
``inferred`` from accumulated evidence, ``predicted`` as a hypothesis). Those three
answers drive real behaviour, not display: a pack version bump triggers targeted
re-extraction of only that pack's facts, uninstalling a pack has to find its rows to
freeze them, and per-pack density/junk dashboards group on them.

REAL COLUMNS, not ``metadata_json`` keys (plan decision D6). The read that matters is
"every active fact from pack X" — as JSON that is a full scan of a table this feature
is designed to grow by an order of magnitude, which is exactly the silent-slow-path
class this codebase keeps paying for. The covering index makes it an index scan.

A NEW migration rather than an edit to ``signal_objects``' original: that one is
already applied on every live node, and editing an applied migration in place strands
a database whose ``user_version`` is past it — the columns would simply never appear.

It also replaces an ad-hoc ``ALTER TABLE`` that the derivation writer ran from its own
constructor during the shadow pilot. That shortcut is how a feature silently changes a
live schema without the migration ledger or ``user_version`` knowing: the same class of
failure that stamped a live database to 63 ahead of its installed engine and cost ~25
minutes of ingest on 2026-08-25 (see ``enrichment_record_progress_v1``). Schema shape
belongs to the registry; features read it, they do not create it.

All three columns are nullable TEXT. Nullable is the honest default: every fact written
before the derivation layer — the whole legacy rules/LLM corpus — genuinely has no pack
and no declared altitude, and a placeholder like ``'core'`` or ``'stated'`` would assert
something the extractor never decided.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "derivation_provenance_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not _table_exists(conn, table):
        return
    names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def apply_derivation_provenance_v1_up(conn: sqlite3.Connection) -> None:
    for column in ("ontology_id", "ontology_version", "altitude"):
        _add_column_if_missing(conn, "signal_objects", column, "TEXT")
    if _table_exists(conn, "signal_objects"):
        # The read this serves: "active facts asserted by pack X" — pack dashboards,
        # version-bump re-extraction sweeps, and uninstall freezes. valid_to is in the
        # index because every one of those reads filters active-vs-closed.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_signal_objects_ontology
            ON signal_objects (ontology_id, valid_to)
            """
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_derivation_provenance_v1_down(conn: sqlite3.Connection) -> None:
    # Columns are left in place: SQLite's DROP COLUMN is recent and conditional, and
    # three nullable TEXT columns cost nothing to leave behind. Dropping the index is
    # safe and reversible, so it goes. Only the ledger stamp truly reverses.
    conn.execute("DROP INDEX IF EXISTS idx_signal_objects_ontology")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
