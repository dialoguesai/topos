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

import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from composition_eval_cases import CompositionCase, Oracle

# qq-seeded-1 used synthetic source_ids that canonical retrieval filters out
# (manifest default_source_ids gate) — its canonical scores measured the corpus,
# not retrieval. qq-seeded-2 seeds rows under each scope's default sources.
# qq-seeded-3: +T-series bi-temporal facts (a supersession chain planted through
# FactStore itself, so belief revision is exercised for real) and the T1/T2
# temporal-integrity cases (PLAN_QUERY_EVAL_DEEP_SUITE.md §T).
# qq-seeded-4: +T4-T8 temporal instrument cases (plan B1.1-B1.5): as-of
# point-in-time, live-lane supersession canary (T5, LIVE_TEMPORAL_CASES),
# stale-fact suppression, edge validity (red until B2.2), staleness honesty.
SEEDED_CORPUS_VERSION = "qq-seeded-4"

_NOW = datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(days_ago: float) -> str:
    return _fmt(_NOW - timedelta(days=days_ago))


# --- T4 (B1.1) as-of month machinery --------------------------------------------------
# The T4 query asks for a point-in-time value "in <month>", where <month> is the
# month containing now-40d — inside the superseded window of the studio-space
# chain. The chain's belief-validity dates are derived from the same _NOW the
# rest of the corpus uses (deterministic relative to run time, no extra
# wall-clock reads) and auto-shift outward when the target month would poke out
# of the default now-60d..now-10d window (e.g. when now-40d falls at a month
# edge), so any as-of instant inside the month is guaranteed to resolve to the
# superseded fact.


def _t4_month_bounds(now: datetime) -> Tuple[datetime, datetime]:
    """[month_start, next_month_start) of the month containing now-40d."""
    anchor = now - timedelta(days=40)
    month_start = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month_start = (month_start + timedelta(days=32)).replace(day=1)
    return month_start, next_month_start


def fact_chain_valid_from(now: datetime) -> Tuple[str, str]:
    """(superseded_valid_from, current_valid_from) for the studio-space chain.

    Defaults to (now-60d, now-10d); shifted so the whole T4 target month sits
    strictly inside the superseded window with a one-day buffer on both sides:
      superseded_valid_from <= month_start - 1d
      current_valid_from    >= next_month_start + 1d   (and always < now)
    """
    month_start, next_month_start = _t4_month_bounds(now)
    superseded_from = min(now - timedelta(days=60), month_start - timedelta(days=1))
    current_from = max(now - timedelta(days=10), next_month_start + timedelta(days=1))
    return _fmt(superseded_from), _fmt(current_from)


_T4_MONTH_START, _T4_NEXT_MONTH_START = _t4_month_bounds(_NOW)
T4_MONTH_LABEL = _T4_MONTH_START.strftime("%B %Y")
FACT_SUPERSEDED_VALID_FROM, FACT_CURRENT_VALID_FROM = fact_chain_valid_from(_NOW)

# T7 (B1.4) edge-validity fixtures: a collaboration that ENDED in the past.
T7_SRC_ENTITY_ID = "seed-ent-maren"
T7_DST_ENTITY_ID = "seed-ent-brindle"
T7_EDGE_TYPE = "worked_with"

# T8 (B1.5) staleness-honesty fixtures: a stat artifact carrying its OWN event
# period. The oracle demands the artifact's date be rendered, not just its value.
T8_STAT_FACT_ID = "stat:financial.spend.by_category:kiln"
T8_STAT_PERIOD_START = _iso(52)
T8_STAT_PERIOD_END = _iso(45)
T8_DATE_NEEDLES = (T8_STAT_PERIOD_END[:10], T8_STAT_PERIOD_START[:10])


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
    # T7: the counterpart of an ENDED collaboration edge (edges:validity).
    "edge_former_collaborator": "Brindle Cassavetes",
    # T8: a stat artifact whose payload carries its own event period.
    "stat_insight_dated": "Total kiln-firing spend: 258.00 across 2 transactions.",
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
        # Chain dates ~(now-60d, now-10d), auto-shifted so the T4 target month
        # (month of now-40d) always sits inside the superseded window.
        fact_store.assert_fact(
            subject_entity_id="seed-self-1",
            predicate="studio space",
            object_value=NEEDLES["fact_superseded"],
            dimension="work",
            valid_from=FACT_SUPERSEDED_VALID_FROM,
        )
        fact_store.assert_fact(
            subject_entity_id="seed-self-1",
            predicate="studio space",
            object_value=NEEDLES["fact_current"],
            dimension="work",
            valid_from=FACT_CURRENT_VALID_FROM,
        )

        # Layer: entity edges (T7). A collaboration that ENDED in the past,
        # written through the production update_edge path. entity_edges has
        # UNIQUE(src,dst,type) and no validity columns (plan B2.2): no closed
        # revision or staleness marker can exist yet — exactly what T7
        # instruments. Maren gets mentions (so she links) but NO dossier, so
        # entity_context_items takes the top_edges branch.
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count,
                first_seen, last_seen)
               VALUES (?, 'person', 'Maren Oxbow', 'maren oxbow', 2, ?, ?)""",
            (T7_SRC_ENTITY_ID, _iso(80), _iso(70)),
        )
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count,
                first_seen, last_seen)
               VALUES (?, 'person', ?, 'brindle cassavetes', 1, ?, ?)""",
            (T7_DST_ENTITY_ID, NEEDLES["edge_former_collaborator"], _iso(80), _iso(70)),
        )
        conn.execute(
            """INSERT INTO entity_mentions
               (mention_id, entity_id, record_id, canonical_table, surface_text, event_at, source_id)
               VALUES ('seed-mention-maren', ?, 'seed-msg-1', 'conversation_messages',
                       'Maren Oxbow signed the sublease ledger', ?, 'demo_messenger_file')""",
            (T7_SRC_ENTITY_ID, _iso(70)),
        )
        from topos.features.entities.edges import update_edge

        update_edge(
            conn,
            src_entity_id=T7_SRC_ENTITY_ID,
            dst_entity_id=T7_DST_ENTITY_ID,
            edge_type=T7_EDGE_TYPE,
            event_at=_iso(75),
        )

        # Layer: dated stat insight (T8). Unlike the kayak stat above, this one
        # carries its OWN event period in the payload (period_start/period_end)
        # and a created_at 45 days back. Staleness honesty (B1.5) requires the
        # response to render that date next to the value; retrieval currently
        # renders tag text only (_load_stat_insight_items), so the date group
        # stays red until stat artifacts are date-stamped (B2.1/B2.3).
        conn.execute(
            """INSERT INTO signal_facts
               (fact_id, dimension, source_id, record_id, model, provider, payload_json, created_at)
               VALUES (?, 'resources', 'stats_engine',
                       'financial.spend.by_category', 'stats_engine_v1', 'topos', ?, ?)""",
            (
                T8_STAT_FACT_ID,
                json.dumps(
                    {
                        "fact_id": T8_STAT_FACT_ID,
                        "dimension": "resources",
                        "source_id": "stats_engine",
                        "record_id": "financial.spend.by_category",
                        "object_type": "stat_insight",
                        "tag": NEEDLES["stat_insight_dated"],
                        "summary_text": NEEDLES["stat_insight_dated"],
                        "stat_summary": {"n": 2, "total": 258.0},
                        "group_key": "kiln",
                        "period_start": T8_STAT_PERIOD_START,
                        "period_end": T8_STAT_PERIOD_END,
                        "confidence": 1.0,
                        "disclosure": "owner_only",
                        "provider": "topos",
                        "model": "stats_engine_v1",
                    }
                ),
                T8_STAT_PERIOD_END,
            ),
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


# --- T5 (B1.2) live-lane supersession canary ------------------------------------------
# The ONLY mutating case in the catalog. Everything it writes is namespaced
# under T5_CANARY_ENTITY_ID / T5_OBJECT_KEY_PREFIX so cleanup is idempotent and
# safe to run at any time. Protocol — the CP runner offers no per-case teardown
# and hands oracle callables a READ-ONLY uri connection, so the canary write
# resolves the DB file via PRAGMA database_list and opens its own connection:
#   1. oracle start: t5_cleanup (removes leftovers from prior/killed runs)
#   2. seed: assert the superseded value, then supersede it at EQUAL confidence
#      (a challenger below incumbent - CONFLICT_CONFIDENCE_MARGIN only queues a
#      fact_conflict and the chain never closes)
#   3. atexit: t5_cleanup again when the eval process exits (best-effort final
#      teardown; step 1 covers crashes)
# Vocabulary is chosen to be collision-free across eval lanes (N10 uses
# "Marzipan Meridian", so no 'meridian' here) and unmatchable by real owner
# queries (linking is impossible: mention_count=0; the fact only surfaces on
# 'probe'/'berth' token overlap).
T5_CANARY_ENTITY_ID = "qq-canary-t5-entity"
T5_CANARY_PREDICATE = "qq_t5_probe_berth"
T5_OBJECT_KEY_PREFIX = f"fact:{T5_CANARY_ENTITY_ID}:"
T5_SUPERSEDED_VALUE = "the Wrenfold Drydock (qq-t5 canary)"
T5_CURRENT_VALUE = "the Osprey Slipway (qq-t5 canary)"

_T5_ATEXIT_PATHS: set = set()


def _db_main_path(conn: sqlite3.Connection) -> Optional[str]:
    """File path behind a connection's 'main' database ('' for :memory:)."""
    try:
        for _seq, name, path in conn.execute("PRAGMA database_list").fetchall():
            if name == "main":
                return str(path) if path else None
    except sqlite3.Error:
        return None
    return None


def t5_cleanup(conn: sqlite3.Connection) -> None:
    """Delete every canary row (fact revisions, queued conflicts, entity).

    Idempotent: keyed on the namespaced object_key prefix / entity id, so it is
    safe on a virgin DB, after a crashed run, or run twice in a row."""
    for sql, params in (
        ("DELETE FROM signal_objects WHERE object_key LIKE ?", (T5_OBJECT_KEY_PREFIX + "%",)),
        ("DELETE FROM fact_conflicts WHERE subject_entity_id = ?", (T5_CANARY_ENTITY_ID,)),
        ("DELETE FROM entities WHERE entity_id = ?", (T5_CANARY_ENTITY_ID,)),
    ):
        try:
            conn.execute(sql, params)
        except sqlite3.OperationalError:
            pass  # table absent (partial schema) — nothing to clean
    conn.commit()


def t5_seed_canary(conn: sqlite3.Connection) -> None:
    """Plant the canary chain through FactStore so supersession runs for real.

    Call t5_cleanup first — re-seeding over a live chain would supersede the
    current value with the old one and scramble the expected revision order."""
    from topos.features.facts.store import FactStore

    conn.execute(
        """INSERT OR IGNORE INTO entities
           (entity_id, entity_type, canonical_name, normalized_name, mention_count,
            first_seen, last_seen)
           VALUES (?, 'person', 'QQ T5 Canary Probe', 'qq t5 canary probe', 0, ?, ?)""",
        (T5_CANARY_ENTITY_ID, _iso(30), _iso(5)),
    )
    store = FactStore(conn)
    store.assert_fact(
        subject_entity_id=T5_CANARY_ENTITY_ID,
        predicate=T5_CANARY_PREDICATE,
        object_value=T5_SUPERSEDED_VALUE,
        dimension="work",
        confidence=0.7,
        valid_from=_iso(30),
    )
    store.assert_fact(
        subject_entity_id=T5_CANARY_ENTITY_ID,
        predicate=T5_CANARY_PREDICATE,
        object_value=T5_CURRENT_VALUE,
        dimension="work",
        confidence=0.7,  # equal confidence: supersession, not a queued conflict
        valid_from=_iso(5),
    )


def _register_t5_atexit(db_path: str) -> None:
    if db_path in _T5_ATEXIT_PATHS:
        return
    _T5_ATEXIT_PATHS.add(db_path)
    import atexit

    def _final_cleanup(path: str = db_path) -> None:
        import os

        if not os.path.exists(path):
            return  # DB gone (seeded tempdir) — nothing to clean, don't recreate
        try:
            conn = sqlite3.connect(path)
        except sqlite3.Error:
            return
        try:
            t5_cleanup(conn)
        finally:
            conn.close()

    atexit.register(_final_cleanup)


def oracle_t5_live_supersession(conn: sqlite3.Connection) -> Oracle:
    """Setup-oracle: plants + supersedes the canary, then states the truth.

    Receives the runner's read-only connection; writes go through a separate
    connection to the same file. Re-invocation (SqlOracle.from_cases re-runs
    oracles for IR metrics) is safe: cleanup-then-seed is idempotent."""
    path = _db_main_path(conn)
    if not path:
        return Oracle([], "T5: eval DB has no file path (in-memory?) — canary not planted", ok=False)
    try:
        rw = sqlite3.connect(path)
    except sqlite3.Error as exc:
        return Oracle([], f"T5: cannot open eval DB read-write: {exc}", ok=False)
    try:
        t5_cleanup(rw)
        t5_seed_canary(rw)
    except sqlite3.Error as exc:
        return Oracle([], f"T5: canary setup failed: {exc}", ok=False)
    finally:
        rw.close()
    _register_t5_atexit(path)
    return Oracle(
        [[T5_SUPERSEDED_VALUE], ["no longer current"]],
        "live supersession: past-tense ask must surface the closed canary revision, marked stale",
    )


# --- T4/T6/T7/T8 oracles ----------------------------------------------------------------


def query_t4_as_of(conn) -> str:
    return f"What was my studio space in {T4_MONTH_LABEL}?"


def oracle_t4_as_of(conn) -> Oracle:
    return Oracle(
        [[NEEDLES["fact_superseded"]]],
        f"as-of {T4_MONTH_LABEL} (inside the superseded window): the point-in-time "
        "value must answer, not the current one",
    )


# Must-NOT-appear contract for T6 (stale-fact suppression). Passed through to
# CompositionCase.poison_groups once that field lands (in-flight sibling change
# to composition_eval_cases); exported so tests and the grader agree on the
# contract either way.
T6_POISON_GROUPS = ((NEEDLES["fact_superseded"],), ("no longer current",))


def oracle_t6_stale_suppression(conn) -> Oracle:
    return Oracle(
        [[NEEDLES["fact_current"]]],
        "present tense: current value only — the superseded value and the stale "
        "marker are poison (see T6_POISON_GROUPS)",
    )


def oracle_t7_edge_validity(conn) -> Oracle:
    return Oracle(
        [[NEEDLES["edge_former_collaborator"]], ["no longer current"]],
        "past-tense relationship: the ended edge must surface AND carry staleness marking",
    )


def oracle_t8_staleness_honesty(conn) -> Oracle:
    return Oracle(
        [[NEEDLES["stat_insight_dated"], "258.00"], list(T8_DATE_NEEDLES)],
        "artifact honesty: the stat's value AND its own period/created date must render",
    )


_CASE_FIELD_NAMES = {f.name for f in dataclasses.fields(CompositionCase)}


def _compat_case(*args, poison_groups=(), **kwargs) -> CompositionCase:
    """CompositionCase constructor tolerant of the in-flight poison_groups field.

    Passes poison_groups through once composition_eval_cases grows the field;
    until then the case grades on its positive oracle alone (the poison
    contract stays declared in T6_POISON_GROUPS)."""
    if poison_groups and "poison_groups" in _CASE_FIELD_NAMES:
        kwargs["poison_groups"] = tuple(tuple(g) for g in poison_groups)
    return CompositionCase(*args, **kwargs)


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
    # --- T4-T8: temporal instrument cases (plan B1.1-B1.5). T5 is the live-lane
    # supersession canary and lives in LIVE_TEMPORAL_CASES below. -------------------
    CompositionCase(
        "T4", "seeded", query_t4_as_of, "work_context:read", "summary",
        oracle_t4_as_of,
        topic_terms=("studio", "larkspur", "annex"),
        layer="facts:as_of", query_class="known_item",
        description="B1.1 as-of point-in-time: the target month lies inside the "
                    "superseded window (chain valid_from dates auto-shift to guarantee "
                    "coverage across month edges). EXPECTED RED: "
                    "FactStore.facts_for_subject(as_of=...) exists but the planner "
                    "never derives an as-of date and _load_fact_store_items never "
                    "passes one — the present-tense fact lane serves the current "
                    "value instead of the point-in-time one."),
    _compat_case(
        "T6", "seeded", "What is my current studio space?", "work_context:read", "summary",
        oracle_t6_stale_suppression,
        topic_terms=("studio", "atelier", "foxglove"),
        poison_groups=T6_POISON_GROUPS,
        layer="facts:stale_suppression", query_class="known_item",
        description="B1.3 stale-fact suppression (poison twin of T1): the superseded "
                    "value and the no-longer-current marker must NOT appear in a "
                    "present-tense answer. Graded via poison_groups once that field "
                    "lands in CompositionCase; until then the positive needle keeps "
                    "the case green and T6_POISON_GROUPS declares the contract. "
                    "Regression tripwire for as-of/temporal work leaking closed "
                    "revisions into present-tense reads."),
    CompositionCase(
        "T7", "seeded", "Who did Maren Oxbow work with before?",
        "relationship_context:read", "summary",
        oracle_t7_edge_validity,
        expected_sources=("entity_graph",),
        topic_terms=("maren", "oxbow", "brindle"),
        layer="edges:validity", query_class="known_item",
        description="B1.4 edge validity — RED BY CONSTRUCTION until B2.2 (SEL-lane "
                    "precedent: instruments a mechanism that does not exist yet): "
                    "entity_edges has UNIQUE(src,dst,type) and no valid_from/valid_to "
                    "(storage/db/migrations/wiki_entities_v1.py), so an ended "
                    "relationship cannot be closed or carry a staleness marker. The "
                    "edge itself surfaces via the top_edges branch of "
                    "entity_context_items (Maren has no dossier); the 'no longer "
                    "current' group stays red. Query avoids 'studio'/'space' tokens "
                    "so the studio-space fact chain's marker cannot false-match."),
    CompositionCase(
        "T8", "seeded", "How much did I spend on kiln firing?", "resources:read", "summary",
        oracle_t8_staleness_honesty,
        expected_sources=("stat_insight",),
        topic_terms=("kiln", "spend", "firing"),
        layer="artifacts:staleness_honesty", query_class="aggregate",
        description="B1.5 staleness honesty: a response carrying a stat artifact must "
                    "render the artifact's OWN period/created date, not just the "
                    "value. The seeded stat carries period_start/period_end in its "
                    "payload and created_at 45d back; _load_stat_insight_items "
                    "renders tag text only, so the date group is EXPECTED RED "
                    "(correctness 0.5) until stat artifacts are date-stamped "
                    "(B2.1/B2.3)."),
]

# T5 (B1.2) runs in the LIVE lane — its oracle mutates the database it is handed
# (namespaced canary chain, idempotent cleanup + atexit teardown; see the T5
# block above). Kept out of SEEDED_COMPOSITION_CASES so the seeded scratch lane
# stays mutation-free; the CP runner (scripts/run_query_quality_eval.py)
# appends this list to LIVE_COMPOSITION_CASES.
LIVE_TEMPORAL_CASES: List[CompositionCase] = [
    CompositionCase(
        "T5", "live", "Where was my probe berth before?", "work_context:read", "summary",
        oracle_t5_live_supersession,
        expected_sources=("fact",),
        topic_terms=("berth", "drydock", "slipway", "canary"),
        layer="facts:live_supersession", query_class="known_item",
        description="B1.2 live-lane supersession: the oracle plants a namespaced "
                    "canary fact through FactStore against the live DB (read-only "
                    "oracle conn -> file path via PRAGMA database_list -> own write "
                    "connection), supersedes it at equal confidence, and the "
                    "past-tense query must surface the closed revision with the "
                    "no-longer-current marker. Cleanup is guaranteed idempotent: "
                    "pre-clean at oracle start (covers crashed runs) + atexit "
                    "teardown; all rows keyed under fact:qq-canary-t5-entity:."),
]
