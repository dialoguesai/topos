"""Guard tests for the control-plane message handler registry.

The registry replaces the old linear if-chain dispatch. These tests make
message-type drift visible: adding or removing a handler requires updating
the snapshot below, and unknown types return an error naming the type.
"""
from __future__ import annotations

import pytest

from topos.core.handlers import HANDLERS, handle_control_plane_request
from topos.core.handlers.registry import handles

SUPPORTED_MESSAGE_TYPES = [
    "advance_routine_next_run_at",
    "app_ingest",
    "auto_resolve_source_contacts",
    "check_inbox_write",
    "compute_invoke",
    "connection_info",
    "create_routine",
    "create_routine_run",
    "delete_data_explorer_table_prefs",
    "delete_database_rows",
    "delete_database_table",
    "delete_enrichment_lab_job_group",
    "delete_facts_llm_config",
    "delete_filter_lab_all_data",
    "delete_filter_lab_job_group",
    "delete_home_chat_session",
    "delete_jsonl_file",
    "delete_routine",
    "delete_sanitization_ollama_config",
    "delete_signal_extraction_config",
    "delete_source_install",
    "enrichment_catalog",
    "enrichment_coverage",
    "enrichment_preview",
    "enrichment_process_source",
    "enrichment_progress",
    "enrichment_status_source",
    "finish_google_contacts_import",
    "finish_google_contacts_import_global",
    "get_analytics",
    "get_data_explorer_table_prefs",
    "get_database_explorer_summary",
    "get_device_info",
    "get_enrichment_lab_bundle_detail",
    "get_enrichment_lab_bundles",
    "get_enrichment_lab_job_group_detail",
    "get_enrichment_lab_model_resolve",
    "get_enrichment_lab_node_sample",
    "get_exposure_profile_config",
    "get_facts_llm_config",
    "get_filter_lab_bundle_detail",
    "get_filter_lab_bundles",
    "get_filter_lab_job_group_detail",
    "get_home_chat_session",
    "get_ingestion_audit",
    "get_ingestion_datasets",
    "get_messages",
    "get_oplog",
    "get_request_counts",
    "get_routine",
    "get_routine_by_id",
    "get_routine_run",
    "get_routine_run_by_id",
    "get_runtime_bootstrap",
    "get_sanitization_ollama_config",
    "get_signal_extraction_config",
    "get_signal_settings",
    "get_source_contacts",
    "get_source_install_status",
    "get_source_settings",
    "get_sources",
    "get_sources_overview",
    "get_table_count",
    "get_table_rows",
    "get_table_schema",
    "get_ui_config",
    "get_upgrade_status",
    "get_user_identity",
    "healthcheck",
    "import_apple_contacts",
    "import_contacts_apple_global",
    "import_google_contacts_token",
    "import_google_contacts_token_global",
    "ingestion_reprocess",
    "list_database_tables",
    "list_due_routines",
    "list_enrichment_lab_job_groups",
    "list_filter_lab_job_groups",
    "list_home_chat_sessions",
    "list_jsonl_files",
    "list_routine_runs",
    "list_routines",
    "list_waiting_routine_runs",
    "llm_generation",
    "llm_integrations_storage",
    "messenger_analytics_communities",
    "messenger_analytics_graph",
    "messenger_analytics_importance",
    "messenger_analytics_periods",
    "messenger_analytics_recompute",
    "messenger_analytics_sources",
    "messenger_contact_graph",
    "migrate_browser_plugin_app_id",
    "ollama_list_models",
    "patch_enrichment_lab_job_group",
    "patch_enrichment_lab_job_run",
    "patch_filter_lab_job_group",
    "patch_filter_lab_job_run",
    "patch_routine",
    "patch_source_install",
    "pooled_scope_backfill_apply",
    "pooled_scope_backfill_dry_run",
    "pooled_scope_backfill_rollback",
    "post_enrichment_lab_apply_preferred",
    "post_enrichment_lab_job_group",
    "post_filter_lab_apply_preferred",
    "post_filter_lab_job_group",
    "post_source_install",
    "post_source_scrub",
    "post_source_test_enrichment",
    "post_source_test_enrichment_trigger",
    "post_source_test_ingestion",
    "put_data_explorer_table_prefs",
    "put_exposure_profile_config",
    "put_fact_verdict",
    "put_facts_llm_config",
    "put_sanitization_ollama_config",
    "put_signal_extraction_config",
    "put_signal_settings",
    "put_source_contact",
    "put_source_settings",
    "put_ui_config",
    "put_user_identity",
    "query",
    "query_live",
    "read_jsonl_file",
    "replay_projection",
    "replay_projection_preview",
    "routine_has_active_run",
    "set_device_name",
    "signal_data_health",
    "signal_entity_graph",
    "signal_entity_graph_search",
    "signal_entity_merge",
    "signal_entity_review_action",
    "signal_entity_review_sweep",
    "signal_entity_split",
    "signal_evaluate_fit",
    "signal_exclude_entity",
    "signal_get_brief",
    "signal_get_definition",
    "signal_get_entity",
    "signal_get_object",
    "signal_list_brief_revisions",
    "signal_list_briefs",
    "signal_list_definitions",
    "signal_list_dimensions",
    "signal_list_entities",
    "signal_list_entity_review",
    "signal_list_facts",
    "signal_list_graph",
    "signal_list_insights",
    "signal_list_objects",
    "signal_list_timeline",
    "signal_list_topic_cluster_members",
    "signal_list_topic_clusters",
    "signal_list_vectors",
    "signal_owner_override_object",
    "signal_refresh_brief",
    "signal_search_vectors",
    "signal_update_brief",
    "signal_upload",
    "signal_vector_source_text",
    "source_enrichment_backfill",
    "source_enrichment_delete",
    "source_enrichment_test",
    "source_enrichment_toggle",
    "source_enrichments_list",
    "source_sync",
    "start_google_contacts_import",
    "start_google_contacts_import_global",
    "start_ingestion",
    "store_message",
    "tools_index",
    "tools_index_status",
    "tools_retrieve",
    "uma_get_messages",
    "uma_get_oplog",
    "uma_get_rows",
    "update_routine_run",
    "upsert_home_chat_session",
]


def test_registry_matches_supported_message_type_snapshot():
    registered = sorted(HANDLERS.keys())
    assert registered == SUPPORTED_MESSAGE_TYPES, (
        "Handler registry drifted from the supported message type snapshot. "
        "If you added or removed a handler intentionally, update "
        "SUPPORTED_MESSAGE_TYPES in this test."
    )


def test_duplicate_registration_rejected():
    with pytest.raises(ValueError, match="duplicate handler registration"):

        @handles("healthcheck")
        async def _dup(message):
            return None


@pytest.mark.asyncio
async def test_unknown_message_type_returns_error_naming_type():
    result = await handle_control_plane_request(
        {"id": "req-1", "type": "definitely_not_a_real_type", "payload": {}}
    )
    assert result == {
        "id": "req-1",
        "status": "error",
        "error": "unhandled message type: definitely_not_a_real_type",
    }


@pytest.mark.asyncio
async def test_known_type_dispatches():
    result = await handle_control_plane_request({"id": "req-2", "type": "healthcheck"})
    assert result == {"id": "req-2", "status": "ok", "payload": {"status": "ok"}}
