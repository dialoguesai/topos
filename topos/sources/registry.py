from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .bundled_canonical_triples import apply_bundled_canonical_defaults
from .definitions import (
    CANONICAL_ADDRESS_BOOK_SOURCE_ID,
    SOURCE_KIND_DERIVED,
    DataSourceDefinition,
    source_gets_discourse_lenses,
)
from shared.filtering import FilterInstance, FilterManifest


# Posture defaults (PLAN_PROVENANCE_SPLIT §3.1): journal/resume/profile-grade
# sources are 'personal' (owner-authored by construction); chat sources are
# 'mixed' (role decided per-row); browser_visits/feeds are 'ambient' (exposure,
# not expression); browser_events is 'personal'-grade engagement (highlights/
# stars are deliberate owner actions). Every bundled source sets it explicitly;
# runtime-installed payloads without the key default to 'mixed'.
def _source(**kwargs) -> DataSourceDefinition:
    return DataSourceDefinition(**apply_bundled_canonical_defaults(kwargs))


def _manifest(*filters: FilterInstance) -> dict:
    return FilterManifest(filters=list(filters)).to_storage_dict()


CHATGPT_FILE = _source(
    source_id="chatgpt_file_ingestion",
    posture="mixed",
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
    # work_context:read — authored goals from ChatGPT feed the work surface;
    # get_sources_by_scope must see this or installs/grants omit the goal source.
    allowed_scope_ids=["ai_conversations:read", "work_context:read"],
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
    posture="mixed",
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
    allowed_scope_ids=["ai_conversations:read", "work_context:read"],
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
    posture="ambient",
    display_name="Browser Visits",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="browser.visits.v1",
    parser_id="browser.visits.v1",
    canonical_group_id="activity",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["embeddings"],
    signal_derivation_jobs=[],
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
    posture="personal",
    display_name="Browser Events",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="browser.events.v1",
    parser_id="browser.events.v1",
    canonical_group_id="activity",
    raw_enrichment_jobs=[],
    # P2.2 (PLAN_PROVENANCE_SPLIT): browser highlights are Annotate-grade
    # engagement (expression of INTEREST) — they must reach the vector index and
    # the entity spine like browser_visits, not sit unenriched (was []/manual).
    # Mirror browser_visits (embeddings) and add entities so
    # a highlighted span is retrievable and its entities join the spine. Posture
    # stays 'personal'-grade engagement; the row role is still ambient+engaged
    # (record_role treats the activity/browser family as ambient regardless, so
    # enabling enrichment never upgrades belief — only interest reachability).
    canonical_enrichment_jobs=["embeddings", "entities"],
    signal_derivation_jobs=["entities"],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
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

# Remote connectors: GitHub user events fetched via remote MCP tools, saved via app_ingest
GITHUB_ACTIVITY = _source(
    source_id="github_activity",
    # Was 'personal' on the reasoning "owner-performed deeds, not consumption".
    # The deed is the owner's; the PROSE increasingly is not. Commit messages are
    # now written by coding agents, and posture answers "whose words is this
    # content" — not "who caused the work". The journal lane makes that concrete:
    # journal_entries is authored-by-construction in provenance.roles, so a
    # commit-derived entry was minting belief-grade first-person text out of
    # sentences the owner may never have read closely. The gate meant to prevent
    # that (authorship=authored, stamped by the producer) keys on a co-author
    # TRAILER regex, so it demotes `Co-Authored-By: Claude` and passes an equally
    # AI-written message that carries no trailer — undetectable by construction.
    # Evidence from this node's own corpus (2026-08-14): once commit text reached
    # the topic layer, `network_bridge :: "Topos (claude)"` came back as the third
    # largest cluster of it, 65 vectors.
    # 'ambient' is the module's own rule for this case — "unknown/ambiguous rows
    # fail toward the LESS-attributing role, never authored; belief extraction
    # must not guess" — and its cap is what strips belief eligibility (goals,
    # self-facts) while leaving interest/topic signal untouched: activity_events
    # rows are ROLE_AMBIENT by table already, so retrieval, clustering and triage
    # are unaffected. The owner can still override per connector
    # (storage.source_settings → effective_posture) if they want their commits
    # read as their own words.
    posture="ambient",
    display_name="GitHub Activity",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="github.activity.v1",
    parser_id="github.activity.v1",
    canonical_group_id="activity",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=[],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
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
    # §5a capability 2, first bundled consumer: the WORK a push event describes
    # is its commit messages, and nothing carried them onto the canonical row —
    # activity_events.content was NULL on every push (451/451 on the first live
    # node checked, 11 distinct titles over 451 rows), so every semantic reader
    # downstream could only ever match the repo NAME. Declared here rather than
    # hardcoded in the mapper so a third-party source states the same thing in
    # its own definition. Event granularity is one row per push — a multi-commit
    # push joins its messages, which is what "what did I work on" wants to read.
    # This row is now the ONLY place commit prose lands: the per-commit
    # journal_entries fan-out was retired with the posture change above, because
    # journal is authored-by-construction and commit prose is not reliably the
    # owner's words. Ambient here, first-person nowhere.
    canonical_field_map={
        "activity_events": {
            "content": {"path": "payload.commits[*].message", "join": "\n\n"},
        },
    },
)

# Sprint 02: Messenger ingestion (local_sync -> conversation_messages)
# B11: goal_extraction on authored (is_from_self) rows so messenger goals
# exist outside chatgpt — role gate in GoalExtractionJob skips non-owner text.
IMESSAGE = _source(
    source_id="imessage",
    posture="mixed",
    display_name="iMessage",
    source_type="local_sync",
    delivery="local_sync",
    schema_id="imessage.messages.v1",
    parser_id="imessage.messages.v1",
    canonical_group_id="conversations",
    raw_enrichment_jobs=[],
    # `entities` added 2026-08-25. It was absent, so NER never ran on messages
    # at ingest and every `message_entities` row this source ever had came from
    # a manual backfill: measured before the fix, a 2,354-message sync landed
    # with 43% emotion coverage and 0% entities, and the corpus as a whole sat
    # at 21%. That gap is upstream of the people work — entity mentions on
    # messages are what tie a person to what was actually said about them, and
    # what an owner-authored mention of a project is derived from.
    #
    # `topics` is deliberately still absent: it is the COST_HIGH lane (one
    # local-model generation per message, capped at MAX_JOB_MESSAGES=1000),
    # where `entities` is a RoBERTa token classifier. Adding topics here would
    # put an LLM call on every message of every sync.
    canonical_enrichment_jobs=["emo_27", "entities"],
    signal_derivation_jobs=['emo_27', 'goal_extraction'],
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
    posture="mixed",
    display_name="Signal Desktop",
    source_type="local_sync",
    delivery="local_sync",
    schema_id="signal.messages.v1",
    parser_id="signal.messages.v1",
    canonical_group_id="conversations",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=['emo_27', 'goal_extraction'],
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

# Remote connectors: Notion pages / Google Drive files, saved via app_ingest
# into the documents canonical lane (PLAN_CANONICAL_CALENDAR_DOCUMENTS Part A).
NOTION_PAGES = _source(
    source_id="notion_pages",
    posture="personal",
    display_name="Notion Pages",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="notion.page.v1",
    parser_id="notion.page.v1",
    canonical_group_id="documents",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=[],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="documents",
    allowed_scope_ids=["documents:read", "documents:write"],
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
)

GDRIVE_FILES = _source(
    source_id="gdrive_files",
    posture="personal",
    display_name="Google Drive Files",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="gdrive.file.v1",
    parser_id="gdrive.file.v1",
    canonical_group_id="documents",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=[],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="documents",
    allowed_scope_ids=["documents:read", "documents:write"],
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
)

# Remote connectors: Google Calendar events, saved via app_ingest into the
# beefed-up schedule lane (PLAN_CANONICAL_CALENDAR_DOCUMENTS Part B).
GCAL_EVENTS = _source(
    source_id="gcal_events",
    posture="personal",
    display_name="Google Calendar Events",
    source_type="ui_stream",
    delivery="client_push",
    schema_id="gcal.events.v1",
    parser_id="gcal.events.v1",
    canonical_group_id="schedule",
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=[],
    signal_derivation_jobs=['availability_scores'],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="schedule",
    allowed_scope_ids=["schedule:read", "availability:read"],
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
)

CALENDAR_STUB = _source(
    source_id="calendar_stub",
    posture="personal",
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

CANONICAL_ADDRESS_BOOK = _source(
    source_id=CANONICAL_ADDRESS_BOOK_SOURCE_ID,
    posture="personal",
    display_name="Canonical Address Book",
    source_type="derived",
    source_kind=SOURCE_KIND_DERIVED,
    schema_id="canonical.contacts.v1",
    parser_id="canonical.contacts.v1",
    canonical_mapper_id=None,
    canonical_group_id="contacts",
    ingestion_trigger="manual",
    enrichment_trigger="manual",
    default_scope_id="contacts",
    allowed_scope_ids=["contacts:resolve", "relationship_context:read"],
)

_DEMO_FILE_SHAPE = {"format": "csv", "has_header": True}

DEMO_MESSENGER_FILE = _source(
    source_id="demo_messenger_file",
    posture="mixed",
    display_name="Demo Messenger (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.messenger.v1",
    parser_id="demo.messenger.v1",
    canonical_group_id="conversations",
    canonical_enrichment_jobs=["emo_27"],
    signal_derivation_jobs=['emo_27', 'goal_extraction'],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="messages",
    allowed_scope_ids=["messages:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_EMAIL_FILE = _source(
    source_id="demo_email_file",
    posture="mixed",
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
    posture="personal",
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
    posture="personal",
    display_name="Demo Journal (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.journal.v1",
    parser_id="demo.journal.v1",
    canonical_group_id="journal",
    canonical_enrichment_jobs=["emo_27"],
    # B11: journal is personal-by-construction; goals feed work_context too.
    signal_derivation_jobs=['emo_27', 'goal_extraction'],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="health",
    allowed_scope_ids=["health:read", "work_context:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_RESUME_FILE = _source(
    source_id="demo_resume_file",
    posture="personal",
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
    posture="personal",
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
    posture="ambient",
    display_name="Demo Browser Activity (private)",
    source_type="file",
    delivery="owner_upload",
    schema_id="demo.browser.v1",
    parser_id="demo.browser.v1",
    canonical_group_id="activity",
    canonical_enrichment_jobs=["embeddings"],
    signal_derivation_jobs=[],
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="activity",
    allowed_scope_ids=["activity:read"],
    file_ingest_shape=_DEMO_FILE_SHAPE,
)

DEMO_PLACES_FILE = _source(
    source_id="demo_places_file",
    posture="personal",
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
    posture="personal",
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
    posture="mixed",
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

# YouTube caption archives (and later meeting / sales-call / lecture tools)
# → transcripts group. Discourse lenses (claims, events, programs, windowed
# relations) attach to this lane, not journals or chats. Ambient by default:
# the engine cannot tell if the owner spoke, sat in the room, or only listened.
# Connectors must not set participation_mode / is_self.
YOUTUBE_TRANSCRIPTS = _source(
    source_id="youtube_transcripts",
    posture="ambient",
    display_name="YouTube Transcripts",
    source_type="file",
    delivery="owner_upload",
    schema_id="transcript.session.v1",
    parser_id="transcript.session.v1",
    canonical_group_id="transcripts",
    discourse_lenses=True,
    raw_enrichment_jobs=[],
    canonical_enrichment_jobs=["entities", "topics", "embeddings", "facts", "derivation"],
    analytics_profile_id=None,
    enrichment_trigger="automatic",
    ingestion_trigger="automatic",
    default_scope_id="transcripts",
    allowed_scope_ids=["transcripts:read", "transcripts:write"],
    file_ingest_shape={"format": "json"},
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
        {"table_id": "transcript_segments", "field": "content", "transform_ids": ["pii_redaction", "nsfw_sanitization"]},
        {"table_id": "transcript_segments", "field": "event_at", "transform_ids": ["timestamp_to_date"]},
    ],
)

REGISTRY = {
    CHATGPT_FILE.source_id: CHATGPT_FILE,
    CHATGPT_UI.source_id: CHATGPT_UI,
    BROWSER_VISITS.source_id: BROWSER_VISITS,
    BROWSER_EVENTS.source_id: BROWSER_EVENTS,
    GITHUB_ACTIVITY.source_id: GITHUB_ACTIVITY,
    NOTION_PAGES.source_id: NOTION_PAGES,
    GDRIVE_FILES.source_id: GDRIVE_FILES,
    GCAL_EVENTS.source_id: GCAL_EVENTS,
    VOXTERM_TRANSCRIPTS.source_id: VOXTERM_TRANSCRIPTS,
    YOUTUBE_TRANSCRIPTS.source_id: YOUTUBE_TRANSCRIPTS,
    IMESSAGE.source_id: IMESSAGE,
    SIGNAL.source_id: SIGNAL,
    CALENDAR_STUB.source_id: CALENDAR_STUB,
    CANONICAL_ADDRESS_BOOK.source_id: CANONICAL_ADDRESS_BOOK,
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

# Pristine engine-shipped definitions. REGISTRY entries are REPLACED by runtime
# installs (install_source_definition), so lane-policy resolution against "the
# bundled definition" must read this snapshot, never REGISTRY: a 2026-05
# browser_visits install row (enrichment_trigger='manual', no jobs) rehydrated
# at every boot and silently disabled all browser enrichment for a month.
BUNDLED_REGISTRY = dict(REGISTRY)


def list_sources() -> list[DataSourceDefinition]:
    return list(REGISTRY.values())


def topic_cluster_source_ids() -> Tuple[str, ...]:
    """Source ids whose canonical embeddings participate in cross-source clustering."""
    from .canonical_signal_defaults import topic_cluster_eligible_source_ids

    return topic_cluster_eligible_source_ids(REGISTRY.values())


def discourse_lens_source_ids() -> Tuple[str, ...]:
    """Bundled source ids whose records may mint discourse-lens graph edges."""
    return tuple(
        sorted(
            str(defn.source_id)
            for defn in REGISTRY.values()
            if source_gets_discourse_lenses(defn)
        )
    )


def _scope_matches(scope_id: str, scope_base: str, default_scope: str, allowed: List[str]) -> bool:
    return (
        (default_scope or "").strip() in (scope_id, scope_base)
        or any(
            (a or "").strip() == scope_id or (a or "").strip().split(":", 1)[0] == scope_base
            for a in (allowed or [])
        )
    )


def _runtime_installed_sources_by_scope(scope_id: str, scope_base: str) -> List[str]:
    """Active RUNTIME-installed sources (grow exports, address-book merges, …)
    whose definitions match the scope. These live in source_runtime_installs,
    not the static registry — and nothing repopulates the in-memory registry
    after a restart (rehydrate_active_installs_runtime has no caller), so scope
    resolution must read the persisted rows directly or runtime sources' data
    is invisible to queries (the query-quality eval found places/health/contacts
    scopes serving demo fixtures while 1.9k real rows sat unreachable)."""
    try:
        from ..core.state import get_db_connection

        conn = get_db_connection()
        if conn is None:
            return []
        rows = conn.execute(
            """SELECT source_id, source_definition_json FROM source_runtime_installs
               WHERE is_active=1 AND status IN ('installed', 'active', 'ready')"""
        ).fetchall()
    except Exception:
        return []
    import json as _json

    out: List[str] = []
    for source_id, def_json in rows:
        try:
            source_def = _json.loads(def_json) if isinstance(def_json, str) else (def_json or {})
        except _json.JSONDecodeError:
            continue
        if _scope_matches(
            scope_id,
            scope_base,
            str(source_def.get("default_scope_id") or ""),
            list(source_def.get("allowed_scope_ids") or []),
        ):
            sid = str(source_id or "").strip()
            if sid and sid not in out:
                out.append(sid)
    return out


def _registry_posture_default(source_id: str) -> Optional[str]:
    """Registry DataSourceDefinition.posture default for a source_id.

    Covers the static REGISTRY first, then active runtime-installed
    definitions (source_runtime_installs snapshots — same rows
    _runtime_installed_sources_by_scope reads). Returns None when the source
    is unknown so effective_posture can fall through to the safe 'mixed'
    default."""
    sid = (source_id or "").strip()
    defn = REGISTRY.get(sid)
    if defn is not None:
        posture = getattr(defn, "posture", None)
        # A runtime install REPLACES the bundled definition, and a payload that simply omits
        # `posture` lands on the dataclass default `mixed` — silently downgrading a source
        # that the bundle declares `ambient` or `personal`. Measured live: browser_visits,
        # imessage, grow_journal and github_activity all resolved `mixed` this way, so
        # posture stopped distinguishing anything and nothing raised. Prefer the bundled
        # declaration when the active definition only carries the default.
        if posture == "mixed":
            bundled = BUNDLED_REGISTRY.get(sid)
            bundled_posture = getattr(bundled, "posture", None) if bundled is not None else None
            if bundled_posture and bundled_posture != "mixed" and defn is not bundled:
                return bundled_posture
        return posture
    if not sid:
        return None
    try:
        from ..core.state import get_db_connection

        conn = get_db_connection()
        if conn is None:
            return None
        row = conn.execute(
            """SELECT source_definition_json FROM source_runtime_installs
               WHERE source_id=? AND is_active=1
                 AND status IN ('installed', 'active', 'ready')
               ORDER BY rowid DESC LIMIT 1""",
            (sid,),
        ).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    import json as _json

    try:
        source_def = _json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    except _json.JSONDecodeError:
        return None
    posture = source_def.get("posture") if isinstance(source_def, dict) else None
    if posture:
        return str(posture)
    # Same rule for the stored-JSON path: an install that never declared a posture must not
    # erase what the bundle knows.
    bundled = BUNDLED_REGISTRY.get(sid)
    bundled_posture = getattr(bundled, "posture", None) if bundled is not None else None
    return str(bundled_posture) if bundled_posture else None


def effective_posture(source_id: str, dataset_id: str = "", conn=None) -> str:
    """Resolve the EFFECTIVE posture for a source (PLAN_PROVENANCE_SPLIT P1.4).

    Precedence (highest first):
      1. the owner's per-connector override in user_ingestion_sources
         (storage.source_settings; NULL there = no override);
      2. the registry DataSourceDefinition.posture default (static REGISTRY,
         then active runtime-installed definition);
      3. 'mixed' — the safe default that decides role per-row.

    ``conn``/``dataset_id`` are optional: without them (or on any read error)
    the override step is skipped and resolution falls through to the registry
    default. This is the single helper every posture consumer must call so the
    override is honoured everywhere posture matters (record_role wiring, role
    gates)."""
    sid = (source_id or "").strip()
    if not sid:
        return "mixed"

    override: Optional[str] = None
    if conn is not None and (dataset_id or "").strip():
        try:
            from ..storage.source_settings import get_source_settings

            settings = get_source_settings(conn, dataset_id, sid) or {}
            candidate = settings.get("posture")
            if candidate:
                override = str(candidate).strip().lower()
        except Exception:  # noqa: BLE001
            override = None
    if override in ("personal", "mixed", "ambient"):
        return override

    registry_default = _registry_posture_default(sid)
    if registry_default in ("personal", "mixed", "ambient"):
        return registry_default

    return "mixed"


def get_sources_by_scope(scope_id: str) -> List[str]:
    """
    Return source_id list for sources whose default_scope_id or allowed_scope_ids match scope_id.
    scope_id may be the base name without :read/:write (e.g. 'messages') or a full MVP scope id.
    Covers the static registry AND active runtime-installed sources.
    Used by Topos/Control Plane for scope → source resolution.
    """
    scope_id = (scope_id or "").strip()
    if not scope_id:
        return []
    scope_base = scope_id.split(":", 1)[0]
    ids = [
        defn.source_id
        for defn in REGISTRY.values()
        if _scope_matches(
            scope_id, scope_base, defn.default_scope_id or "", list(defn.allowed_scope_ids or [])
        )
    ]
    for sid in _runtime_installed_sources_by_scope(scope_id, scope_base):
        if sid not in ids:
            ids.append(sid)
    return ids
