from __future__ import annotations

from typing import List, Sequence, Tuple

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
    signal_derivation_jobs=[
        "emo_27",
        "entities",
        "embeddings",
        "topics",
        "dimension_summary",
        "goal_extraction",
        "relationship_edges",
        "topic_clusters",
    ],
    analytics_profile_id="chatgpt_dev",
    enrichment_trigger="automatic",
    ingestion_trigger="manual",  # Ingestion processing waits for manual trigger after upload
    default_scope_id="ai_conversations",
    allowed_scope_ids=["ai_conversations:read"],
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
    signal_derivation_jobs=[
        "emo_27",
        "entities",
        "embeddings",
        "topics",
        "dimension_summary",
        "goal_extraction",
        "relationship_edges",
        "topic_clusters",
    ],
    analytics_profile_id="chatgpt_dev",
    enrichment_trigger="automatic",  # Enrichment runs automatically during ingestion
    default_scope_id="ai_conversations",
    allowed_scope_ids=["ai_conversations:read"],
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
    canonical_mapper_id="browser_activity",
    canonical_group_id="activity",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["url_classification", "embeddings"],
    signal_derivation_jobs=["url_classification", "embeddings", "topic_clusters"],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
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
    canonical_mapper_id="browser_activity",
    canonical_group_id="activity",
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
    signal_derivation_jobs=["entities", "relationship_edges", "emo_27"],
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
    signal_derivation_jobs=["entities", "relationship_edges", "emo_27"],
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

CALENDAR_STUB = DataSourceDefinition(
    source_id="calendar_stub",
    display_name="Calendar (stub)",
    source_type="stub",
    schema_id="calendar.stub.v1",
    parser_id="calendar.stub.v1",
    canonical_mapper_id=None,
    canonical_group_id="schedule",
    ingestion_trigger="manual",
    enrichment_trigger="manual",
    default_scope_id="schedule",
    allowed_scope_ids=["schedule:read"],
)

CONTACTS_ENRICHMENT_STUB = DataSourceDefinition(
    source_id="contacts_enrichment_stub",
    display_name="Contacts enrichment (stub)",
    source_type="stub",
    schema_id="contacts.stub.v1",
    parser_id="contacts.stub.v1",
    canonical_mapper_id=None,
    canonical_group_id="contacts",
    ingestion_trigger="manual",
    enrichment_trigger="manual",
    default_scope_id="contacts",
    allowed_scope_ids=["contacts:resolve"],
)

_DEMO_SIGNAL_JOBS_BASE = ["entities", "relationship_edges", "emo_27"]
_DEMO_FILE_SHAPE = {"format": "csv", "has_header": True}

DEMO_MESSENGER_FILE = DataSourceDefinition(
    source_id="demo_messenger_file",
    display_name="Demo Messenger (private)",
    source_type="file",
    schema_id="demo.messenger.v1",
    parser_id="demo.messenger.v1",
    canonical_mapper_id="demo_messenger",
    canonical_group_id="conversations",
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=_DEMO_SIGNAL_JOBS_BASE,
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="messages",
    allowed_scope_ids=["messages:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_EMAIL_FILE = DataSourceDefinition(
    source_id="demo_email_file",
    display_name="Demo Email Threads (private)",
    source_type="file",
    schema_id="demo.messenger.v1",
    parser_id="demo.messenger.v1",
    canonical_mapper_id="demo_messenger",
    canonical_group_id="conversations",
    canonical_enrichment_jobs=["emo_27", "topics"],
    signal_derivation_jobs=_DEMO_SIGNAL_JOBS_BASE + ["topics"],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="messages",
    allowed_scope_ids=["messages:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_CALENDAR_FILE = DataSourceDefinition(
    source_id="demo_calendar_file",
    display_name="Demo Calendar (private)",
    source_type="file",
    schema_id="demo.calendar.v1",
    parser_id="demo.calendar.v1",
    canonical_mapper_id="demo_calendar",
    canonical_group_id="schedule",
    signal_derivation_jobs=["availability_scores", "dimension_summary"],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="schedule",
    allowed_scope_ids=["schedule:read", "availability:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_JOURNAL_FILE = DataSourceDefinition(
    source_id="demo_journal_file",
    display_name="Demo Journal (private)",
    source_type="file",
    schema_id="demo.journal.v1",
    parser_id="demo.journal.v1",
    canonical_mapper_id="demo_journal",
    canonical_group_id="journal",
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=["dimension_summary", "emo_27"],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="health",
    allowed_scope_ids=["health:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_RESUME_FILE = DataSourceDefinition(
    source_id="demo_resume_file",
    display_name="Demo Resume (private)",
    source_type="file",
    schema_id="demo.profile.v1",
    parser_id="demo.profile.v1",
    canonical_mapper_id="demo_profile",
    canonical_group_id="profile",
    signal_derivation_jobs=["dimension_summary", "goal_extraction"],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="public_bio",
    allowed_scope_ids=["public_bio:read", "work_context:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_FINANCIAL_FILE = DataSourceDefinition(
    source_id="demo_financial_file",
    display_name="Demo Bank Accounts (private)",
    source_type="file",
    schema_id="demo.financial.v1",
    parser_id="demo.financial.v1",
    canonical_mapper_id="demo_financial",
    canonical_group_id="financial",
    signal_derivation_jobs=["dimension_summary"],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="resources",
    allowed_scope_ids=["resources:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_BROWSER_FILE = DataSourceDefinition(
    source_id="demo_browser_file",
    display_name="Demo Browser Activity (private)",
    source_type="file",
    schema_id="demo.browser.v1",
    parser_id="demo.browser.v1",
    canonical_mapper_id="browser_activity",
    canonical_group_id="activity",
    canonical_enrichment_jobs=["url_classification", "embeddings"],
    signal_derivation_jobs=["url_classification", "embeddings", "topic_clusters"],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="activity",
    allowed_scope_ids=["activity:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_PLACES_FILE = DataSourceDefinition(
    source_id="demo_places_file",
    display_name="Demo Places / Travel (private)",
    source_type="file",
    schema_id="demo.places.v1",
    parser_id="demo.places.v1",
    canonical_mapper_id="demo_places",
    canonical_group_id="places",
    signal_derivation_jobs=["dimension_summary"],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="places",
    allowed_scope_ids=["places:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

DEMO_CONTACTS_FILE = DataSourceDefinition(
    source_id="demo_contacts_file",
    display_name="Demo Contacts (private)",
    source_type="file",
    schema_id="demo.contacts.v1",
    parser_id="demo.contacts.v1",
    canonical_mapper_id="demo_contacts",
    canonical_group_id="contacts",
    enrichment_trigger="manual",
    ingestion_trigger="automatic",
    default_scope_id="contacts",
    allowed_scope_ids=["contacts:resolve"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

GROW_DATA_FILE = DataSourceDefinition(
    source_id="grow_data_file",
    display_name="Grow Time Log (private)",
    source_type="file",
    schema_id="grow.time_log.v1",
    parser_id="grow.time_log.v1",
    canonical_mapper_id="grow_time_log",
    canonical_group_id="journal",
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=[
        "dimension_summary",
        "emo_27",
        "goal_extraction",
        "availability_scores",
        "embeddings",
        "topic_clusters",
    ],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="health",
    allowed_scope_ids=[
        "health:read",
        "work_context:read",
        "schedule:read",
        "availability:read",
        "relationship_context:read",
        "places:read",
    ],
    brief_update_dimensions=["time", "wellbeing", "memory", "work", "relationships", "places", "intentions", "profile"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
    canonical_mapping_connected=True,
)

REGISTRY = {
    CHATGPT_FILE.source_id: CHATGPT_FILE,
    CHATGPT_UI.source_id: CHATGPT_UI,
    BROWSER_VISITS.source_id: BROWSER_VISITS,
    BROWSER_EVENTS.source_id: BROWSER_EVENTS,
    IMESSAGE.source_id: IMESSAGE,
    SIGNAL.source_id: SIGNAL,
    CALENDAR_STUB.source_id: CALENDAR_STUB,
    CONTACTS_ENRICHMENT_STUB.source_id: CONTACTS_ENRICHMENT_STUB,
    DEMO_MESSENGER_FILE.source_id: DEMO_MESSENGER_FILE,
    DEMO_EMAIL_FILE.source_id: DEMO_EMAIL_FILE,
    DEMO_CALENDAR_FILE.source_id: DEMO_CALENDAR_FILE,
    DEMO_JOURNAL_FILE.source_id: DEMO_JOURNAL_FILE,
    DEMO_RESUME_FILE.source_id: DEMO_RESUME_FILE,
    DEMO_FINANCIAL_FILE.source_id: DEMO_FINANCIAL_FILE,
    DEMO_BROWSER_FILE.source_id: DEMO_BROWSER_FILE,
    DEMO_PLACES_FILE.source_id: DEMO_PLACES_FILE,
    DEMO_CONTACTS_FILE.source_id: DEMO_CONTACTS_FILE,
    GROW_DATA_FILE.source_id: GROW_DATA_FILE,
}


def list_sources() -> list[DataSourceDefinition]:
    return list(REGISTRY.values())


def topic_cluster_source_ids() -> Tuple[str, ...]:
    """Source ids that opt into cross-source topic clustering via their definition."""
    return tuple(
        sorted(
            defn.source_id
            for defn in REGISTRY.values()
            if "topic_clusters" in (defn.signal_derivation_jobs or [])
        )
    )


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
