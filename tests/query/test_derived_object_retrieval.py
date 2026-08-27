"""The derived layer's objects, reached by MEANING rather than by a curated alias.

Measured on the first live node (2026-08-26): every one of 9,213 signal_objects
was absent from `signal_embeddings`, so the vector/FTS lane could only ever hand
back raw source records. A derived answer was reachable only through
`query/facts_direct.py`, whose `_ALIASES` is a hand-written regex per question —
so "Who's in my close circle?" returned a place called *Hood Circle* and three
messages about shops being close by, while 216 RelationshipEdge rows naming the
actual people sat one table away.

Every test here goes WRITE-THROUGH-READ: it runs the indexer and then asks the
retrieval path, never asserting on the written row alone. Writing is not
reading, and in this codebase that gap is the recurring defect — an index the
producer fills and no lane queries looks exactly like a working feature from the
writer's side.

The tier tests are the other half. The derived lane names people, and it must
not name them to anyone but the owner: `relationship_context:read` declares
`relationship_edges` in its `signal_objects`, so `_fact_disclosure_allowed`
ALLOWS a grantee — measured, not assumed — and the grantee scrub does not save
it (`_redact_pii` removes emails and phone numbers, never names).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List

import pytest

from topos.features.signal.derived_index import index_derived_objects
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

SCOPE = "relationship_context:read"
QUERY = "who is in my close circle"

#: Words the fake encoder scores on. Real cosine over a 384-dim MiniLM is not
#: reproducible in a unit test; what IS testable is that the lane carries
#: whatever the encoder ranked highest through to the caller.
_VOCAB = (
    "close", "circle", "personal", "family", "friend", "work", "professional",
    "grandma", "who", "my", "sushi", "thrift", "store",
)


class _BagOfWordsEncoder:
    """Cosine over a tiny fixed vocabulary — deterministic and inspectable."""

    def run_inference(self, payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        texts = payload.get("texts")
        if texts is None:
            single = payload.get("text") or payload.get("content") or ""
            texts = [single] if single else []
        vectors = [self._vector(str(t)) for t in texts]
        return {
            "vectors": vectors,
            "dims": len(_VOCAB),
            "model": config.get("model") or "fake",
            "provider": "fake",
            "normalized": True,
        }

    @staticmethod
    def _vector(text: str) -> List[float]:
        lowered = text.lower()
        raw = [1.0 if word in lowered else 0.0 for word in _VOCAB]
        norm = sum(v * v for v in raw) ** 0.5 or 1.0
        return [v / norm for v in raw]


def _add_object(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    object_key: str,
    payload: Dict[str, Any],
    dimension: str = "relationships",
) -> str:
    object_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO signal_objects (
            object_id, signal_dimension, object_type, object_key, payload_json,
            confidence, source_refs_json, valid_from, valid_to, extractor_version,
            created_at, updated_at, created_by
        ) VALUES (?, ?, ?, ?, ?, 0.8, '[]', '2026-08-01', NULL, 'v1',
                  '2026-08-01', '2026-08-01', 'system')
        """,
        (object_id, dimension, object_type, object_key, json.dumps(payload)),
    )
    conn.commit()
    return object_id


def _add_raw_embedding(conn: sqlite3.Connection, record_id: str, text: str) -> None:
    """A raw source row in the SAME index, so the split is exercised rather than
    trivially satisfied by there being nothing else to split from."""
    from topos.storage.adapters.sqlite.stores import SQLiteVectorIndex

    SQLiteVectorIndex(conn).upsert(
        {
            "record_id": record_id,
            "source_id": "imessage",
            "signal_dimension": "relationships",
            "model": "fake",
            "dims": len(_VOCAB),
            "text_preview": text,
            "search_text": text,
            "record_type": "conversation_message",
            "chunk_index": 0,
        },
        vector=_BagOfWordsEncoder._vector(text),
    )


@pytest.fixture()
def node(tmp_path, monkeypatch, pin_db_path):
    """A migrated database that is also the GLOBAL one.

    `_bundle_is_global_db` refuses to run the vector layers when the query's
    bundle points at a different file than the global connection — so a test
    that skipped this would exercise a retrieval path with the vector lane
    silently switched off and prove nothing.
    """
    db_path = tmp_path / "derived_retrieval.db"
    conn = sqlite3.connect(str(db_path))
    apply_all_migrations(conn)
    pin_db_path(db_path)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda *a, **k: conn)
    monkeypatch.setattr(
        "topos.engine.backends.huggingface.HuggingFaceAdapter",
        _BagOfWordsEncoder,
        raising=True,
    )
    monkeypatch.setattr(
        "topos.features.signal.query_embed_cache.get_cached_query_embedding",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "topos.features.signal.query_embed_cache.set_cached_query_embedding",
        lambda *a, **k: None,
    )
    # The cross-encoder is a separate model and a separate question; the lane's
    # reach must not depend on it loading.
    monkeypatch.setenv("TOPOS_RERANK", "off")
    monkeypatch.setenv("TOPOS_EMBED_MODEL", "fake")
    yield conn
    conn.close()


def _seed_close_circle(conn: sqlite3.Connection) -> None:
    _add_object(
        conn,
        object_type="RelationshipEdge",
        object_key="grandma",
        payload={
            "target_entity_key": "grandma",
            "tier": "personal",
            "warmth_band": "medium",
            "cadence_band": "recent",
        },
    )
    _add_raw_embedding(
        conn, "imessage:1", "there are really good thrift stores close to the convent"
    )
    _add_raw_embedding(conn, "imessage:2", "this place has cheap sushi and is very close")


def _retrieve(conn: sqlite3.Connection, *, disclosure_tier: str = "owner_raw", query: str = QUERY):
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
    return adapter.retrieve(
        RetrievalRequest(
            manifest=resolve_scope_manifest(SCOPE),
            access_mode="summary",
            query_text=query,
            installed_source_ids=["imessage"],
            disclosure_tier=disclosure_tier,
        )
    )


def _derived_items(bundle) -> List[Dict[str, Any]]:
    return [
        item
        for item in (bundle.context_packet.get("summaries") or [])
        if str(item.get("retrieval_source") or "").startswith("derived:")
    ]


class TestWritingIsNotReading:
    def test_an_indexed_edge_comes_back_named_from_a_question_that_never_names_it(
        self, node
    ) -> None:
        _seed_close_circle(node)
        assert index_derived_objects(node)["written"] == 1

        bundle = _retrieve(node)
        items = _derived_items(bundle)
        assert items, (
            "the derived lane returned nothing. The row exists — the write half "
            "passed — so the read half is where this broke, which is the exact "
            "failure this test is here to catch"
        )
        assert any("Grandma" in str(item.get("summary_text") or "") for item in items), (
            f"no name came back: {[i.get('summary_text') for i in items]!r}"
        )
        assert any("personal circle" in str(item.get("summary_text") or "").lower() for item in items)

    def test_the_fusion_path_declares_the_lane_it_read(self, node) -> None:
        """`stores_touched` is the caller's only evidence that a store was
        consulted. A lane that contributes items without appearing here is
        unauditable, and one that appears here without contributing is a lie."""
        _seed_close_circle(node)
        index_derived_objects(node)
        bundle = _retrieve(node)
        assert "derived_objects" in bundle.stores_touched, bundle.stores_touched
        assert _derived_items(bundle)

    def test_no_indexed_objects_means_no_lane_and_no_claim(self, node) -> None:
        """The control. Without it, a `stores_touched` entry that is appended
        unconditionally would satisfy the test above."""
        _add_raw_embedding(node, "imessage:1", "cheap sushi close by")
        bundle = _retrieve(node)
        assert "derived_objects" not in bundle.stores_touched
        assert _derived_items(bundle) == []

    def test_raw_records_stay_in_the_raw_lane(self, node) -> None:
        """`semantic_hits` promises dated source rows a consumer can follow back
        to a connector. A derived summary is not one of those."""
        _seed_close_circle(node)
        index_derived_objects(node)
        bundle = _retrieve(node)
        previews = [
            str(hit.get("text_preview") or "")
            for hit in (bundle.context_packet.get("semantic_hits") or [])
        ]
        assert not any("personal circle" in p.lower() for p in previews), previews

    def test_the_kill_switch_stops_the_reader(self, node, monkeypatch) -> None:
        _seed_close_circle(node)
        index_derived_objects(node)
        monkeypatch.setenv("TOPOS_DERIVED_OBJECT_INDEX", "off")
        bundle = _retrieve(node)
        assert "derived_objects" not in bundle.stores_touched
        assert _derived_items(bundle) == []


class TestTheLaneNamesPeopleOnlyToTheOwner:
    def test_a_grantee_gets_no_derived_items(self, node) -> None:
        _seed_close_circle(node)
        index_derived_objects(node)
        bundle = _retrieve(node, disclosure_tier="default_disclosure")
        assert _derived_items(bundle) == [], (
            "the owner's personal circle reached a grantee by name. The scope "
            "declares `relationship_edges`, so the per-object grant check ALLOWS "
            "this — the tier gate is the only thing standing here"
        )
        assert "derived_objects" not in bundle.stores_touched

    def test_no_grantee_summary_carries_the_name(self, node) -> None:
        """Stronger than the lane check: the name must not arrive through ANY
        lane the derived rows feed."""
        _seed_close_circle(node)
        index_derived_objects(node)
        bundle = _retrieve(node, disclosure_tier="default_disclosure")
        blob = json.dumps(bundle.context_packet).lower()
        assert "grandma" not in blob, "the derived rendering leaked into a grantee packet"

    def test_the_owner_still_gets_it(self, node) -> None:
        """The control for both tests above — a gate that withheld from everyone
        would pass them and destroy the feature."""
        _seed_close_circle(node)
        index_derived_objects(node)
        assert _derived_items(_retrieve(node, disclosure_tier="owner_raw"))
