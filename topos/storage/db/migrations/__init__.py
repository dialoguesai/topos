"""Wiki MVP storage migration runner."""

from __future__ import annotations

import sqlite3

from .wiki_mvp_phase0 import MIGRATION_ID as PHASE0_ID, apply_wiki_mvp_phase0_up
from .wiki_mvp_phase1 import MIGRATION_ID as PHASE1_ID, apply_wiki_mvp_phase1_up
from .wiki_mvp_phase4_messages_cutover import (
    MIGRATION_ID as PHASE4_ID,
    apply_wiki_mvp_phase4_messages_cutover_up,
)
from .wiki_mvp_phase5_topic_clusters import (
    MIGRATION_ID as PHASE5_ID,
    apply_wiki_mvp_phase5_topic_clusters_up,
)
from .wiki_mvp_query_quality import (
    MIGRATION_ID as QUERY_QUALITY_ID,
    apply_wiki_mvp_query_quality_up,
)
from .remediation_person_model import (
    MIGRATION_ID as PERSON_MODEL_ID,
    apply_remediation_person_model_up,
)

__all__ = ["apply_all_migrations", "ensure_migrations_applied"]


def _migration_applied(conn: sqlite3.Connection, migration_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
            (migration_id,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def apply_all_migrations(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase0_up(conn)
    apply_wiki_mvp_phase1_up(conn)
    apply_wiki_mvp_phase4_messages_cutover_up(conn)
    apply_wiki_mvp_phase5_topic_clusters_up(conn)
    apply_wiki_mvp_query_quality_up(conn)
    apply_remediation_person_model_up(conn)


def ensure_migrations_applied(conn: sqlite3.Connection) -> None:
    """Apply wiki MVP migrations; phase1 always re-runs idempotent ALTERs after legacy DDL."""
    if not _migration_applied(conn, PHASE0_ID):
        apply_wiki_mvp_phase0_up(conn)
    # Provenance column adds are cheap and must run after legacy _ensure_tables DDL.
    apply_wiki_mvp_phase1_up(conn)
    if not _migration_applied(conn, PHASE4_ID):
        apply_wiki_mvp_phase4_messages_cutover_up(conn)
    if not _migration_applied(conn, PHASE5_ID):
        apply_wiki_mvp_phase5_topic_clusters_up(conn)
    apply_wiki_mvp_query_quality_up(conn)
    if not _migration_applied(conn, PERSON_MODEL_ID):
        apply_remediation_person_model_up(conn)
