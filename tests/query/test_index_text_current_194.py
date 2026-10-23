"""Lane 194: below the owner's tier an index hit's stored text is served only while it is still the row's text.

The vector and recent lanes hand a grantee the text `signal_embeddings` stored when the row was embedded. A row
rewritten since (an edit, a re-capture, a re-import, a merged reply) keeps that old copy until something re-embeds
it, so a grantee could read words the owner had changed (DR-34 default-on review, 22 Oct 2026). The gate
`_index_hits_inside_grant` now keeps a hit only when its `content_hash` equals the embeddings job's hash of the row's
current text. The owner's tier is unchanged. Every line here is invented.
"""
from __future__ import annotations

import hashlib

import pytest

from topos.features.signal.embed_context import embeddable_content
from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import _index_hits_inside_grant, _index_text_current
from topos.storage.adapters.fakes import InMemoryCanonicalStore

NOW_TEXT = "Weekend grocery list with apples bread and oat milk."
OLD_TEXT = "I am sketching a talk about tide pools for the spring open day."


def _hash(text: str) -> str:
    return hashlib.sha256(embeddable_content({"content": text}).encode("utf-8")).hexdigest()


@pytest.fixture
def store():
    store = InMemoryCanonicalStore()
    store.upsert("ai_chat_messages", {"record_id": "edited", "content": NOW_TEXT, "content_nsfw": 0})
    store.upsert("ai_chat_messages", {"record_id": "same", "content": NOW_TEXT, "content_nsfw": 0})
    return store


MANIFEST = ScopeResolutionManifest(scope_id="ai_conversations:read", primary_dimensions=[],
                                   canonical_tables=["ai_chat_messages"])


def _hits(*pairs):
    return [{"record_id": rid, "record_type": "ai_chat_message", "content_hash": h} for rid, h in pairs]


def test_a_hit_embedded_from_text_the_row_no_longer_holds_is_not_served_to_a_grantee(store):
    hits = _hits(("edited", _hash(OLD_TEXT)), ("same", _hash(NOW_TEXT)))
    kept = _index_hits_inside_grant(hits, conn=None, manifest=MANIFEST, disclosure_tier="default_disclosure",
                                    canonical=store)
    assert [h["record_id"] for h in kept] == ["same"]


def test_the_owner_tier_is_unchanged(store):
    hits = _hits(("edited", _hash(OLD_TEXT)), ("same", _hash(NOW_TEXT)))
    assert _index_hits_inside_grant(hits, conn=None, manifest=MANIFEST, disclosure_tier="owner_raw",
                                    canonical=store) == hits


def test_the_owner_summary_packet_keeps_every_hit_and_carries_no_hash(monkeypatch):
    """The hash rides on the hit only as far as the gate: the owner's packet is the one it was before lane 194
    (every hit, its stored text, no `content_hash`), and a grantee's carries none either."""
    from topos.features.signal import service as signal_service
    from topos.features.signal.derived_index import DERIVED_SOURCE_ID
    from topos.query import retrieval as R
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterBundle
    from topos.storage.adapters.fakes import (InMemoryAuditLogStore, InMemoryGraphEdgeStore,
                                              InMemoryQuerySessionStore, InMemorySignalFeatureStore,
                                              InMemoryVectorIndex)

    hit = {"record_id": "same", "record_type": "ai_chat_message", "similarity": 0.8, "source_id": "chatgpt_file_ingestion",
           "signal_dimension": "memory", "event_at": "2026-10-01T10:00:00Z", "text_preview": NOW_TEXT,
           "search_text": NOW_TEXT, "content_hash": _hash(NOW_TEXT)}

    class _OneHit:
        def search_vectors(self, **kwargs):
            if kwargs.get("source_id") == DERIVED_SOURCE_ID:
                return {"items": [], "total": 0}
            return {"items": [dict(hit)], "total": 1}

    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: _OneHit())
    monkeypatch.setattr(R, "_load_ranked_clusters_unfiltered", lambda *a, **k: [])
    canonical = InMemoryCanonicalStore()
    canonical.upsert("ai_chat_messages", {"record_id": "same", "message_id": "same", "content": NOW_TEXT,
                                          "content_nsfw": 0, "source_id": "chatgpt_file_ingestion"})
    bundle = AdapterBundle(canonical=canonical, signal=InMemorySignalFeatureStore(), vector=InMemoryVectorIndex(),
                           graph=InMemoryGraphEdgeStore(), audit=InMemoryAuditLogStore(),
                           query_session=InMemoryQuerySessionStore(), backend="memory")
    for tier in ("owner_raw", "default_disclosure"):
        packet = R.DefaultSignalRetrievalAdapter(bundle).retrieve(RetrievalRequest(
            manifest=MANIFEST, access_mode="summary", query_text="grocery list", disclosure_tier=tier,
            owner_mode=True)).context_packet
        hits = packet.get("semantic_hits") or []
        assert [h.get("record_id") for h in hits] == ["same"], (tier, packet)
        assert all("content_hash" not in h for h in hits), tier
        assert hits[0].get("text_preview") == NOW_TEXT, tier


# Rows whose `content` is empty: every ingest path hands the embeddings job the row as `prepare_signal_record`
# renders it (a one-line summary: the title alone, "[category] title", "@ place"), while the activity reload path
# hands it the bare row (its descriptive fields joined with " — "). Both are the row's current fields, so a current
# row must pass with the hash of either; before this check only the bare rendering counted and every such row
# (activity:read, schedule:read, work_context:read, public_bio:read) lost its vector and recent lanes unedited.
CONTENTLESS = {
    "browser_visit": ({"event_id": "v1", "activity_type": "visit", "url": "https://example.test/quillfeather",
                       "title": "Quillfeather field guide", "content": None, "hostname": "example.test",
                       "metadata_json": None, "occurred_at": "2026-10-01T00:00:00Z", "source_id": "browser"}, "title"),
    "calendar_with_description": ({"event_id": "c2", "title": "Kite repair", "description": "bring the spare spars",
                                   "starts_at": "2026-10-01", "source_id": "cal"}, "title"),
    "calendar_with_category": ({"event_id": "c3", "title": "Kite repair", "category": "hobby",
                                "starts_at": "2026-10-01", "source_id": "cal"}, "title"),
    "profile_with_organization": ({"record_id": "p1", "record_type": "job", "title": "Lamp maker",
                                   "organization": "Moth and Wick", "description": "Built lamps.",
                                   "source_id": "li"}, "title"),
    "location": ({"event_id": "l1", "place_name": "Tide pool cafe", "city": "Seaford",
                  "event_at": "2026-10-01", "source_id": "loc"}, "place_name"),
}


def _job_hashes(row):
    """The hash the job stores after a prepared ingest, and the one it stores after a bare activity reload."""
    from topos.enrichment.jobs.canonical.brief_fallback import prepare_signal_record

    texts = (embeddable_content(prepare_signal_record(dict(row))), embeddable_content(dict(row)))
    return {hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts if t}


@pytest.mark.parametrize("shape", list(CONTENTLESS), ids=list(CONTENTLESS))
def test_a_current_row_without_content_passes_with_either_hash_the_job_stores(shape):
    row, edited_field = CONTENTLESS[shape]
    stored = _job_hashes(row)
    assert len(stored) == 2, "the two renderings differ for this shape, or the case proves nothing"
    assert all(_index_text_current(dict(row), h) is True for h in stored)
    edited = {**row, edited_field: "Something else entirely"}
    assert all(_index_text_current(edited, h) is False for h in stored)


def test_a_current_browser_visit_reaches_a_grantee_of_the_activity_scope():
    row, _ = CONTENTLESS["browser_visit"]
    store = InMemoryCanonicalStore()
    store.upsert("activity_events", {**row, "record_id": row["event_id"], "content_nsfw": 0})
    manifest = ScopeResolutionManifest(scope_id="activity:read", primary_dimensions=[],
                                       canonical_tables=["activity_events"])
    hits = [{"record_id": "v1", "record_type": "activity_event", "content_hash": h} for h in sorted(_job_hashes(row))]
    kept = _index_hits_inside_grant(hits, conn=None, manifest=manifest, disclosure_tier="default_disclosure",
                                    canonical=store)
    assert [h["record_id"] for h in kept] == ["v1", "v1"]
    assert all("content_hash" not in h for h in kept)


@pytest.mark.parametrize("stored_hash", [None, "", 7, "0" * 64], ids=["none", "empty", "not_text", "other"])
def test_a_hit_without_the_current_hash_is_dropped(store, stored_hash):
    kept = _index_hits_inside_grant(_hits(("same", stored_hash)), conn=None, manifest=MANIFEST,
                                    disclosure_tier="default_disclosure", canonical=store)
    assert kept == []


def test_a_row_with_no_embeddable_text_matches_nothing():
    assert _index_text_current({"content": ""}, _hash("")) is False
    assert _index_text_current(None, _hash(NOW_TEXT)) is False
    assert _index_text_current({"content": NOW_TEXT}, _hash(NOW_TEXT)) is True


def test_the_recent_lane_refuses_a_grantee_when_the_index_has_no_content_hash_column():
    import sqlite3

    from topos.query.retrieval import _load_recent_summary_items

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE signal_embeddings (embedding_id TEXT, record_id TEXT, source_id TEXT, "
                 "signal_dimension TEXT, text_preview TEXT, event_at TEXT, chunk_index INTEGER, record_type TEXT)")
    conn.execute("INSERT INTO signal_embeddings VALUES ('e','edited','chatgpt_file_ingestion','x',?, ?, 0, "
                 "'ai_chat_message')", (OLD_TEXT, "2026-10-21T10:00:00Z"))
    try:
        assert _load_recent_summary_items(conn, disclosure_tier="default_disclosure", manifest=MANIFEST) == []
    finally:
        conn.close()  # an open connection collected inside a later test breaks that test's own sqlite calls
