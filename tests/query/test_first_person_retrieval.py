"""Query-side provenance/temporal mechanisms (PLAN_PROVENANCE_SPLIT P3.3 +
PLAN_NODE_UPGRADE B1): as-of point-in-time fact reads, now= threading,
first-person owner-authored preference, authored-ledger stat selection,
speaker attribution prefixes, and the temporal_shift pass-through to entity
context. Pure sqlite scratch DBs — no models, no network."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import (
    DefaultSignalRetrievalAdapter,
    _apply_first_person_stat_preference,
    _load_canonical_summary_items,
    _load_fact_store_items,
    _load_stat_insight_items,
    _resolve_plan_now,
)
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

_NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    c = sqlite3.connect(str(tmp_path / "fp.db"))
    apply_all_migrations(c)
    ensure_all_tables(c)  # conversations + conversation_messages
    CanonicalTablesManager(c)  # ai_chat tables
    yield c
    c.close()


def _plan(**flags):
    base = dict(
        first_person_intent=False,
        first_person_belief=False,
        interaction_browse=False,
        temporal_shift=None,
        as_of=None,
        dimensions=[],
        time_range=None,
        semantic_residual="",
    )
    base.update(flags)
    return SimpleNamespace(**base)


def _seed_stat(conn, fact_id, record_id, tag, *, dimension="relationships",
               group_key="thread-1", created_at="2026-06-01T00:00:00Z",
               extra_payload=None):
    payload = {
        "fact_id": fact_id,
        "dimension": dimension,
        "source_id": "stats_engine",
        "record_id": record_id,
        "object_type": "stat_insight",
        "tag": tag,
        "summary_text": tag,
        "stat_summary": {"n": 5},
        "group_key": group_key,
        "confidence": 1.0,
        "disclosure": "owner_only",
        "provider": "topos",
        "model": "stats_engine_v1",
    }
    payload.update(extra_payload or {})
    conn.execute(
        """INSERT INTO signal_facts
           (fact_id, dimension, source_id, record_id, model, provider, payload_json, created_at)
           VALUES (?, ?, 'stats_engine', ?, 'stats_engine_v1', 'topos', ?, ?)""",
        (fact_id, dimension, record_id, json.dumps(payload), created_at),
    )
    conn.commit()


def _seed_messages(conn):
    """One owner row + one contact row (with resolvable display name), both
    matching the token 'updates'."""
    conn.execute(
        """INSERT INTO conversation_messages
           (message_id, conversation_id, dataset_id, sender_type, sender_id, content,
            event_at, source_id, is_from_self)
           VALUES ('msg-self', 'conv-1', 'd', 'human', 'self',
                   'I posted the updates to the shared sheet myself',
                   '2026-07-01T10:00:00Z', 'demo_messenger_file', 1)"""
    )
    conn.execute(
        """INSERT INTO conversation_messages
           (message_id, conversation_id, dataset_id, sender_type, sender_id, content,
            event_at, source_id, is_from_self)
           VALUES ('msg-bram', 'conv-1', 'd', 'contact', 'bram-7',
                   'the updates look wrong to me, redo them',
                   '2026-07-02T10:00:00Z', 'demo_messenger_file', 0)"""
    )
    conn.execute(
        """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
           VALUES ('c-bram', 'd', 'demo_contacts_file', 'Bram Holloway', 0)"""
    )
    conn.execute(
        """INSERT INTO contact_identifiers
           (dataset_id, source_id, identifier, identifier_type, contact_id)
           VALUES ('d', 'demo_contacts_file', 'bram-7', 'phone', 'c-bram')"""
    )
    conn.commit()


# --- now= threading ---------------------------------------------------------------------


class TestNowThreading:
    def test_request_now_wins(self) -> None:
        req = RetrievalRequest(manifest=None, access_mode="summary", now=_NOW)
        assert _resolve_plan_now(req) == _NOW

    def test_iso_string_and_env(self, monkeypatch) -> None:
        req = RetrievalRequest(manifest=None, access_mode="summary", now="2026-03-01T00:00:00Z")
        resolved = _resolve_plan_now(req)
        assert resolved is not None and resolved.month == 3 and resolved.tzinfo is not None
        monkeypatch.setenv("TOPOS_QUERY_NOW", "2025-12-31T23:00:00+00:00")
        env_req = RetrievalRequest(manifest=None, access_mode="summary")
        env_resolved = _resolve_plan_now(env_req)
        assert env_resolved is not None and env_resolved.year == 2025

    def test_default_is_wall_clock(self) -> None:
        assert _resolve_plan_now(RetrievalRequest(manifest=None, access_mode="summary")) is None

    def test_planner_receives_injected_now(self, conn, monkeypatch) -> None:
        """End to end through retrieve(): the injected now decides which year a
        bare month resolves to, visible in the query-plan metadata."""
        import topos.core.state as state_mod

        monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        adapter = DefaultSignalRetrievalAdapter(bundle)
        manifest = resolve_scope_manifest("work_context:read")
        result = adapter.retrieve(
            RetrievalRequest(
                manifest=manifest,
                access_mode="summary",
                query_text="What was my studio space in January?",
                disclosure_tier="owner_raw",
                now=datetime(2026, 1, 15, tzinfo=timezone.utc),
            )
        )
        plan_meta = result.retrieval_metadata.get("query_plan") or {}
        assert plan_meta.get("as_of") == "2025-01-31"


# --- as-of point-in-time fact reads (B1.1 / T4) ------------------------------------------


class TestAsOfFactLane:
    def _seed_chain(self, conn):
        from topos.features.facts.store import FactStore

        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count, is_self)
               VALUES ('self-1', 'person', 'Owner', 'owner', 1, 1)"""
        )
        store = FactStore(conn)
        store.assert_fact(
            subject_entity_id="self-1", predicate="studio space",
            object_value="the Larkspur Annex", dimension="work",
            valid_from="2026-04-01T00:00:00Z",
        )
        store.assert_fact(
            subject_entity_id="self-1", predicate="studio space",
            object_value="the Foxglove Atelier", dimension="work",
            valid_from="2026-06-20T00:00:00Z",
        )

    def test_as_of_serves_point_in_time_value_without_stale_marker(self, conn) -> None:
        self._seed_chain(conn)
        manifest = resolve_scope_manifest("work_context:read")
        items = _load_fact_store_items(
            conn, "What was my studio space in May 2026?", [],
            disclosure_tier="owner_raw", manifest=manifest, as_of="2026-05-31",
        )
        blob = " ".join(i["summary_text"] for i in items)
        assert "the Larkspur Annex" in blob
        assert "the Foxglove Atelier" not in blob  # not yet true at as_of
        assert "no longer current" not in blob  # it WAS current at as_of

    def test_past_shift_without_as_of_keeps_stale_marker(self, conn) -> None:
        self._seed_chain(conn)
        manifest = resolve_scope_manifest("work_context:read")
        items = _load_fact_store_items(
            conn, "Where was my studio space before?", [],
            disclosure_tier="owner_raw", manifest=manifest, temporal_shift="past",
        )
        blob = " ".join(i["summary_text"] for i in items)
        assert "the Larkspur Annex" in blob and "no longer current" in blob

    def test_present_tense_unchanged(self, conn) -> None:
        self._seed_chain(conn)
        manifest = resolve_scope_manifest("work_context:read")
        items = _load_fact_store_items(
            conn, "What is my studio space?", [],
            disclosure_tier="owner_raw", manifest=manifest,
        )
        blob = " ".join(i["summary_text"] for i in items)
        assert "the Foxglove Atelier" in blob
        assert "the Larkspur Annex" not in blob


# --- stat selection (contract 5 / IMB6 / T8) ---------------------------------------------


class TestStatSelection:
    def _seed_pair(self, conn):
        _seed_stat(
            conn, "stat:messages.volume.sent.by_thread:thread-1",
            "messages.volume.sent.by_thread", "You sent 23 messages in Harbor Collective.",
        )
        _seed_stat(
            conn, "stat:messages.volume.by_thread:thread-1",
            "messages.volume.by_thread", "Harbor Collective total thread volume: 2000 messages.",
        )

    def test_first_person_prefers_sent_and_drops_volume_twin(self, conn) -> None:
        self._seed_pair(conn)
        manifest = resolve_scope_manifest("messages:read")
        items = _load_stat_insight_items(
            conn, "How many messages have I sent in the Harbor Collective group?",
            disclosure_tier="owner_raw", manifest=manifest, first_person=True,
        )
        blob = " ".join(i["summary_text"] for i in items)
        assert "You sent 23 messages" in blob
        assert "total thread volume" not in blob

    def test_general_query_keeps_both(self, conn) -> None:
        self._seed_pair(conn)
        manifest = resolve_scope_manifest("messages:read")
        items = _load_stat_insight_items(
            conn, "How many messages are in the Harbor Collective group?",
            disclosure_tier="owner_raw", manifest=manifest, first_person=False,
        )
        blob = " ".join(i["summary_text"] for i in items)
        assert "You sent 23 messages" in blob and "total thread volume" in blob

    def test_exposure_ledger_dropped_when_first_person(self, conn) -> None:
        _seed_stat(
            conn, "stat:activity.topics:knives", "activity.topics",
            "Reading exposure: knife reviews weekly.",
            extra_payload={"ledger": "exposure"},
        )
        manifest = resolve_scope_manifest("messages:read")
        gated = _load_stat_insight_items(
            conn, "how often do I read knife reviews — my interests?",
            disclosure_tier="owner_raw", manifest=manifest, first_person=True,
        )
        assert gated == []
        kept = _load_stat_insight_items(
            conn, "how often do I read knife reviews",
            disclosure_tier="owner_raw", manifest=manifest, first_person=False,
        )
        assert kept, "exposure stat must stay reachable for non-first-person asks"

    def test_exposure_ledger_nested_in_stat_summary_also_gates(self, conn) -> None:
        """Live promote path: STAT_PAYLOADS merges into the insight summary
        (stats/insights.py _summary_with_payload), so promoted facts carry the
        marker at stat_summary.ledger, not top-level. Both channels must gate."""
        _seed_stat(
            conn, "stat:activity.visits.by_title:knives", "activity.visits.by_title",
            "Reading exposure: knife reviews weekly.",
            extra_payload={"stat_summary": {"n": 5, "ledger": "exposure"}},
        )
        manifest = resolve_scope_manifest("messages:read")
        gated = _load_stat_insight_items(
            conn, "how often do I read knife reviews — my interests?",
            disclosure_tier="owner_raw", manifest=manifest, first_person=True,
        )
        assert gated == []
        kept = _load_stat_insight_items(
            conn, "how often do I read knife reviews",
            disclosure_tier="owner_raw", manifest=manifest, first_person=False,
        )
        assert kept, "exposure stat must stay reachable for non-first-person asks"

    def test_stat_summary_carries_artifact_date(self, conn) -> None:
        """T8 staleness honesty: period_end preferred, created_at fallback."""
        _seed_stat(
            conn, "stat:financial.spend.by_category:kiln",
            "financial.spend.by_category", "Total kiln-firing spend: 258.00.",
            dimension="resources", created_at="2026-05-25T00:00:00Z",
            extra_payload={
                "period_start": "2026-05-17T00:00:00Z",
                "period_end": "2026-05-24T00:00:00Z",
            },
        )
        _seed_stat(
            conn, "stat:financial.spend.by_category:kayak",
            "financial.spend.by_category:kayak", "Total kayak spend: 412.00.",
            dimension="resources", created_at="2026-04-02T09:00:00Z",
        )
        manifest = resolve_scope_manifest("resources:read")
        items = _load_stat_insight_items(
            conn, "how much did I spend on kiln firing and kayaks",
            disclosure_tier="owner_raw", manifest=manifest,
        )
        by_id = {i["record_id"]: i["summary_text"] for i in items}
        assert "2026-05-24" in by_id["stat:financial.spend.by_category:kiln"]
        assert "2026-04-02" in by_id["stat:financial.spend.by_category:kayak"]
        # topic stays the clean tag (needle prefixes unaffected)
        topics = {i["record_id"]: i["topic"] for i in items}
        assert "(as of" not in topics["stat:financial.spend.by_category:kiln"]

    def test_preference_helper_passes_non_stats_through(self) -> None:
        entries = [
            {"record_id": "messages.volume.sent.by_thread", "group_key": "t",
             "object_type": "stat_insight"},
            {"record_id": "messages.volume.by_thread", "group_key": "t",
             "object_type": "stat_insight"},
            {"record_id": "messages.volume.by_thread", "group_key": "OTHER",
             "object_type": "stat_insight"},  # different group: no shadow
            {"record_id": "rec-1", "summary_text": "a plain item"},
        ]
        kept = _apply_first_person_stat_preference(entries)
        families = [(e.get("record_id"), e.get("group_key")) for e in kept]
        assert ("messages.volume.by_thread", "t") not in families
        assert ("messages.volume.by_thread", "OTHER") in families
        assert ("rec-1", None) in families or any(e.get("record_id") == "rec-1" for e in kept)


# --- speaker attribution + owner-first canonical lane (P3.3) -----------------------------


class TestSpeakerAttribution:
    def test_first_person_prefixes_non_owner_and_ranks_owner_first(self, conn) -> None:
        _seed_messages(conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        manifest = resolve_scope_manifest("messages:read")
        items = _load_canonical_summary_items(
            manifest=manifest, adapters=bundle, query_text="updates",
            source_ids=["demo_messenger_file"], disclosure_tier="owner_raw",
            plan=_plan(first_person_intent=True), conn=conn,
        )
        texts = [i["summary_text"] for i in items]
        assert any(t.startswith("[Bram Holloway] ") for t in texts)
        owner_texts = [t for t in texts if "myself" in t]
        assert owner_texts and not owner_texts[0].startswith("[")
        # owner-authored row re-ranked first
        assert "myself" in texts[0]

    def test_belief_intent_hard_filters_non_owner_rows(self, conn) -> None:
        _seed_messages(conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        manifest = resolve_scope_manifest("messages:read")
        items = _load_canonical_summary_items(
            manifest=manifest, adapters=bundle, query_text="updates",
            source_ids=["demo_messenger_file"], disclosure_tier="owner_raw",
            plan=_plan(first_person_intent=True, first_person_belief=True), conn=conn,
        )
        blob = " ".join(i["summary_text"] for i in items)
        assert "myself" in blob and "redo them" not in blob

    def test_general_recall_is_untouched(self, conn) -> None:
        _seed_messages(conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        manifest = resolve_scope_manifest("messages:read")
        items = _load_canonical_summary_items(
            manifest=manifest, adapters=bundle, query_text="updates",
            source_ids=["demo_messenger_file"], disclosure_tier="owner_raw",
        )
        texts = [i["summary_text"] for i in items]
        assert len(texts) == 2
        assert not any(t.startswith("[") for t in texts)  # no prefixes
        assert not any("owner_authored" in i for i in items)

    def test_assistant_rows_prefixed_for_first_person_ai_chat(self, conn) -> None:
        conn.execute(
            """INSERT INTO ai_chat_messages
               (message_id, conversation_id, sender_type, source_id, content, event_at)
               VALUES ('ai-u', 'ac-1', 'user', 'chatgpt_ingestion',
                       'I prefer linen workwear as my style', '2026-07-01T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO ai_chat_messages
               (message_id, conversation_id, sender_type, source_id, content, event_at)
               VALUES ('ai-a', 'ac-1', 'assistant', 'chatgpt_ingestion',
                       'you clearly prefer a raw-denim style', '2026-07-01T00:01:00Z')"""
        )
        conn.commit()
        bundle = AdapterFactory.create("local_database", conn=conn)
        manifest = resolve_scope_manifest("ai_conversations:read")
        items = _load_canonical_summary_items(
            manifest=manifest, adapters=bundle, query_text="style",
            source_ids=["chatgpt_ingestion"], disclosure_tier="owner_raw",
            plan=_plan(first_person_intent=True), conn=conn,
        )
        texts = [i["summary_text"] for i in items]
        assert any(t.startswith("[assistant] ") for t in texts)
        assert any("linen workwear" in t and not t.startswith("[") for t in texts)


# --- temporal_shift pass-through to entity context (T7) ----------------------------------


class TestEntityContextPassThrough:
    def _seed_entity(self, conn):
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count)
               VALUES ('ent-m', 'person', 'Maren Oxbow', 'maren oxbow', 3)"""
        )
        conn.commit()

    def _retrieve(self, conn, monkeypatch, recorder):
        import topos.core.state as state_mod
        import topos.features.entities.linking as linking_mod

        monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
        monkeypatch.setattr(linking_mod, "entity_context_items", recorder)
        bundle = AdapterFactory.create("local_database", conn=conn)
        adapter = DefaultSignalRetrievalAdapter(bundle)
        manifest = resolve_scope_manifest("relationship_context:read")
        return adapter.retrieve(
            RetrievalRequest(
                manifest=manifest, access_mode="summary",
                query_text="Who did Maren Oxbow work with before?",
                disclosure_tier="owner_raw",
            )
        )

    def test_temporal_shift_kwarg_reaches_linking(self, conn, monkeypatch) -> None:
        self._seed_entity(conn)
        seen = {}

        def recorder(conn_, linked, *, max_per_entity=4, temporal_shift=None):
            seen["temporal_shift"] = temporal_shift
            return []

        self._retrieve(conn, monkeypatch, recorder)
        assert seen.get("temporal_shift") == "past"

    def test_pre_m1_linking_signature_falls_back(self, conn, monkeypatch) -> None:
        """A linking module without the kwarg must not break retrieval."""
        self._seed_entity(conn)
        calls = []

        def legacy(conn_, linked, *, max_per_entity=4):
            calls.append(True)
            return []

        result = self._retrieve(conn, monkeypatch, legacy)
        assert calls, "legacy entity_context_items was never invoked"
        assert result.error is None


# --- interaction browse (IMB7) -----------------------------------------------------------


class TestInteractionBrowse:
    def test_contacts_answer_who_do_i_talk_to(self, conn, monkeypatch) -> None:
        import topos.core.state as state_mod

        for cid, name, is_self in (
            ("c-1", "Bram Holloway", 0), ("c-2", "Saskia Vreeland", 0), ("c-0", "Owner", 1),
        ):
            conn.execute(
                """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
                   VALUES (?, 'd', 'demo_contacts_file', ?, ?)""",
                (cid, name, is_self),
            )
        conn.commit()
        monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        adapter = DefaultSignalRetrievalAdapter(bundle)
        manifest = resolve_scope_manifest("relationship_context:read")
        result = adapter.retrieve(
            RetrievalRequest(
                manifest=manifest, access_mode="summary",
                query_text="Who are the people I talk to and interact with?",
                disclosure_tier="owner_raw",
            )
        )
        summaries = result.context_packet.get("summaries") or []
        blob = " ".join(str(s.get("summary_text")) for s in summaries)
        assert "Bram Holloway" in blob and "Saskia Vreeland" in blob
        assert "Owner" not in blob  # is_self contact excluded

    def test_edges_answer_without_contacts_lane(self, conn, monkeypatch) -> None:
        """P3.2 / B7: co-participation edges answer IMB7 without contacts."""
        import topos.core.state as state_mod

        from topos.features.entities.edges import EDGE_COMMUNICATES, update_edge

        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, mention_count, is_self)
               VALUES ('e-self', 'person', 'Owner', 'owner', 1, 1)"""
        )
        for eid, name in (
            ("e-bram", "Bram Holloway"),
            ("e-saskia", "Saskia Vreeland"),
            ("e-odile", "Odile Ferrant"),
        ):
            conn.execute(
                """INSERT INTO entities
                   (entity_id, entity_type, canonical_name, normalized_name, mention_count, is_self)
                   VALUES (?, 'person', ?, ?, 1, 0)""",
                (eid, name, name.lower()),
            )
        # Real partners via communicates_with; Odile only via co_occurrence (mention).
        update_edge(conn, src_entity_id="e-self", dst_entity_id="e-bram",
                    edge_type=EDGE_COMMUNICATES, event_at="2026-01-01T00:00:00Z")
        update_edge(conn, src_entity_id="e-self", dst_entity_id="e-saskia",
                    edge_type=EDGE_COMMUNICATES, event_at="2026-01-01T00:00:00Z")
        update_edge(conn, src_entity_id="e-bram", dst_entity_id="e-odile",
                    edge_type="co_occurrence", event_at="2026-01-01T00:00:00Z")
        conn.commit()
        monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        adapter = DefaultSignalRetrievalAdapter(bundle)
        manifest = resolve_scope_manifest("relationship_context:read")
        result = adapter.retrieve(
            RetrievalRequest(
                manifest=manifest, access_mode="summary",
                query_text="Who are the people I talk to and interact with?",
                disclosure_tier="owner_raw",
            )
        )
        summaries = result.context_packet.get("summaries") or []
        blob = " ".join(str(s.get("summary_text")) for s in summaries)
        assert "Bram Holloway" in blob and "Saskia Vreeland" in blob
        assert "Odile Ferrant" not in blob
        assert any(
            str(s.get("retrieval_source") or "").startswith("entity_edge:communicates_with")
            for s in summaries
        )
