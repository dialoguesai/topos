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
from .wiki_mvp_phase6_dimension_briefs import (
    MIGRATION_ID as PHASE6_ID,
    apply_wiki_mvp_phase6_dimension_briefs_up,
)
from .remediation_person_model import (
    MIGRATION_ID as PERSON_MODEL_ID,
    apply_remediation_person_model_up,
)
from .signal_dimension_harness import (
    MIGRATION_ID as HARNESS_ID,
    apply_signal_dimension_harness_up,
)
from .scope_source_generation import (
    MIGRATION_ID as SOURCE_GENERATION_ID,
    apply_scope_source_generation_up,
)
from .wiki_mvp_topic_clusters_coordination import (
    MIGRATION_ID as TOPIC_CLUSTERS_COORDINATION_ID,
    apply_wiki_mvp_topic_clusters_coordination_up,
)
from .vector_storage_v1 import MIGRATION_ID as VECTOR_STORAGE_V1_ID, apply_vector_storage_v1_up
from .vector_storage_v2 import MIGRATION_ID as VECTOR_STORAGE_V2_ID, apply_vector_storage_v2_up
from .vector_storage_v3 import MIGRATION_ID as VECTOR_STORAGE_V3_ID, apply_vector_storage_v3_up
from .canonical_disclosure_v1 import (
    MIGRATION_ID as CANONICAL_DISCLOSURE_V1_ID,
    apply_canonical_disclosure_v1_up,
)
from .canonical_nsfw_v1 import (
    MIGRATION_ID as CANONICAL_NSFW_V1_ID,
    apply_canonical_nsfw_v1_up,
)
from .signal_objects import MIGRATION_ID as SIGNAL_OBJECTS_ID, apply_signal_objects_up
from .extraction_artifacts import (
    MIGRATION_ID as EXTRACTION_ARTIFACTS_ID,
    apply_extraction_artifacts_up,
)
from .journal_entries_people_v1 import (
    MIGRATION_ID as JOURNAL_PEOPLE_ID,
    apply_journal_entries_people_v1_up,
)
from .journal_entries_place_name_v1 import (
    MIGRATION_ID as JOURNAL_PLACE_NAME_ID,
    apply_journal_entries_place_name_v1_up,
)
from .journal_entries_duration_v1 import (
    MIGRATION_ID as JOURNAL_DURATION_ID,
    apply_journal_entries_duration_v1_up,
)
from .journal_entries_ends_at_v1 import (
    MIGRATION_ID as JOURNAL_ENDS_AT_ID,
    apply_journal_entries_ends_at_v1_up,
)
from .journal_entries_starts_at_v1 import (
    MIGRATION_ID as JOURNAL_STARTS_AT_ID,
    apply_journal_entries_starts_at_v1_up,
)
from .signal_objects_updated_by_v1 import (
    MIGRATION_ID as SIGNAL_OBJECTS_UPDATED_BY_ID,
    apply_signal_objects_updated_by_v1_up,
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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def apply_all_migrations(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase0_up(conn)
    apply_wiki_mvp_phase1_up(conn)
    apply_wiki_mvp_phase4_messages_cutover_up(conn)
    apply_wiki_mvp_phase5_topic_clusters_up(conn)
    apply_wiki_mvp_phase6_dimension_briefs_up(conn)
    apply_wiki_mvp_query_quality_up(conn)
    apply_remediation_person_model_up(conn)
    apply_signal_dimension_harness_up(conn)
    apply_scope_source_generation_up(conn)
    apply_wiki_mvp_topic_clusters_coordination_up(conn)
    apply_vector_storage_v1_up(conn)
    apply_vector_storage_v2_up(conn)
    apply_vector_storage_v3_up(conn)
    apply_canonical_disclosure_v1_up(conn)
    apply_canonical_nsfw_v1_up(conn)
    apply_signal_objects_up(conn)
    apply_extraction_artifacts_up(conn)
    apply_journal_entries_people_v1_up(conn)
    apply_journal_entries_place_name_v1_up(conn)
    apply_journal_entries_duration_v1_up(conn)
    apply_journal_entries_ends_at_v1_up(conn)
    apply_journal_entries_starts_at_v1_up(conn)
    apply_signal_objects_updated_by_v1_up(conn)


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
    if not _migration_applied(conn, PHASE6_ID):
        apply_wiki_mvp_phase6_dimension_briefs_up(conn)
    apply_wiki_mvp_query_quality_up(conn)
    if not _migration_applied(conn, PERSON_MODEL_ID):
        apply_remediation_person_model_up(conn)
    if not _migration_applied(conn, HARNESS_ID) or not _table_exists(conn, "journal_entries"):
        apply_signal_dimension_harness_up(conn)
    apply_wiki_mvp_topic_clusters_coordination_up(conn)
    if not _migration_applied(conn, SOURCE_GENERATION_ID):
        apply_scope_source_generation_up(conn)
    if not _migration_applied(conn, VECTOR_STORAGE_V1_ID):
        apply_vector_storage_v1_up(conn)
    if not _migration_applied(conn, VECTOR_STORAGE_V2_ID):
        apply_vector_storage_v2_up(conn)
    if not _migration_applied(conn, VECTOR_STORAGE_V3_ID):
        apply_vector_storage_v3_up(conn)
    if not _migration_applied(conn, CANONICAL_DISCLOSURE_V1_ID):
        apply_canonical_disclosure_v1_up(conn)
    if not _migration_applied(conn, CANONICAL_NSFW_V1_ID):
        apply_canonical_nsfw_v1_up(conn)
    if not _migration_applied(conn, SIGNAL_OBJECTS_ID):
        apply_signal_objects_up(conn)
    if not _migration_applied(conn, EXTRACTION_ARTIFACTS_ID):
        apply_extraction_artifacts_up(conn)
    apply_journal_entries_people_v1_up(conn)
    apply_journal_entries_place_name_v1_up(conn)
    apply_journal_entries_duration_v1_up(conn)
    apply_journal_entries_ends_at_v1_up(conn)
    apply_journal_entries_starts_at_v1_up(conn)
    apply_signal_objects_updated_by_v1_up(conn)
