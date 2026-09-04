"""Single ordered migration registry (PLAN_NODE_RELEASE_MIGRATIONS §4a).

``apply_all_migrations`` and ``ensure_migrations_applied`` both iterate
``MIGRATIONS``. Ledger-guarded steps run once; ``always_run`` steps re-apply
cheap idempotent ALTERs after legacy DDL on every boot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

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
from .journal_origin_dimension_v1 import (
    MIGRATION_ID as JOURNAL_ORIGIN_DIMENSION_V1_ID,
    apply_journal_origin_dimension_v1_up,
)
from .community_names_v1 import (
    MIGRATION_ID as COMMUNITY_NAMES_V1_ID,
    apply_community_names_v1_up,
)
from .conflict_provenance_v1 import (
    MIGRATION_ID as CONFLICT_PROVENANCE_V1_ID,
    apply_conflict_provenance_v1_up,
)
from .derivation_factory_v1 import (
    MIGRATION_ID as DERIVATION_FACTORY_V1_ID,
    apply_derivation_factory_v1_up,
)
from .net_subject_policy_v1 import (
    MIGRATION_ID as NET_SUBJECT_POLICY_V1_ID,
    apply_net_subject_policy_v1_up,
)
from .pack_registry_v1 import (
    MIGRATION_ID as PACK_REGISTRY_V1_ID,
    apply_pack_registry_v1_up,
)
from .derivation_provenance_v1 import (
    MIGRATION_ID as DERIVATION_PROVENANCE_V1_ID,
    apply_derivation_provenance_v1_up,
)
from .enrichment_record_progress_v1 import (
    MIGRATION_ID as ENRICHMENT_RECORD_PROGRESS_V1_ID,
    apply_enrichment_record_progress_v1_up,
)
from .query_artifacts_duration_ms_v1 import (
    MIGRATION_ID as QUERY_ARTIFACTS_DURATION_MS_V1_ID,
    apply_query_artifacts_duration_ms_v1_up,
)
from .derived_row_identity_v1 import (
    MIGRATION_ID as DERIVED_ROW_IDENTITY_V1_ID,
)
from .derived_row_identity_v1 import (
    apply_derived_row_identity_v1_up,
)
from .mention_table_stamp_backfill_v1 import (
    MIGRATION_ID as MENTION_TABLE_STAMP_BACKFILL_V1_ID,
)
from .mention_table_stamp_backfill_v1 import (
    apply_mention_table_stamp_backfill_v1_up,
)
from .fact_dimension_by_entity_type_v1 import (
    MIGRATION_ID as FACT_DIMENSION_BY_ENTITY_TYPE_V1_ID,
)
from .fact_dimension_by_entity_type_v1 import (
    apply_fact_dimension_by_entity_type_v1_up,
)
from .signal_dimension_backfill_v1 import (
    MIGRATION_ID as SIGNAL_DIMENSION_BACKFILL_V1_ID,
    apply_signal_dimension_backfill_v1_up,
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
from .vector_storage_v4 import MIGRATION_ID as VECTOR_STORAGE_V4_ID, apply_vector_storage_v4_up
from .wiki_stats_v1 import MIGRATION_ID as WIKI_STATS_V1_ID, apply_wiki_stats_v1_up
from .wiki_entities_v1 import MIGRATION_ID as WIKI_ENTITIES_V1_ID, apply_wiki_entities_v1_up
from .wiki_facts_v1 import MIGRATION_ID as WIKI_FACTS_V1_ID, apply_wiki_facts_v1_up
from .wiki_clusters_v2 import MIGRATION_ID as WIKI_CLUSTERS_V2_ID, apply_wiki_clusters_v2_up
from .wiki_timeline_v1 import MIGRATION_ID as WIKI_TIMELINE_V1_ID, apply_wiki_timeline_v1_up
from .wiki_lifecycle_v1 import MIGRATION_ID as WIKI_LIFECYCLE_V1_ID, apply_wiki_lifecycle_v1_up
from .wiki_entity_review_v2 import MIGRATION_ID as WIKI_ENTITY_REVIEW_V2_ID, apply_wiki_entity_review_v2_up
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
from .signal_objects_period_v1 import (
    MIGRATION_ID as SIGNAL_OBJECTS_PERIOD_V1_ID,
    apply_signal_objects_period_v1_up,
)
from .entity_edges_validity_v1 import (
    MIGRATION_ID as ENTITY_EDGES_VALIDITY_V1_ID,
    apply_entity_edges_validity_v1_up,
)
from .episodes_v1 import (
    MIGRATION_ID as EPISODES_V1_ID,
    apply_episodes_v1_up,
)
from .actor_role_v1 import (
    MIGRATION_ID as ACTOR_ROLE_V1_ID,
    apply_actor_role_v1_up,
)
from .conversation_context_v1 import (
    MIGRATION_ID as CONVERSATION_CONTEXT_V1_ID,
    apply_conversation_context_v1_up,
)
from .activity_events_content_v1 import (
    MIGRATION_ID as ACTIVITY_EVENTS_CONTENT_V1_ID,
    apply_activity_events_content_v1_up,
)
from .derivation_ledger_v1 import (
    MIGRATION_ID as DERIVATION_LEDGER_V1_ID,
    apply_derivation_ledger_v1_up,
)
from .pipeline_jobs_v1 import (
    MIGRATION_ID as PIPELINE_JOBS_V1_ID,
    apply_pipeline_jobs_v1_up,
)
from .canonical_address_book_v1 import (
    MIGRATION_ID as CANONICAL_ADDRESS_BOOK_V1_ID,
    apply_canonical_address_book_v1_up,
)
from .documents_v1 import (
    MIGRATION_ID as DOCUMENTS_V1_ID,
    apply_documents_v1_up,
)
from .transcripts_v1 import (
    MIGRATION_ID as TRANSCRIPTS_V1_ID,
    apply_transcripts_v1_up,
)
from .calendar_events_scheduling_v1 import (
    MIGRATION_ID as CALENDAR_EVENTS_SCHEDULING_V1_ID,
    apply_calendar_events_scheduling_v1_up,
)
from .attention_triage_v1 import (
    MIGRATION_ID as ATTENTION_TRIAGE_V1_ID,
    apply_attention_triage_v1_up,
)
from .attention_triage_v2 import (
    MIGRATION_ID as ATTENTION_TRIAGE_V2_ID,
    apply_attention_triage_v2_up,
)
from .entity_blackhole_v1 import (
    MIGRATION_ID as ENTITY_BLACKHOLE_V1_ID,
    apply_entity_blackhole_v1_up,
)
from .complexity_v1 import (
    MIGRATION_ID as COMPLEXITY_V1_ID,
    apply_complexity_v1_up,
)
from .enrichment_spec_version_v1 import (
    MIGRATION_ID as ENRICHMENT_SPEC_VERSION_V1_ID,
    apply_enrichment_spec_version_v1_up,
)
from .entity_context_vectors_v1 import (
    MIGRATION_ID as ENTITY_CONTEXT_VECTORS_V1_ID,
    apply_entity_context_vectors_v1_up,
)
from .entity_affinity_v1 import (
    MIGRATION_ID as ENTITY_AFFINITY_V1_ID,
    apply_entity_affinity_v1_up,
)
from .entity_context_vectors_v2 import (
    MIGRATION_ID as ENTITY_CONTEXT_VECTORS_V2_ID,
    apply_entity_context_vectors_v2_up,
)
from .affinity_pair_labels_v1 import (
    MIGRATION_ID as AFFINITY_PAIR_LABELS_V1_ID,
    apply_affinity_pair_labels_v1_up,
)
from .message_emotions_role_v1 import (
    MIGRATION_ID as MESSAGE_EMOTIONS_ROLE_V1_ID,
    apply_message_emotions_role_v1_up,
)
from .signal_summaries_drop_v1 import (
    MIGRATION_ID as SIGNAL_SUMMARIES_DROP_V1_ID,
    apply_signal_summaries_drop_v1_up,
)
from .message_emotions_role_backfill_v1 import (
    MIGRATION_ID as MESSAGE_EMOTIONS_ROLE_BACKFILL_V1_ID,
    apply_message_emotions_role_backfill_v1_up,
)
from .entity_mentions_authored_v1 import (
    MIGRATION_ID as ENTITY_MENTIONS_AUTHORED_V1_ID,
    apply_entity_mentions_authored_v1_up,
)
from .entity_review_dedup_v1 import (
    MIGRATION_ID as ENTITY_REVIEW_DEDUP_V1_ID,
    apply_entity_review_dedup_v1_up,
)
from .entity_review_provenance_v1 import (
    MIGRATION_ID as ENTITY_REVIEW_PROVENANCE_V1_ID,
    apply_entity_review_provenance_v1_up,
)
from .retire_url_classification_v1 import (
    MIGRATION_ID as RETIRE_URL_CLASSIFICATION_V1_ID,
    apply_retire_url_classification_v1_up,
)
from .entity_review_candidate_bar_v1 import (
    MIGRATION_ID as ENTITY_REVIEW_CANDIDATE_BAR_V1_ID,
    apply_entity_review_candidate_bar_v1_up,
)

ApplyFn = Callable[[sqlite3.Connection], object]


@dataclass(frozen=True)
class MigrationSpec:
    """One schema-shape migration in the append-only registry."""

    order: int
    id: str
    fn: ApplyFn
    always_run: bool = False
    # When set, also run if this table is missing (harness bootstrap).
    also_if_missing_table: Optional[str] = None


def _spec(
    order: int,
    migration_id: str,
    fn: ApplyFn,
    *,
    always_run: bool = False,
    also_if_missing_table: Optional[str] = None,
) -> MigrationSpec:
    return MigrationSpec(
        order=order,
        id=migration_id,
        fn=fn,
        always_run=always_run,
        also_if_missing_table=also_if_missing_table,
    )


# Append-only. Never rename, reorder, or delete a shipped id — fixes are new entries.
# ``always_run`` matches the historical ensure_migrations_applied re-run set.
MIGRATIONS: List[MigrationSpec] = [
    _spec(1, PHASE0_ID, apply_wiki_mvp_phase0_up),
    _spec(2, PHASE1_ID, apply_wiki_mvp_phase1_up, always_run=True),
    _spec(3, PHASE4_ID, apply_wiki_mvp_phase4_messages_cutover_up),
    _spec(4, PHASE5_ID, apply_wiki_mvp_phase5_topic_clusters_up),
    _spec(5, PHASE6_ID, apply_wiki_mvp_phase6_dimension_briefs_up),
    _spec(6, QUERY_QUALITY_ID, apply_wiki_mvp_query_quality_up, always_run=True),
    _spec(7, PERSON_MODEL_ID, apply_remediation_person_model_up),
    _spec(
        8,
        HARNESS_ID,
        apply_signal_dimension_harness_up,
        also_if_missing_table="journal_entries",
    ),
    _spec(9, SOURCE_GENERATION_ID, apply_scope_source_generation_up),
    _spec(
        10,
        TOPIC_CLUSTERS_COORDINATION_ID,
        apply_wiki_mvp_topic_clusters_coordination_up,
        always_run=True,
    ),
    _spec(11, VECTOR_STORAGE_V1_ID, apply_vector_storage_v1_up),
    _spec(12, VECTOR_STORAGE_V2_ID, apply_vector_storage_v2_up),
    _spec(13, VECTOR_STORAGE_V3_ID, apply_vector_storage_v3_up),
    _spec(14, VECTOR_STORAGE_V4_ID, apply_vector_storage_v4_up, always_run=True),
    _spec(15, WIKI_STATS_V1_ID, apply_wiki_stats_v1_up),
    _spec(16, WIKI_ENTITIES_V1_ID, apply_wiki_entities_v1_up),
    _spec(17, WIKI_FACTS_V1_ID, apply_wiki_facts_v1_up),
    _spec(18, WIKI_CLUSTERS_V2_ID, apply_wiki_clusters_v2_up),
    _spec(19, WIKI_TIMELINE_V1_ID, apply_wiki_timeline_v1_up),
    _spec(20, WIKI_LIFECYCLE_V1_ID, apply_wiki_lifecycle_v1_up),
    _spec(21, WIKI_ENTITY_REVIEW_V2_ID, apply_wiki_entity_review_v2_up),
    _spec(22, CANONICAL_DISCLOSURE_V1_ID, apply_canonical_disclosure_v1_up, always_run=True),
    _spec(23, CANONICAL_NSFW_V1_ID, apply_canonical_nsfw_v1_up),
    _spec(24, SIGNAL_OBJECTS_ID, apply_signal_objects_up),
    _spec(25, EXTRACTION_ARTIFACTS_ID, apply_extraction_artifacts_up),
    _spec(26, JOURNAL_PEOPLE_ID, apply_journal_entries_people_v1_up, always_run=True),
    _spec(27, JOURNAL_PLACE_NAME_ID, apply_journal_entries_place_name_v1_up, always_run=True),
    _spec(28, JOURNAL_DURATION_ID, apply_journal_entries_duration_v1_up, always_run=True),
    _spec(29, JOURNAL_ENDS_AT_ID, apply_journal_entries_ends_at_v1_up, always_run=True),
    _spec(30, JOURNAL_STARTS_AT_ID, apply_journal_entries_starts_at_v1_up, always_run=True),
    _spec(31, SIGNAL_OBJECTS_UPDATED_BY_ID, apply_signal_objects_updated_by_v1_up, always_run=True),
    _spec(32, SIGNAL_DIMENSION_BACKFILL_V1_ID, apply_signal_dimension_backfill_v1_up),
    _spec(33, SIGNAL_OBJECTS_PERIOD_V1_ID, apply_signal_objects_period_v1_up, always_run=True),
    _spec(34, ENTITY_EDGES_VALIDITY_V1_ID, apply_entity_edges_validity_v1_up, always_run=True),
    _spec(35, EPISODES_V1_ID, apply_episodes_v1_up),
    _spec(36, ACTOR_ROLE_V1_ID, apply_actor_role_v1_up, always_run=True),
    _spec(37, CONVERSATION_CONTEXT_V1_ID, apply_conversation_context_v1_up, always_run=True),
    _spec(38, ACTIVITY_EVENTS_CONTENT_V1_ID, apply_activity_events_content_v1_up, always_run=True),
    _spec(39, DERIVATION_LEDGER_V1_ID, apply_derivation_ledger_v1_up, always_run=True),
    _spec(40, PIPELINE_JOBS_V1_ID, apply_pipeline_jobs_v1_up, always_run=True),
    _spec(41, CANONICAL_ADDRESS_BOOK_V1_ID, apply_canonical_address_book_v1_up, always_run=True),
    _spec(42, DOCUMENTS_V1_ID, apply_documents_v1_up),
    _spec(
        43,
        CALENDAR_EVENTS_SCHEDULING_V1_ID,
        apply_calendar_events_scheduling_v1_up,
        always_run=True,
    ),
    _spec(44, ATTENTION_TRIAGE_V1_ID, apply_attention_triage_v1_up),
    _spec(45, ATTENTION_TRIAGE_V2_ID, apply_attention_triage_v2_up, always_run=True),
    _spec(46, ENTITY_BLACKHOLE_V1_ID, apply_entity_blackhole_v1_up),
    _spec(47, COMPLEXITY_V1_ID, apply_complexity_v1_up),
    _spec(48, ENRICHMENT_SPEC_VERSION_V1_ID, apply_enrichment_spec_version_v1_up),
    _spec(49, ENTITY_CONTEXT_VECTORS_V1_ID, apply_entity_context_vectors_v1_up),
    _spec(50, ENTITY_AFFINITY_V1_ID, apply_entity_affinity_v1_up),
    _spec(51, ENTITY_CONTEXT_VECTORS_V2_ID, apply_entity_context_vectors_v2_up),
    _spec(52, AFFINITY_PAIR_LABELS_V1_ID, apply_affinity_pair_labels_v1_up),
    _spec(53, MESSAGE_EMOTIONS_ROLE_V1_ID, apply_message_emotions_role_v1_up, always_run=True),
    _spec(54, SIGNAL_SUMMARIES_DROP_V1_ID, apply_signal_summaries_drop_v1_up),
    _spec(55, MESSAGE_EMOTIONS_ROLE_BACKFILL_V1_ID, apply_message_emotions_role_backfill_v1_up),
    _spec(
        56,
        ENTITY_MENTIONS_AUTHORED_V1_ID,
        apply_entity_mentions_authored_v1_up,
        always_run=True,
    ),
    _spec(57, ENTITY_REVIEW_DEDUP_V1_ID, apply_entity_review_dedup_v1_up),
    _spec(58, ENTITY_REVIEW_PROVENANCE_V1_ID, apply_entity_review_provenance_v1_up),
    _spec(59, ENTITY_REVIEW_CANDIDATE_BAR_V1_ID, apply_entity_review_candidate_bar_v1_up),
    _spec(60, RETIRE_URL_CLASSIFICATION_V1_ID, apply_retire_url_classification_v1_up),
    _spec(61, JOURNAL_ORIGIN_DIMENSION_V1_ID, apply_journal_origin_dimension_v1_up),
    _spec(62, QUERY_ARTIFACTS_DURATION_MS_V1_ID, apply_query_artifacts_duration_ms_v1_up),
    # 63 was written first and held back from the repo head on 2026-08-25 (see the module
    # docstring): registering it early stamped the live database past its installed engine
    # and cost ~25 minutes of ingest. A release ships it, which is what this is.
    _spec(63, ENRICHMENT_RECORD_PROGRESS_V1_ID, apply_enrichment_record_progress_v1_up),
    _spec(64, DERIVATION_PROVENANCE_V1_ID, apply_derivation_provenance_v1_up),
    _spec(65, PACK_REGISTRY_V1_ID, apply_pack_registry_v1_up),
    _spec(66, DERIVATION_FACTORY_V1_ID, apply_derivation_factory_v1_up),
    _spec(67, COMMUNITY_NAMES_V1_ID, apply_community_names_v1_up),
    _spec(68, CONFLICT_PROVENANCE_V1_ID, apply_conflict_provenance_v1_up),
    # 69 shipped the same way 63 did — written and held out of the head so it could not
    # stamp a live database past its installed engine, then landed at a release cut. The
    # table holds OVERRIDES to the net-subject rule, so its absence was never a denial;
    # what the owner could not do until now is record an explicit `deny` for someone the
    # node can name, for which the only lever was the blackhole.
    _spec(69, NET_SUBJECT_POLICY_V1_ID, apply_net_subject_policy_v1_up),
    _spec(70, FACT_DIMENSION_BY_ENTITY_TYPE_V1_ID, apply_fact_dimension_by_entity_type_v1_up),
    _spec(71, MENTION_TABLE_STAMP_BACKFILL_V1_ID, apply_mention_table_stamp_backfill_v1_up),
    _spec(72, DERIVED_ROW_IDENTITY_V1_ID, apply_derived_row_identity_v1_up),
    _spec(73, TRANSCRIPTS_V1_ID, apply_transcripts_v1_up),
]


def max_migration_order() -> int:
    return max(m.order for m in MIGRATIONS)


# Re-export IDs commonly imported by tests/tools.
__all__ = [
    "MigrationSpec",
    "MIGRATIONS",
    "max_migration_order",
    "PHASE0_ID",
    "PHASE1_ID",
    "PHASE4_ID",
    "PHASE5_ID",
    "PHASE6_ID",
    "QUERY_QUALITY_ID",
    "PERSON_MODEL_ID",
    "HARNESS_ID",
    "SOURCE_GENERATION_ID",
    "TOPIC_CLUSTERS_COORDINATION_ID",
    "VECTOR_STORAGE_V1_ID",
    "VECTOR_STORAGE_V2_ID",
    "VECTOR_STORAGE_V3_ID",
    "VECTOR_STORAGE_V4_ID",
    "WIKI_STATS_V1_ID",
    "WIKI_ENTITIES_V1_ID",
    "WIKI_FACTS_V1_ID",
    "WIKI_CLUSTERS_V2_ID",
    "WIKI_TIMELINE_V1_ID",
    "WIKI_LIFECYCLE_V1_ID",
    "WIKI_ENTITY_REVIEW_V2_ID",
    "CANONICAL_DISCLOSURE_V1_ID",
    "CANONICAL_NSFW_V1_ID",
    "SIGNAL_OBJECTS_ID",
    "EXTRACTION_ARTIFACTS_ID",
    "JOURNAL_PEOPLE_ID",
    "JOURNAL_PLACE_NAME_ID",
    "JOURNAL_DURATION_ID",
    "JOURNAL_ENDS_AT_ID",
    "JOURNAL_STARTS_AT_ID",
    "SIGNAL_OBJECTS_UPDATED_BY_ID",
    "SIGNAL_DIMENSION_BACKFILL_V1_ID",
    "SIGNAL_OBJECTS_PERIOD_V1_ID",
    "ENTITY_EDGES_VALIDITY_V1_ID",
    "EPISODES_V1_ID",
    "ACTOR_ROLE_V1_ID",
    "CONVERSATION_CONTEXT_V1_ID",
    "ACTIVITY_EVENTS_CONTENT_V1_ID",
    "DERIVATION_LEDGER_V1_ID",
    "ENRICHMENT_RECORD_PROGRESS_V1_ID",
    "DERIVATION_PROVENANCE_V1_ID",
    "PIPELINE_JOBS_V1_ID",
    "CANONICAL_ADDRESS_BOOK_V1_ID",
    "DOCUMENTS_V1_ID",
    "TRANSCRIPTS_V1_ID",
    "CALENDAR_EVENTS_SCHEDULING_V1_ID",
    "ATTENTION_TRIAGE_V1_ID",
    "ATTENTION_TRIAGE_V2_ID",
    "ENTITY_BLACKHOLE_V1_ID",
    "COMPLEXITY_V1_ID",
    "ENRICHMENT_SPEC_VERSION_V1_ID",
    "ENTITY_CONTEXT_VECTORS_V1_ID",
    "ENTITY_AFFINITY_V1_ID",
    "ENTITY_CONTEXT_VECTORS_V2_ID",
    "ENTITY_REVIEW_DEDUP_V1_ID",
    "ENTITY_REVIEW_PROVENANCE_V1_ID",
    "ENTITY_REVIEW_CANDIDATE_BAR_V1_ID",
    "JOURNAL_ORIGIN_DIMENSION_V1_ID",
    "RETIRE_URL_CLASSIFICATION_V1_ID",
]
