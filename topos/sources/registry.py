from __future__ import annotations

from typing import List

from .definitions import DataSourceDefinition
from shared.filtering import FilterInstance, FilterManifest


def _manifest(*filters: FilterInstance) -> dict:
    return FilterManifest(filters=list(filters)).to_storage_dict()


CHATGPT_FILE = DataSourceDefinition(
    source_id="chatgpt_file_ingestion",
    display_name="ChatGPT File Ingestion",
    source_type="file",
    schema_id="chatgpt.conversation.v2",  # Updated to v2 for real ChatGPT data
    parser_id="chatgpt.conversation.v2",  # Updated to v2
    canonical_mapper_id="chatgpt",
    canonical_group_id="ai_messages",
    raw_enrichment_jobs=["attachments", "tool_calls", "language", "time_normalization"],
    canonical_enrichment_jobs=["entities", "topics", "sentiment", "embeddings", "emo_27"],
    analytics_profile_id="chatgpt_dev",
    enrichment_trigger="manual",  # Enrichment skipped during ingestion, trigger via POST /v1/enrichment/process
    ingestion_trigger="manual",  # Ingestion processing waits for manual trigger after upload
    default_scope_id="aiMessages",
    allowed_scope_ids=["aiMessages:read", "aiChat:read"],
    default_filter_hints=["rolling_window_days", "max_rows"],
    filter_tier_kind="sensitivity",
    default_filter_tiers={
        "low": _manifest(FilterInstance(filter_id="rolling_window_days", params={"days": 90})),
        "medium": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 30}),
            FilterInstance(filter_id="max_rows", params={"count": 500}),
        ),
        "high": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 7}),
            FilterInstance(filter_id="max_rows", params={"count": 100}),
        ),
    },
    field_transform_defaults=[
        {"table_id": "ai_chat_messages", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
        {"table_id": "ai_chat_messages", "field": "event_at", "transform_ids": ["timestamp_to_date"]},
    ],
)

CHATGPT_UI = DataSourceDefinition(
    source_id="chatgpt_ui_conversation",
    display_name="ChatGPT UI Conversation",
    source_type="ui_stream",
    schema_id="chatgpt.conversation.v1",
    parser_id="chatgpt.conversation.v1",
    canonical_mapper_id="chatgpt",
    canonical_group_id="ai_messages",
    raw_enrichment_jobs=["attachments", "tool_calls", "language", "time_normalization"],
    canonical_enrichment_jobs=["entities", "topics", "sentiment", "embeddings", "emo_27"],
    analytics_profile_id="chatgpt_dev",
    enrichment_trigger="automatic",  # Enrichment runs automatically during ingestion
    default_scope_id="aiMessages",
    allowed_scope_ids=["aiMessages:read", "aiChat:read"],
    default_filter_hints=["rolling_window_days", "max_rows"],
    filter_tier_kind="sensitivity",
    default_filter_tiers={
        "low": _manifest(FilterInstance(filter_id="rolling_window_days", params={"days": 90})),
        "medium": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 30}),
            FilterInstance(filter_id="max_rows", params={"count": 500}),
        ),
        "high": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 7}),
            FilterInstance(filter_id="max_rows", params={"count": 100}),
        ),
    },
    field_transform_defaults=[
        {"table_id": "ai_chat_messages", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
        {"table_id": "ai_chat_messages", "field": "event_at", "transform_ids": ["timestamp_to_date"]},
    ],
)

# Sprint 3: Browser plugin source
BROWSER_VISITS = DataSourceDefinition(
    source_id="browser_visits",
    display_name="Browser Visits",
    source_type="ui_stream",
    schema_id="browser.visits.v1",
    parser_id="browser.visits.v1",
    canonical_mapper_id=None,  # No canonical mapping for MVP
    canonical_group_id=None,
    raw_enrichment_jobs=["url_classification"],  # Classify URL category during browser ingestion
    canonical_enrichment_jobs=[],
    analytics_profile_id=None,
    enrichment_trigger="manual",  # No automatic enrichment
    ingestion_trigger="automatic",
    default_scope_id="activity",
    allowed_scope_ids=["activity:read", "activity:write"],
    default_filter_hints=["rolling_window_days", "timestamp_to_date", "column_blocklist"],
    filter_tier_kind="inferability",
    default_filter_tiers={
        "low": _manifest(FilterInstance(filter_id="rolling_window_days", params={"days": 30})),
        "medium": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 14}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
        ),
        "high": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 7}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
            FilterInstance(filter_id="column_blocklist", params={"fields": ["url"]}),
        ),
    },
    field_transform_defaults=[
        {"table_id": "browser_visits", "field": "url", "transform_ids": ["pii_redaction"]},
        {"table_id": "browser_visits", "field": "title", "transform_ids": ["pii_redaction"]},
        {"table_id": "browser_visits", "field": "visited_at", "transform_ids": ["timestamp_to_date"]},
    ],
)

# Browser plugin events: clicks, highlights, star_page, VIDEO_PLAY
BROWSER_EVENTS = DataSourceDefinition(
    source_id="browser_events",
    display_name="Browser Events",
    source_type="ui_stream",
    schema_id="browser.events.v1",
    parser_id="browser.events.v1",
    canonical_mapper_id=None,
    canonical_group_id=None,
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=[],
    analytics_profile_id=None,
    enrichment_trigger="manual",
    ingestion_trigger="automatic",
    default_scope_id="activity",
    allowed_scope_ids=["activity:read", "activity:write"],
    default_filter_hints=["rolling_window_days", "timestamp_to_date"],
    filter_tier_kind="inferability",
    default_filter_tiers={
        "low": _manifest(FilterInstance(filter_id="rolling_window_days", params={"days": 30})),
        "medium": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 14}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
        ),
        "high": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 7}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
            FilterInstance(filter_id="max_rows", params={"count": 250}),
        ),
    },
    field_transform_defaults=[
        {"table_id": "browser_events", "field": "url", "transform_ids": ["pii_redaction"]},
        {"table_id": "browser_events", "field": "title", "transform_ids": ["pii_redaction"]},
        {"table_id": "browser_events", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
        {"table_id": "browser_events", "field": "visited_at", "transform_ids": ["timestamp_to_date"]},
    ],
)

# Sprint 02: Messenger ingestion (local_sync -> conversation_messages)
IMESSAGE = DataSourceDefinition(
    source_id="imessage",
    display_name="iMessage",
    source_type="local_sync",
    schema_id="imessage.messages.v1",
    parser_id="imessage.messages.v1",
    canonical_mapper_id="imessage",
    canonical_group_id="conversations",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["emo_27"],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",  # Sync runs on schedule or "Sync now"
    default_scope_id="messages",
    allowed_scope_ids=["messages:read", "messages:write"],
    default_filter_hints=["rolling_window_days", "max_rows", "timestamp_to_date"],
    filter_tier_kind="sensitivity",
    default_filter_tiers={
        "low": _manifest(FilterInstance(filter_id="rolling_window_days", params={"days": 90})),
        "medium": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 30}),
            FilterInstance(filter_id="max_rows", params={"count": 1000}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
        ),
        "high": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 14}),
            FilterInstance(filter_id="max_rows", params={"count": 250}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
        ),
    },
    field_transform_defaults=[
        {"table_id": "conversation_messages", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
        {"table_id": "conversation_messages", "field": "event_at", "transform_ids": ["timestamp_to_date"]},
    ],
)

SIGNAL = DataSourceDefinition(
    source_id="signal",
    display_name="Signal Desktop",
    source_type="local_sync",
    schema_id="signal.messages.v1",
    parser_id="signal.messages.v1",
    canonical_mapper_id="signal",
    canonical_group_id="conversations",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["emo_27"],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="messages",
    allowed_scope_ids=["messages:read", "messages:write"],
    default_filter_hints=["rolling_window_days", "max_rows", "timestamp_to_date"],
    filter_tier_kind="sensitivity",
    default_filter_tiers={
        "low": _manifest(FilterInstance(filter_id="rolling_window_days", params={"days": 90})),
        "medium": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 30}),
            FilterInstance(filter_id="max_rows", params={"count": 1000}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
        ),
        "high": _manifest(
            FilterInstance(filter_id="rolling_window_days", params={"days": 14}),
            FilterInstance(filter_id="max_rows", params={"count": 250}),
            FilterInstance(filter_id="timestamp_to_date", params={}),
        ),
    },
    field_transform_defaults=[
        {"table_id": "conversation_messages", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
        {"table_id": "conversation_messages", "field": "event_at", "transform_ids": ["timestamp_to_date"]},
    ],
)

REGISTRY = {
    CHATGPT_FILE.source_id: CHATGPT_FILE,
    CHATGPT_UI.source_id: CHATGPT_UI,
    BROWSER_VISITS.source_id: BROWSER_VISITS,
    BROWSER_EVENTS.source_id: BROWSER_EVENTS,
    IMESSAGE.source_id: IMESSAGE,
    SIGNAL.source_id: SIGNAL,
}


def list_sources() -> list[DataSourceDefinition]:
    return list(REGISTRY.values())


def get_sources_by_scope(scope_id: str) -> List[str]:
    """
    Return source_id list for sources whose default_scope_id or allowed_scope_ids match scope_id.
    scope_id may be the base name without :read/:write (e.g. 'messages') or a full MVP scope id.
    Used by Topos/Control Plane for scope → source resolution.
    """
    scope_id = (scope_id or "").strip()
    if not scope_id:
        return []
    scope_base = scope_id.split(":", 1)[0]
    return [
        defn.source_id
        for defn in REGISTRY.values()
        if (
            (defn.default_scope_id or "").strip() == scope_id
            or (defn.default_scope_id or "").strip() == scope_base
            or any(
                (allowed or "").strip() == scope_id or (allowed or "").strip().split(":", 1)[0] == scope_base
                for allowed in (defn.allowed_scope_ids or [])
            )
        )
    ]
