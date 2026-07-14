"""Control-plane message handling.

Handlers are registered per message type in domain modules; dispatch is a
registry lookup. Names are re-exported here for backwards compatibility
(tests monkeypatch e.g. topos.core.handlers.get_db_connection).
"""
from __future__ import annotations

from .common import (  # noqa: F401
    Any,
    Dict,
    HTTPException,
    List,
    MESSENGER_COMMUNITIES_TABLE,
    MESSENGER_PARTICIPANT_IMPORTANCE_TABLE,
    MESSENGER_SOCIAL_EDGES_TABLE,
    Optional,
    REGISTRY,
    RUNTIME_PROFILE_OPERATIONS,
    RawFileStore,
    ScopedTokenValidationError,
    UMAFilterError,
    _TABLE_ROW_CANONICAL_TIME_ORDER,
    _TABLE_ROW_TIME_ORDER_COLUMNS,
    _is_sqlite_conn,
    _normalize_contact_key,
    _resource_owner_for_mcp_log,
    _table_exists,
    _table_row_order_clause,
    _uma_transform_progress_hook,
    apply_filter_manifest,
    apply_filter_manifest_async,
    apply_message_contact_pipeline,
    asyncio,
    avg_message_length,
    base64,
    build_sql_constraints,
    compute_and_persist_messenger_analytics,
    connect_postgres,
    datetime,
    engine_state,
    enrich_contact_rows_with_resolved_display_names,
    enrich_conversation_thread_previews,
    ensure_messenger_analytics_tables,
    extract_field_transforms,
    extract_filter_manifest,
    get_db_connection,
    get_engine_config_value,
    get_limit_cap,
    get_mcp_request_counts,
    get_or_create_user_id,
    get_services,
    get_signal_identity,
    get_source_settings,
    get_uma_request_counts,
    get_user_id,
    hashlib,
    ingest_file_payload,
    ingest_ui_payload,
    json,
    layer_for_category,
    layer_kind_labels,
    load_raw_messages,
    logging,
    messages_by_sender,
    messages_per_day,
    os,
    parse_dataset_id_from_uma_dataset_resource_id,
    put_signal_identity,
    put_source_settings,
    record_mcp_request,
    record_uma_request,
    resolve_file_format,
    resolve_participant_labels,
    resolve_runtime_profile,
    routine_uma_attribution,
    set_engine_config_value,
    settings,
    store_user_id,
    strip_contact_runtime_filters,
    time_module,
    timezone,
    total_messages,
    update_sync_result,
    uuid,
    validate_scoped_invocation_token,
)
from .config import (  # noqa: F401
    ALLOWED_PINNED_WIDGETS,
    MAX_PINNED_WIDGETS,
    UI_CONFIG_KEY,
    _default_ui_config,
    _normalize_ui_config,
)
from .device import (  # noqa: F401
    COMPUTE_ENVELOPE_SCHEMA_VERSION,
    _compute_envelope,
    _operation_to_msg_type,
)
from .messages import (  # noqa: F401
    _query_avg_message_length_db,
    _query_combined_avg_message_length,
    _query_combined_messages_by_sender,
    _query_combined_messages_per_day,
    _query_combined_total_messages,
    _query_messages_by_sender_db,
    _query_messages_per_day_db,
    _query_total_messages_db,
)
from .ingest import (  # noqa: F401
    _download_ingestion_payload,
    _owner_user_id_from_dataset_id,
)
from .database_explorer import (  # noqa: F401
    POOLED_DECLARED_ENDPOINT_POLICY,
    _POOLED_SCOPE_COLUMNS,
    _device_id_for_topos_key,
    _ensure_pooled_scope_journal_table,
    _pooled_endpoint_policy_for_message,
    _pooled_read_enforcement_enabled,
    _pooled_scope_backfill_apply,
    _pooled_scope_backfill_dry_run,
    _pooled_scope_backfill_rollback,
    _pooled_scope_columns_for_table,
    _pooled_scope_missing_predicate,
    _pooled_scope_tables,
    _pooled_table_scope_for_columns,
    _primary_dataset_id_for_engine_context,
    _resolve_table_row_order_clause,
    _safe_sql_identifier,
    _sqlite_query_plan,
)
from .uma import (  # noqa: F401
    _build_uma_scope_clause,
    _resolve_uma_scope,
    _uma_attribution_from_payload,
)
from .messenger_analytics import (  # noqa: F401
    _build_messenger_contact_graph,
    _messenger_source_scope,
    _normalize_messenger_source_filter,
)
from .contacts_import import (  # noqa: F401
    _GOOGLE_CONTACT_IMPORT_SESSIONS,
    _resolve_contact_import_targets,
)
from .common import logger
from .registry import HANDLERS, handles  # noqa: F401
from . import (  # noqa: F401  (imported for handler registration side effects)
    config,
    sources,
    filter_lab,
    enrichment_lab,
    home_chat,
    routines,
    device,
    messages,
    ingest,
    signal_features,
    query,
    database_explorer,
    enrichment,
    tool_index,
    uma,
    messenger_analytics,
    contacts_import,
)


async def handle_control_plane_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    msg_type = str(message.get("type") or "").strip().lower()
    handler = HANDLERS.get(msg_type)
    if handler is None:
        logger.warning("unhandled control plane message type: %r", msg_type)
        return {
            "id": message.get("id"),
            "status": "error",
            "error": f"unhandled message type: {msg_type}",
        }
    return await handler(message)
