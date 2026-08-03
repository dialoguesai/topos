"""B8: inference attribution metadata + list_metadata null provenance resilience."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import (
    _INFERENCE_CANONICAL_EXCLUDED_KEYS,
    _load_canonical_summary_items,
)
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.adapters.sqlite.stores import SQLiteVectorIndex

pytestmark = [pytest.mark.check("C-quality-imb-phrasing")]

_ENGINE_QQ = Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"
sys.path.insert(0, str(_ENGINE_QQ))


def test_list_metadata_tolerates_null_provenance_json(tmp_path: Path) -> None:
    """IMB/PRV search_text-only rows leave provenance_json NULL — inference must not crash."""
    db = tmp_path / "v.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            signal_dimension TEXT,
            model TEXT,
            text_preview TEXT,
            search_text TEXT,
            provenance_json TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension, "
        "search_text, provenance_json) VALUES ('e1', 'r1', 's', 'memory', 'hello', NULL)"
    )
    conn.commit()
    store = SQLiteVectorIndex(conn)
    page = store.list_metadata(limit=10, offset=0)
    assert page.total == 1
    assert page.items == [{}]


def test_inference_strip_keeps_speaker_label_and_owner_authored(tmp_path_factory) -> None:
    """speaker_label + owner_authored survive topic/summary_text inference strip."""
    from imbalance_seed_corpus import build_imbalance_corpus

    db = build_imbalance_corpus(tmp_path_factory.mktemp("b8-meta") / "imb.db")
    adapters = AdapterFactory.create("local_database", db_path=db)
    conn = adapters.signal._conn  # noqa: SLF001
    manifest = resolve_scope_manifest("messages:read")
    plan = SimpleNamespace(first_person_intent=True, first_person_belief=False)
    items = _load_canonical_summary_items(
        manifest=manifest,
        adapters=adapters,
        query_text="messages",
        source_ids=[],
        disclosure_tier="owner_raw",
        browse_fallback=True,
        plan=plan,
        conn=conn,
    )
    assert items
    assert any(i.get("owner_authored") is True for i in items)
    non_owner = [i for i in items if i.get("owner_authored") is False]
    assert non_owner, "expected ambient rows under first_person re-rank (not belief filter)"
    assert any(i.get("speaker_label") for i in non_owner)

    stripped = [
        {k: v for k, v in item.items() if k not in _INFERENCE_CANONICAL_EXCLUDED_KEYS}
        for item in items
    ]
    for score in stripped:
        assert "summary_text" not in score
        assert "topic" not in score
    assert any(s.get("owner_authored") is True for s in stripped)
    assert any(s.get("speaker_label") for s in stripped)


def test_inference_retrieve_threads_plan_for_owner_authored(tmp_path_factory) -> None:
    """Regression: inference retrieve must pass plan (B8) so belief asks stamp owner_authored."""
    from imbalance_seed_corpus import build_imbalance_corpus
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest

    db = build_imbalance_corpus(tmp_path_factory.mktemp("b8-plan") / "imb.db")
    adapters = AdapterFactory.create("local_database", db_path=db)
    result = DefaultSignalRetrievalAdapter(adapters).retrieve(
        RetrievalRequest(
            manifest=resolve_scope_manifest("messages:read"),
            access_mode="inference",
            query_text="What do I actually think about cryptocurrency?",
        )
    )
    scores = result.context_packet.get("scores") or []
    msg_scores = [
        s for s in scores
        if isinstance(s, dict) and str(s.get("retrieval_source") or "").startswith("canonical:")
    ]
    assert msg_scores, "expected canonical message scores"
    assert all(s.get("owner_authored") is True for s in msg_scores), msg_scores[:3]
