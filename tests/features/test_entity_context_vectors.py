"""M1 §3.1 — person-only mention-context centroids.

The trap these tests exist to hold shut: ``entities.embedding_blob`` is a
canonical-*name* embedding and is NOT what a context centroid is built from.
These centroids come from the embeddings of the records an entity is mentioned
in, they are person-only, and they never leave the engine.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from topos.features.entities.context_vectors import (
    MIN_CONTEXT_MENTIONS,
    load_context_centroid,
    rebuild_entity_context_vectors,
)
from topos.features.signal.vector_codec import encode_f32
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

_DIMS = 8


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "context_vectors.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _add_entity(conn, entity_id: str, entity_type: str, name: str) -> None:
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)
        VALUES (?, ?, ?, ?)
        """,
        (entity_id, entity_type, name, name.lower()),
    )


def _add_mention_with_embedding(conn, entity_id: str, record_id: str, vector) -> None:
    conn.execute(
        """
        INSERT INTO entity_mentions (mention_id, entity_id, record_id, surface_text)
        VALUES (?, ?, ?, ?)
        """,
        (f"m-{entity_id}-{record_id}", entity_id, record_id, entity_id),
    )
    conn.execute(
        """
        INSERT INTO signal_embeddings
            (embedding_id, record_id, vector_blob, vector_format, dims, model)
        VALUES (?, ?, ?, 'f32', ?, ?)
        """,
        (
            f"e-{record_id}",
            record_id,
            encode_f32(vector),
            len(vector),
            "sentence-transformers/all-MiniLM-L6-v2",
        ),
    )


def _basis(index: int, scale: float = 1.0):
    vector = [0.0] * _DIMS
    vector[index % _DIMS] = scale
    return vector


def _seed_mentions(conn, entity_id: str, count: int, *, offset: int = 0) -> None:
    for i in range(count):
        _add_mention_with_embedding(
            conn, entity_id, f"rec-{entity_id}-{i}", _basis(offset + i)
        )


class TestPersonOnlyScope:
    def test_non_person_entities_get_no_row(self, conn) -> None:
        """D3 enforced at build time, not filtered downstream."""
        _add_entity(conn, "p1", "person", "Maya Chen")
        _add_entity(conn, "o1", "org", "Mudlark Studio")
        _add_entity(conn, "pl1", "place", "Lisbon")
        _add_entity(conn, "pr1", "project", "Topos")
        for entity_id in ("p1", "o1", "pl1", "pr1"):
            _seed_mentions(conn, entity_id, MIN_CONTEXT_MENTIONS + 2)
        conn.commit()

        rebuild_entity_context_vectors(conn)

        stored = {
            row[0]
            for row in conn.execute("SELECT entity_id FROM entity_context_vectors").fetchall()
        }
        assert stored == {"p1"}
        non_person = conn.execute(
            """
            SELECT COUNT(*) FROM entity_context_vectors v
            JOIN entities e ON e.entity_id = v.entity_id
            WHERE e.entity_type != 'person'
            """
        ).fetchone()[0]
        assert non_person == 0


class TestMentionFloor:
    def test_below_floor_gets_no_centroid(self, conn) -> None:
        _add_entity(conn, "sparse", "person", "Sparse Person")
        _seed_mentions(conn, "sparse", MIN_CONTEXT_MENTIONS - 1)
        conn.commit()

        result = rebuild_entity_context_vectors(conn)

        assert result["centroids_written"] == 0
        assert result["skipped_below_floor"] == 1
        assert load_context_centroid(conn, "sparse") is None

    def test_at_floor_gets_a_centroid(self, conn) -> None:
        _add_entity(conn, "dense", "person", "Dense Person")
        _seed_mentions(conn, "dense", MIN_CONTEXT_MENTIONS)
        conn.commit()

        result = rebuild_entity_context_vectors(conn)

        assert result["centroids_written"] == 1
        row = conn.execute(
            "SELECT mention_sample, model_name FROM entity_context_vectors WHERE entity_id='dense'"
        ).fetchone()
        assert row[0] == MIN_CONTEXT_MENTIONS
        assert row[1] == "sentence-transformers/all-MiniLM-L6-v2"

    def test_repeated_mentions_of_one_record_count_once(self, conn) -> None:
        """mention_sample counts distinct contexts, not mention rows."""
        _add_entity(conn, "echo", "person", "Echo Person")
        _seed_mentions(conn, "echo", MIN_CONTEXT_MENTIONS - 1)
        # A second mention of an already-counted record: same context, so the
        # floor must not be cleared by it.
        conn.execute(
            """
            INSERT INTO entity_mentions (mention_id, entity_id, record_id, surface_text)
            VALUES ('m-echo-dup', 'echo', 'rec-echo-0', 'Echo')
            """
        )
        conn.commit()

        result = rebuild_entity_context_vectors(conn)

        assert result["centroids_written"] == 0


class TestCentroidShape:
    def test_centroid_is_l2_normalised(self, conn) -> None:
        _add_entity(conn, "p1", "person", "Maya Chen")
        _seed_mentions(conn, "p1", MIN_CONTEXT_MENTIONS + 3)
        conn.commit()

        rebuild_entity_context_vectors(conn)

        centroid = load_context_centroid(conn, "p1")
        assert centroid is not None
        assert len(centroid) == _DIMS
        assert math.isclose(math.sqrt(sum(x * x for x in centroid)), 1.0, rel_tol=1e-5)

    def test_centroid_is_the_mean_direction_of_its_contexts(self, conn) -> None:
        _add_entity(conn, "p1", "person", "Maya Chen")
        for i in range(MIN_CONTEXT_MENTIONS):
            # Two axes only, unequal counts, so a wrong reduction (sum, max,
            # last-wins) would not land on the same direction as the mean.
            _add_mention_with_embedding(
                conn, "p1", f"rec-p1-{i}", _basis(0 if i < 4 else 1)
            )
        conn.commit()

        rebuild_entity_context_vectors(conn)

        centroid = load_context_centroid(conn, "p1")
        expected = [4.0 / 5.0, 1.0 / 5.0] + [0.0] * (_DIMS - 2)
        norm = math.sqrt(sum(x * x for x in expected))
        for got, want in zip(centroid, expected):
            assert math.isclose(got, want / norm, abs_tol=1e-5)

    def test_centroid_is_not_the_canonical_name_embedding(self, conn) -> None:
        """§2.2: name embeddings are a dedup signal; a centroid must ignore them."""
        _add_entity(conn, "p1", "person", "Maya Chen")
        conn.execute(
            "UPDATE entities SET embedding_blob = ? WHERE entity_id = 'p1'",
            (encode_f32(_basis(7)),),
        )
        for i in range(MIN_CONTEXT_MENTIONS):
            _add_mention_with_embedding(conn, "p1", f"rec-p1-{i}", _basis(0))
        conn.commit()

        rebuild_entity_context_vectors(conn)

        centroid = load_context_centroid(conn, "p1")
        assert math.isclose(centroid[0], 1.0, abs_tol=1e-5)
        assert math.isclose(centroid[7], 0.0, abs_tol=1e-5)


class TestRebuildSemantics:
    def test_rebuild_replaces_rather_than_accumulates(self, conn) -> None:
        _add_entity(conn, "p1", "person", "Maya Chen")
        _seed_mentions(conn, "p1", MIN_CONTEXT_MENTIONS)
        conn.commit()

        first = rebuild_entity_context_vectors(conn)
        second = rebuild_entity_context_vectors(conn)

        assert first["centroids_written"] == second["centroids_written"] == 1
        assert conn.execute("SELECT COUNT(*) FROM entity_context_vectors").fetchone()[0] == 1

    def test_entity_falling_below_floor_loses_its_row(self, conn) -> None:
        _add_entity(conn, "p1", "person", "Maya Chen")
        _seed_mentions(conn, "p1", MIN_CONTEXT_MENTIONS)
        conn.commit()
        rebuild_entity_context_vectors(conn)
        assert load_context_centroid(conn, "p1") is not None

        conn.execute("DELETE FROM entity_mentions WHERE record_id = 'rec-p1-0'")
        conn.commit()
        rebuild_entity_context_vectors(conn)

        assert load_context_centroid(conn, "p1") is None

    def test_entity_retyped_away_from_person_loses_its_row(self, conn) -> None:
        _add_entity(conn, "p1", "person", "Mudlark")
        _seed_mentions(conn, "p1", MIN_CONTEXT_MENTIONS)
        conn.commit()
        rebuild_entity_context_vectors(conn)
        assert load_context_centroid(conn, "p1") is not None

        conn.execute("UPDATE entities SET entity_type = 'org' WHERE entity_id = 'p1'")
        conn.commit()
        rebuild_entity_context_vectors(conn)

        assert load_context_centroid(conn, "p1") is None


class TestNightlyCadence:
    def test_cluster_rebuild_carries_the_centroid_build(self, conn) -> None:
        """D2: nightly, alongside the cluster rebuild — not per enrichment pass."""
        from topos.features.signal import topic_clustering

        _add_entity(conn, "p1", "person", "Maya Chen")
        _seed_mentions(conn, "p1", MIN_CONTEXT_MENTIONS)
        conn.commit()

        result = topic_clustering.recompute_topic_clusters(conn, min_records=1)

        assert result["context_centroids"]["centroids_written"] == 1
        assert load_context_centroid(conn, "p1") is not None

    def test_centroid_failure_does_not_fail_the_cluster_rebuild(
        self, conn, monkeypatch
    ) -> None:
        from topos.features.entities import context_vectors
        from topos.features.signal import topic_clustering

        _add_entity(conn, "p1", "person", "Maya Chen")
        _seed_mentions(conn, "p1", MIN_CONTEXT_MENTIONS)
        conn.commit()

        def boom(*_args, **_kwargs):
            raise RuntimeError("synthetic centroid failure")

        monkeypatch.setattr(context_vectors, "rebuild_entity_context_vectors", boom)

        result = topic_clustering.recompute_topic_clusters(conn, min_records=1)

        assert result["context_centroids"]["status"] == "error"
        assert load_context_centroid(conn, "p1") is None


class TestSchema:
    def test_table_is_separate_from_entities(self, conn) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(entity_context_vectors)")}
        assert cols == {
            "entity_id",
            "centroid_blob",
            "mention_sample",
            "model_name",
            "computed_at",
        }
        entity_cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)")}
        assert "centroid_blob" not in entity_cols
