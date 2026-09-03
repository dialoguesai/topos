"""S8 — goals enter the derived index.

protects: a stated goal is findable by paraphrase, not only by naming it.

Measured on the owner store 2026-09-03 (the gap this closes): 1,548
``user_goals`` + 68 ``Goal`` objects held real prose and ZERO of them were
embedded, so "what am I trying to achieve commercially?" reached a
connector dossier and a stray fact instead. Dossiers/facts were already at
100% coverage; goals were the whole remaining hole.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from topos.features.signal.derived_index import (
    DERIVED_RECORD_TYPES,
    _NameResolver,
    load_indexable_objects,
    render_object,
)
from topos.storage.db.migrations import apply_all_migrations


def _seed_goal(conn: sqlite3.Connection, object_id: str, object_type: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,"
        " payload_json, confidence, valid_from, created_by, created_at, updated_at)"
        " VALUES (?, 'goals', ?, ?, ?, 0.8, '2026-08-01T00:00:00', 'test',"
        " '2026-08-01T00:00:00', '2026-08-01T00:00:00')",
        (object_id, object_type, f"{object_type}:{object_id}", json.dumps(payload)),
    )


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(tmp_path / "goals.db"))
    apply_all_migrations(c)
    _seed_goal(c, "g-1", "user_goals", {
        "goal_text": "Aim the pitch at the segments that actually convert",
        "source_id": "grow_data_file",
    })
    _seed_goal(c, "g-2", "Goal", {
        "goal_text": "Make sure the simulations service reads from a local and a hosted instance",
        "horizon": "near_term",
        "status": "in_progress",
    })
    # Identity-shaped: no prose to embed. Must be skipped, not rendered as its key.
    _seed_goal(c, "g-3", "user_goals", {"goal_text": "", "source_id": "x"})
    c.commit()
    return c


def test_goal_types_are_registered_for_indexing(conn):
    assert "user_goals" in DERIVED_RECORD_TYPES
    assert "Goal" in DERIVED_RECORD_TYPES
    loaded = {o["object_id"] for o in load_indexable_objects(conn)}
    assert {"g-1", "g-2"} <= loaded


def test_goal_renders_as_a_sentence_a_person_could_have_written(conn):
    resolver = _NameResolver(conn)
    objs = {o["object_id"]: o for o in load_indexable_objects(conn)}

    r1 = render_object(objs["g-1"], resolver)
    assert r1 is not None
    assert "segments that actually convert" in r1.text
    assert r1.record_type == DERIVED_RECORD_TYPES["user_goals"]
    # The kind hint is encoder scaffolding, never shown to a reader.
    assert "goal" in r1.header.lower()
    assert "|" not in r1.text

    r2 = render_object(objs["g-2"], resolver)
    assert r2 is not None
    assert "simulations service" in r2.text
    # A near-term in-progress goal says so — status/horizon are retrieval
    # handles ("what am I working on right now").
    assert "progress" in r2.text.lower() or "near" in r2.text.lower()


def test_textless_goal_is_skipped_not_rendered_as_its_key(conn):
    resolver = _NameResolver(conn)
    objs = {o["object_id"]: o for o in load_indexable_objects(conn)}
    assert render_object(objs.get("g-3") or {"object_type": "user_goals"}, resolver) is None


def test_goal_rendering_carries_owner_only_disclosure(conn):
    """A goal is the owner's own statement of intent — the index must not
    widen it. Every derived row inherits owner_only unless its payload says
    otherwise, the same rule facts carry."""
    resolver = _NameResolver(conn)
    objs = {o["object_id"]: o for o in load_indexable_objects(conn)}
    r = render_object(objs["g-1"], resolver)
    assert r.disclosure == "owner_only"


def test_indexing_writes_goal_embeddings(conn, monkeypatch):
    """End-to-end through the real indexer with a stub encoder."""
    from topos.engine.backends import huggingface as hf
    from topos.features.signal import derived_index as di

    class _StubAdapter:
        """Deterministic encoder — the indexer's contract is vectors-per-text."""

        def run_inference(self, payload, options):  # noqa: ANN001
            texts = payload.get("texts") or []
            return {"vectors": [[0.1] * 384 for _ in texts]}

    # The adapter is imported INSIDE index_derived_objects, so the stub has to
    # land on its source module, not on derived_index's namespace.
    monkeypatch.setattr(hf, "HuggingFaceAdapter", lambda *a, **k: _StubAdapter())
    counts = di.index_derived_objects(conn, model="stub-model")
    assert counts.get("disabled") != 1, "derived index disabled in this env"
    assert counts["written"] >= 2, counts

    rows = conn.execute(
        "SELECT record_type, text_preview FROM signal_embeddings"
        " WHERE record_type IN (?, ?)",
        (DERIVED_RECORD_TYPES["user_goals"], DERIVED_RECORD_TYPES["Goal"]),
    ).fetchall()
    assert len(rows) >= 2
    assert any("segments that actually convert" in str(r[1]) for r in rows)


def test_full_rendering_reaches_answers_not_the_200_char_preview():
    """The vector-hit rebuild is an explicit allow-list; omitting search_text
    capped every derived summary at the preview length (measured 2026-09-03:
    45 of 462 derived rows carried up to 850 chars no answer could show)."""
    import inspect

    from topos.query import retrieval

    src = inspect.getsource(retrieval._semantic_hits)
    assert '"search_text": item.get("search_text")' in src, (
        "the hit rebuild dropped search_text again — derived summaries are "
        "silently truncated to text_preview"
    )
