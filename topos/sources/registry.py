from __future__ import annotations

from typing import List, Sequence, Tuple

from .bundled_canonical_triples import apply_bundled_canonical_defaults
from .definitions import DataSourceDefinition
from shared.filtering import FilterInstance, FilterManifest


def _source(**kwargs) -> DataSourceDefinition:
    return DataSourceDefinition(**apply_bundled_canonical_defaults(kwargs))


def _manifest(*filters: FilterInstance) -> dict:
    return FilterManifest(filters=list(filters)).to_storage_dict()


CHATGPT_FILE = _source(
    source_id="chatgpt_file_ingestion",
    display_name="ChatGPT File Ingestion",
    source_type="file",
    delivery="owner_upload",
    schema_id="chatgpt.conversation.v2",  # Updated to v2 for real ChatGPT data
    parser_id="chatgpt.conversation.v2",  # Updated to v2
    canonical_group_id="ai_messages",
    raw_enrichment_jobs=["attachments", "tool_calls", "language", "time_normalization"],
    canonical_enrichment_jobs=["entities", "topics", "sentiment", "embeddings", "emo_27"],
    signal_derivation_jobs=['emo_27', 'topics', 'goal_extraction'],
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

CHATGPT_UI = _source(
    source_id="chatgpt_ui_conversation",
    display_name="ChatGPT UI Conversation",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="chatgpt.conversation.v1",
    parser_id="chatgpt.conversation.v1",
    canonical_group_id="ai_messages",
    raw_enrichment_jobs=["attachments", "tool_calls", "language", "time_normalization"],
    canonical_enrichment_jobs=["entities", "topics", "sentiment", "embeddings", "emo_27"],
    signal_derivation_jobs=['emo_27', 'topics', 'goal_extraction'],
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
BROWSER_VISITS = _source(
    source_id="browser_visits",
    display_name="Browser Visits",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="browser.visits.v1",
    parser_id="browser.visits.v1",
    canonical_group_id="activity",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["url_classification", "embeddings"],
    signal_derivation_jobs=['url_classification'],
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
BROWSER_EVENTS = _source(
    source_id="browser_events",
    display_name="Browser Events",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="browser.events.v1",
    parser_id="browser.events.v1",
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
IMESSAGE = _source(
    source_id="imessage",
    display_name="iMessage",
    source_type="local_sync",
    delivery="local_sync",
    schema_id="imessage.messages.v1",
    parser_id="imessage.messages.v1",
    canonical_group_id="conversations",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=['emo_27'],
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

SIGNAL = _source(
    source_id="signal",
    display_name="Signal Desktop",
    source_type="local_sync",
    delivery="local_sync",
    schema_id="signal.messages.v1",
    parser_id="signal.messages.v1",
    canonical_group_id="conversations",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=['emo_27'],
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

CALENDAR_STUB = _source(
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

CONTACTS_ENRICHMENT_STUB = _source(
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

_DEMO_FILE_SHAPE = {"format": "csv", "has_header": True}

DEMO_MESSENGER_FILE = _source(
    source_id="demo_messenger_file",
    display_name="Demo Messenger (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.messenger.v1",
    parser_id="demo.messenger.v1",
    canonical_group_id="conversations",
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=['emo_27'],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_EMAIL_FILE = _source(
    source_id="demo_email_file",
    display_name="Demo Email Threads (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.messenger.v1",
    parser_id="demo.messenger.v1",
    canonical_group_id="conversations",
    canonical_enrichment_jobs=["emo_27", "topics"],
    signal_derivation_jobs=['emo_27', 'topics'],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="messages",
    allowed_scope_ids=["messages:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_CALENDAR_FILE = _source(
    source_id="demo_calendar_file",
    display_name="Demo Calendar (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.calendar.v1",
    parser_id="demo.calendar.v1",
    canonical_group_id="schedule",
    signal_derivation_jobs=['availability_scores'],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="schedule",
    allowed_scope_ids=["schedule:read", "availability:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_JOURNAL_FILE = _source(
    source_id="demo_journal_file",
    display_name="Demo Journal (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.journal.v1",
    parser_id="demo.journal.v1",
    canonical_group_id="journal",
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=['emo_27'],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="health",
    allowed_scope_ids=["health:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_RESUME_FILE = _source(
    source_id="demo_resume_file",
    display_name="Demo Resume (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.profile.v1",
    parser_id="demo.profile.v1",
    canonical_group_id="profile",
    signal_derivation_jobs=['goal_extraction'],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="public_bio",
    allowed_scope_ids=["public_bio:read", "work_context:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_FINANCIAL_FILE = _source(
    source_id="demo_financial_file",
    display_name="Demo Bank Accounts (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.financial.v1",
    parser_id="demo.financial.v1",
    canonical_group_id="financial",
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="resources",
    allowed_scope_ids=["resources:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_BROWSER_FILE = _source(
    source_id="demo_browser_file",
    display_name="Demo Browser Activity (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.browser.v1",
    parser_id="demo.browser.v1",
    canonical_group_id="activity",
    canonical_enrichment_jobs=["url_classification", "embeddings"],
    signal_derivation_jobs=['url_classification'],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="activity",
    allowed_scope_ids=["activity:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_PLACES_FILE = _source(
    source_id="demo_places_file",
    display_name="Demo Places / Travel (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.places.v1",
    parser_id="demo.places.v1",
    canonical_group_id="places",
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="places",
    allowed_scope_ids=["places:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_CONTACTS_FILE = _source(
    source_id="demo_contacts_file",
    display_name="Demo Contacts (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.contacts.v1",
    parser_id="demo.contacts.v1",
    canonical_group_id="contacts",
    enrichment_trigger="manual",
    ingestion_trigger="automatic",
    default_scope_id="contacts",
    allowed_scope_ids=["contacts:resolve"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

# VoxTerm voice transcript segments (ui_stream / app_ingest)
VOXTERM_TRANSCRIPTS = _source(
    source_id="voxterm_transcripts",
    display_name="VoxTerm Transcripts",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="voxterm.transcript.v1",
    parser_id="voxterm.transcript.v1",
    canonical_group_id="conversations",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=[],
    analytics_profile_id=None,
    enrichment_trigger="manual",
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
    VOXTERM_TRANSCRIPTS.source_id: VOXTERM_TRANSCRIPTS,
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
}


def list_sources() -> list[DataSourceDefinition]:
    return list(REGISTRY.values())


def topic_cluster_source_ids() -> Tuple[str, ...]:
    """Source ids whose canonical embeddings participate in cross-source clustering."""
    from .canonical_signal_defaults import topic_cluster_eligible_source_ids

    return topic_cluster_eligible_source_ids(REGISTRY.values())


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
