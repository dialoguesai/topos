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
