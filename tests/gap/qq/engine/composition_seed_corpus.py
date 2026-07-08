"""Seeded-needle corpus: a deterministic scratch database with one unique canary
per storage layer, plus the S-series composition cases that grade needle recall.

This is the reproducible lane: no live data, no enrichment, no LLM — every
needle is written directly into the layer's own store, so a case failing means
*retrieval* cannot extract that layer's information, not that the data is absent.
The vector/embedding layer is intentionally NOT seeded (writing honest vectors
requires the embedding model, which breaks machine-independence); it is covered
by the live lane and marked live-only in the coverage matrix.

Bump SEEDED_CORPUS_VERSION when needles/cases change.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from composition_eval_cases import CompositionCase, Oracle

# qq-seeded-1 used synthetic source_ids that canonical retrieval filters out
# (manifest default_source_ids gate) — its canonical scores measured the corpus,
# not retrieval. qq-seeded-2 seeds rows under each scope's default sources.
# qq-seeded-3: +T-series bi-temporal facts (a supersession chain planted through
# FactStore itself, so belief revision is exercised for real) and the T1/T2
# temporal-integrity cases (PLAN_QUERY_EVAL_DEEP_SUITE.md §T).
SEEDED_CORPUS_VERSION = "qq-seeded-3"

_NOW = datetime.now(timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# One needle phrase per layer — distinctive, never plausibly in real data.
NEEDLES = {
    "conversation_messages": "the cobalt kayak invoice from Aurelio is ready for review",
    "ai_chat_messages": "vermilion heron migration notes for the field survey",
    "entity_dossier": "Quillon Marsh leads the tidepool mapping initiative",
    "stat_insight": "Total kayak spend: 412.00 across 3 transactions.",
    "user_goal": "assemble the orrery workshop for the studio",
    "dimension_brief": "Quarterly focus: the zephyr manifold refactor.",
    "calendar_events": "Umber Council sync",
    "journal_entries": "practiced cane pulling at the glasswork studio",
    "location_events": "Fennel Street Studio",
    "contacts": "peridot.vale@example.com",
    "financial_transactions": "cobalt kayak deposit",
    # T-series bi-temporal chain: the old value is superseded by the new one.
    "fact_current": "the Foxglove Atelier",
    "fact_superseded": "the Larkspur Annex",
}


def build_seeded_corpus(db_path: Path) -> Path:
    """Create the scratch DB at db_path with schema + all layer needles."""
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        apply_all_migrations(conn)
        ensure_all_tables(conn)  # conversations + conversation_messages
        CanonicalTablesManager(conn)  # ai_chat tables

        # Layer: canonical messenger messages
        conn.execute(
            """INSERT INTO conversation_messages
               (message_id, conversation_id, dataset_id, sender_type, sender_id, content,
                event_at, source_id, is_from_self)
               VALUES ('seed-msg-1', 'seed-conv-1', 'seed', 'contact', 'aurelio-1', ?, ?, 'demo_messenger_file', 0)""",
            (NEEDLES["conversation_messages"], _iso(0.5)),
        )

        # Layer: canonical AI chat
        conn.execute(
            """INSERT INTO ai_chat_messages
               (message_id, conversation_id, sender_type, source_id, content, event_at)
               VALUES ('seed-ai-1', 'seed-aiconv-1', 'user', 'chatgpt_ingestion', ?, ?)""",
            (NEEDLES["ai_chat_messages"], _iso(1)),
        )

        # Layer: entity spine (entity + dossier + mention)
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count,
                first_seen, last_seen)
               VALUES ('seed-ent-1', 'person', 'Quillon Marsh', 'quillon marsh', 3, ?, ?)""",
            (_iso(30), _iso(1)),
        )
        conn.execute(
            """INSERT INTO signal_objects
               (object_id, signal_dimension, object_type, object_key, payload_json,
                confidence, valid_from, created_at, updated_at)
               VALUES ('seed-dossier-1', 'relationships', 'entity_dossier', 'dossier:seed-ent-1',
                       ?, 0.9, ?, ?, ?)""",
            (
                json.dumps(
                    {
                        "canonical_name": "Quillon Marsh",
                        "summary_text": NEEDLES["entity_dossier"],
                        "top_connections": ["Owner"],
                        "stat_lines": [],
                    }
                ),
                _iso(2), _iso(2), _iso(2),
            ),
        )
        conn.execute(
            """INSERT INTO entity_mentions
               (mention_id, entity_id, record_id, canonical_table, surface_text, event_at, source_id)
               VALUES ('seed-mention-1', 'seed-ent-1', 'seed-msg-1', 'conversation_messages',
                       'Quillon Marsh shared the tidepool charts', ?, 'demo_messenger_file')""",
            (_iso(1),),
        )

        # Layer: stats engine insight
        conn.execute(
            """INSERT INTO signal_facts
               (fact_id, dimension, source_id, record_id, model, provider, payload_json, created_at)
               VALUES ('stat:financial.spend.by_category:kayak', 'resources', 'stats_engine',
                       'financial.spend.by_category', 'stats_engine_v1', 'topos', ?, ?)""",
            (
                json.dumps(
                    {
                        "fact_id": "stat:financial.spend.by_category:kayak",
                        "dimension": "resources",
                        "source_id": "stats_engine",
                        "record_id": "financial.spend.by_category",
                        "object_type": "stat_insight",
                        "tag": NEEDLES["stat_insight"],
                        "summary_text": NEEDLES["stat_insight"],
                        "stat_summary": {"n": 3, "total": 412.0},
                        "group_key": "kayak",
                        "confidence": 1.0,
                        "disclosure": "owner_only",
                        "provider": "topos",
                        "model": "stats_engine_v1",
                    }
                ),
                _iso(1),
            ),
        )

        # Layer: user goals
        conn.execute(
            """INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json, created_at)
               VALUES ('seed-goal-1', 'seed-rec-goal', 'chatgpt_ingestion', ?, '{}', ?)""",
            (NEEDLES["user_goal"], _iso(3)),
        )

        # Layer: dimension brief
        conn.execute(
            """INSERT INTO signal_dimension_briefs
               (brief_id, signal_dimension, head_revision_id, structured_json, markdown_body,
                revision_number, updated_at)
               VALUES ('seed-brief-1', 'work', 'seed-brief-rev-1', '{}', ?, 1, ?)""",
            (NEEDLES["dimension_brief"], _iso(1)),
        )

        # Layer: calendar
        conn.execute(
            """INSERT INTO calendar_events (event_id, title, starts_at, ends_at, source_id)
               VALUES ('seed-cal-1', ?, ?, ?, 'demo_calendar_file')""",
            (NEEDLES["calendar_events"], _iso(-2), _iso(-2.05)),
        )

        # Layer: journal
        conn.execute(
            """INSERT INTO journal_entries (entry_id, entry_at, category, content, source_id)
               VALUES ('seed-journal-1', ?, 'glasswork', ?, 'demo_journal_file')""",
            (_iso(2), NEEDLES["journal_entries"]),
        )

        # Layer: places
        conn.execute(
            """INSERT INTO location_events (event_id, place_name, city, event_at, event_type, source_id)
               VALUES ('seed-loc-1', ?, 'Austin', ?, 'visit', 'demo_places_file')""",
            (NEEDLES["location_events"], _iso(1)),
        )

        # Layer: contacts
        conn.execute(
            """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
               VALUES ('seed-contact-1', 'seed', 'demo_contacts_file', 'Peridot Vale', 0)"""
        )
        conn.execute(
            """INSERT INTO contact_identifiers
               (dataset_id, source_id, identifier, identifier_type, contact_id)
               VALUES ('seed', 'demo_contacts_file', ?, 'email', 'seed-contact-1')""",
            (NEEDLES["contacts"],),
        )

        # Layer: financial transactions
        conn.execute(
            """INSERT INTO financial_transactions
               (transaction_id, account_type, account_name, posted_at, amount, currency,
                category, description, source_id)
               VALUES ('seed-fin-1', 'checking', 'seed', ?, -137.5, 'USD', 'kayak', ?, 'demo_financial_file')""",
            (_iso(4), NEEDLES["financial_transactions"]),
        )

        # Layer: bi-temporal fact store (T-series). Asserted through FactStore so
        # the supersession logic itself runs: the second assert closes the first
        # fact's validity window (valid_to set, row kept — never deleted).
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count,
                first_seen, last_seen, is_self)
               VALUES ('seed-self-1', 'person', 'Owner', 'owner', 1, ?, ?, 1)""",
            (_iso(90), _iso(1)),
        )
        from topos.features.facts.store import FactStore

        fact_store = FactStore(conn)
        fact_store.assert_fact(
            subject_entity_id="seed-self-1",
            predicate="studio space",
            object_value=NEEDLES["fact_superseded"],
            dimension="work",
            valid_from=_iso(60),
        )
        fact_store.assert_fact(
            subject_entity_id="seed-self-1",
            predicate="studio space",
            object_value=NEEDLES["fact_current"],
            dimension="work",
            valid_from=_iso(10),
        )

        conn.commit()
    finally:
        conn.close()
    return db_path


def _needle_oracle(key: str, extra: List[str] | None = None):
    def _oracle(conn: sqlite3.Connection) -> Oracle:
        groups = [[NEEDLES[key]]]
        for alt in extra or []:
            groups.append([alt])
        return Oracle(groups, f"seeded needle: {key}")

    return _oracle


SEEDED_COMPOSITION_CASES: List[CompositionCase] = [
    CompositionCase(
        "S1", "seeded", "cobalt kayak invoice", "messages:read", "raw",
        _needle_oracle("conversation_messages"), layer="canonical:conversation_messages",
        description="Raw retrieval finds the planted messenger message"),
    CompositionCase(
        "S2", "seeded", "cobalt kayak invoice from Aurelio", "messages:read", "summary",
        _needle_oracle("conversation_messages"),
        topic_terms=("kayak", "aurelio", "invoice"),
        layer="canonical:conversation_messages",
        description="Summary mode surfaces the planted messenger message"),
    CompositionCase(
        "S3", "seeded", "vermilion heron migration notes", "ai_conversations:read", "raw",
        _needle_oracle("ai_chat_messages"), layer="canonical:ai_chat_messages",
        description="Raw retrieval finds the planted AI-chat message"),
    CompositionCase(
        "S4", "seeded", "Who is Quillon Marsh?", "relationship_context:read", "summary",
        _needle_oracle("entity_dossier", extra=["tidepool"]),
        expected_sources=("entity_dossier",), topic_terms=("quillon", "marsh", "tidepool"),
        layer="entities:dossier",
        description="Entity spine surfaces the planted dossier"),
    CompositionCase(
        "S5", "seeded", "How much do I typically spend on kayaks?", "resources:read", "summary",
        _needle_oracle("stat_insight", extra=["412"]),
        expected_sources=("stat_insight",), topic_terms=("kayak", "spend"),
        layer="stats:stat_insight",
        description="Aggregate routing surfaces the planted stat insight"),
    CompositionCase(
        "S6", "seeded", "What are my current goals?", "work_context:read", "summary",
        _needle_oracle("user_goal", extra=["orrery"]),
        expected_sources=("user_goal",), topic_terms=("orrery", "goal", "workshop"),
        layer="signals:user_goals",
        description="Goal store surfaces the planted goal"),
    CompositionCase(
        "S7", "seeded", "What is my current work focus?", "work_context:read", "summary",
        _needle_oracle("dimension_brief", extra=["zephyr manifold"]),
        expected_sources=("dimension_brief",), topic_terms=("zephyr", "manifold", "focus", "work"),
        layer="signals:dimension_brief",
        description="Dimension brief surfaces the planted brief"),
    CompositionCase(
        "S8", "seeded", "What is on my calendar?", "schedule:read", "summary",
        _needle_oracle("calendar_events"),
        expected_sources=("canonical:calendar_events",), topic_terms=("umber", "council", "sync"),
        layer="canonical:calendar_events",
        description="Calendar canonical rows surface the planted event"),
    CompositionCase(
        "S9", "seeded", "What have I been journaling about?", "health:read", "summary",
        _needle_oracle("journal_entries", extra=["glasswork"]),
        expected_sources=("canonical:journal_entries",), topic_terms=("glasswork", "cane", "studio"),
        layer="canonical:journal_entries",
        description="Journal canonical rows surface the planted entry"),
    CompositionCase(
        "S10", "seeded", "Which places have I been to?", "places:read", "summary",
        _needle_oracle("location_events"),
        expected_sources=("canonical:location_events",), topic_terms=("fennel", "studio"),
        layer="canonical:location_events",
        description="Places canonical rows surface the planted location"),
    CompositionCase(
        "S11", "seeded", "Find the contact record for Peridot Vale", "contacts:resolve", "raw",
        _needle_oracle("contacts"), layer="canonical:contacts",
        description="Contact resolution returns the planted identifier"),
    CompositionCase(
        "S12", "seeded", "What did I spend on the kayak deposit?", "resources:read", "raw",
        _needle_oracle("financial_transactions", extra=["137.5"]),
        layer="canonical:financial_transactions",
        description="Raw financial rows surface the planted transaction"),
    CompositionCase(
        "S13", "seeded", "Tell me about the marzipan lighthouse restoration",
        "messages:read", "summary",
        lambda conn: Oracle([], "fabricated topic on a canary-only DB"),
        negative=True, layer="negative_control",
        description="Nothing but canaries exists — a fabricated topic must return nothing"),
    # --- T-series: temporal integrity (PLAN_QUERY_EVAL_DEEP_SUITE.md §T) --------------
    CompositionCase(
        "T1", "seeded", "What is my current studio space?", "work_context:read", "summary",
        _needle_oracle("fact_current"),
        topic_terms=("studio", "atelier", "foxglove"),
        layer="facts:bitemporal_current",
        description="T1a as-of-now: the ACTIVE fact answers; the superseded one must not "
                    "read as current"),
    CompositionCase(
        "T2", "seeded", "Where was my studio space before?", "work_context:read", "summary",
        lambda conn: Oracle(
            [[NEEDLES["fact_superseded"]], ["no longer current"]],
            "superseded fact must surface for a past-tense ask, marked stale",
        ),
        topic_terms=("studio", "larkspur", "annex"),
        layer="facts:bitemporal_history",
        description="T1b belief revision: planner temporal_shift='past' widens the fact "
                    "read to closed revisions; T3 staleness honesty: the superseded fact "
                    "carries an explicit no-longer-current marker"),
    CompositionCase(
        "T3", "seeded", "What is on my calendar from the last week and coming days?",
        "schedule:read", "summary",
        lambda conn: Oracle([], "graded on dated items within the window"),
        temporal_days=7, layer="planner:time_window_seeded",
        description="T2 window arithmetic on a deterministic corpus (seeded sibling of "
                    "the live C9/C30 lanes)"),
]
