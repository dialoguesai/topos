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
    MIN_CONTEXT_SOURCES,
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


def _add_mention_with_embedding(
    conn, entity_id: str, record_id: str, vector, *, content_hash: str | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO entity_mentions (mention_id, entity_id, record_id, surface_text)
        VALUES (?, ?, ?, ?)
        """,
        (f"m-{entity_id}-{record_id}", entity_id, record_id, entity_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO signal_embeddings
            (embedding_id, record_id, vector_blob, vector_format, dims, model,
             content_hash)
        VALUES (?, ?, ?, 'f32', ?, ?, ?)
        """,
        (
            f"e-{record_id}",
            record_id,
            encode_f32(vector),
            len(vector),
            "sentence-transformers/all-MiniLM-L6-v2",
            content_hash,
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


class TestSourceDiversityFloor:
    """§3.1a defect A: the floor counts source documents, not mentions.

    The live shape it was found in: one page of epigrams at
    ``thehypertexts.com`` visited three times, leaving three distinct
    ``record_id``s that carry one document.
    """

    def test_three_mentions_of_one_document_do_not_clear_the_floor(self, conn) -> None:
        _add_entity(conn, "woolf", "person", "Virginia Woolf")
        for i in range(MIN_CONTEXT_SOURCES):
            _add_mention_with_embedding(
                conn,
                "woolf",
                f"browser:http://example.test/epigrams.htm_2026-07-2{i}T00:00:00.000Z",
                _basis(0),
                content_hash="one-page",
            )
        conn.commit()

        result = rebuild_entity_context_vectors(
            conn, min_sources=MIN_CONTEXT_SOURCES, min_mentions=1
        )

        assert result["centroids_written"] == 0
        assert result["skipped_below_source_floor"] == 1
        assert load_context_centroid(conn, "woolf") is None

    def test_three_mentions_across_three_documents_do_clear_the_floor(self, conn) -> None:
        _add_entity(conn, "woolf", "person", "Virginia Woolf")
        for i in range(MIN_CONTEXT_SOURCES):
            _add_mention_with_embedding(
                conn, "woolf", f"rec-woolf-{i}", _basis(i), content_hash=f"page-{i}"
            )
        conn.commit()

        result = rebuild_entity_context_vectors(
            conn, min_sources=MIN_CONTEXT_SOURCES, min_mentions=1
        )

        assert result["centroids_written"] == 1
        assert load_context_centroid(conn, "woolf") is not None

    def test_source_count_is_recorded_separately_from_the_mention_count(
        self, conn
    ) -> None:
        """Their ratio is the re-read factor that hid the defect."""
        _add_entity(conn, "woolf", "person", "Virginia Woolf")
        # Six records, three documents: each page read twice.
        for i in range(6):
            _add_mention_with_embedding(
                conn, "woolf", f"rec-woolf-{i}", _basis(i % 3), content_hash=f"page-{i % 3}"
            )
        conn.commit()

        rebuild_entity_context_vectors(conn, min_sources=MIN_CONTEXT_SOURCES, min_mentions=1)

        row = conn.execute(
            "SELECT mention_sample, source_sample FROM entity_context_vectors "
            "WHERE entity_id='woolf'"
        ).fetchone()
        assert row == (6, 3)

    def test_a_re_read_document_does_not_outvote_a_single_read_one(self, conn) -> None:
        """The centroid averages sources, so reading a page twice cannot tilt it."""
        _add_entity(conn, "p1", "person", "Maya Chen")
        for i in range(4):
            # Four records of one document on axis 0 ...
            _add_mention_with_embedding(
                conn, "p1", f"rec-p1-a{i}", _basis(0), content_hash="page-a"
            )
        for i in range(2):
            _add_mention_with_embedding(
                conn, "p1", f"rec-p1-{i}", _basis(i + 1), content_hash=f"page-{i}"
            )
        conn.commit()

        rebuild_entity_context_vectors(conn, min_sources=3, min_mentions=1)

        centroid = load_context_centroid(conn, "p1")
        # ... which counts once, so all three axes come out equal. Averaging
        # records instead would put axis 0 at four times the others.
        assert math.isclose(centroid[0], centroid[1], abs_tol=1e-5)
        assert math.isclose(centroid[0], centroid[2], abs_tol=1e-5)

    def test_browser_revisits_collapse_by_url_when_no_content_hash(self, conn) -> None:
        """Fallback path for rows predating ``content_hash``."""
        _add_entity(conn, "woolf", "person", "Virginia Woolf")
        base = "browser:http://www.thehypertexts.com/Epigrams.htm"
        for stamp in ("2026-07-22T00:46:42.328Z", "2026-07-22T04:14:39.394Z", "2026-07-22T04:14:39.632Z"):
            _add_mention_with_embedding(conn, "woolf", f"{base}_{stamp}", _basis(0))
        conn.commit()

        result = rebuild_entity_context_vectors(conn, min_sources=3, min_mentions=1)

        assert result["centroids_written"] == 0
        assert result["skipped_below_source_floor"] == 1

    def test_mention_floor_still_applies_as_the_secondary_gate(self, conn) -> None:
        _add_entity(conn, "p1", "person", "Maya Chen")
        for i in range(MIN_CONTEXT_SOURCES):
            _add_mention_with_embedding(
                conn, "p1", f"rec-p1-{i}", _basis(i), content_hash=f"page-{i}"
            )
        conn.commit()

        result = rebuild_entity_context_vectors(conn)

        assert MIN_CONTEXT_SOURCES < MIN_CONTEXT_MENTIONS
        assert result["centroids_written"] == 0
        assert result["skipped_below_mention_floor"] == 1


class TestDegeneracyRejection:
    def test_five_entities_sharing_one_record_set_get_no_centroids(self, conn) -> None:
        """The live §3.1a shape, end to end.

        Woolf, Shakespeare, Aristotle, Voltaire and Hafiz were all quoted on
        one page. Their centroids came out byte-identical and every pairwise
        cosine was exactly 1.0000 — 26 maximally-confident "latent affinities"
        out of a list of quoted authors. Nothing survives here: the shared
        records are one document, and even if they were not, five coincident
        centroids describe a page rather than five people.
        """
        names = ["Virginia Woolf", "Shakespeare", "Aristotle", "Voltaire", "Hafiz"]
        shared = [f"browser:http://example.test/epigrams.htm_2026-07-2{i}T00:00:00.000Z" for i in range(6)]
        for index, name in enumerate(names):
            entity_id = f"p{index}"
            _add_entity(conn, entity_id, "person", name)
            for offset, record_id in enumerate(shared):
                _add_mention_with_embedding(
                    conn, entity_id, record_id, _basis(offset), content_hash="one-page"
                )
        conn.commit()

        result = rebuild_entity_context_vectors(conn, min_sources=1, min_mentions=1)

        assert result["dropped_degenerate"] == len(names)
        assert result["centroids_written"] == 0
        assert conn.execute("SELECT COUNT(*) FROM entity_context_vectors").fetchone()[0] == 0

    def test_coincident_centroids_are_dropped_and_counted(self, conn, caplog) -> None:
        # Two entities on identical contexts, one on its own.
        for entity_id, name in (("clone-a", "Ana"), ("clone-b", "Bo"), ("solo", "Cy")):
            _add_entity(conn, entity_id, "person", name)
        for entity_id in ("clone-a", "clone-b"):
            for i in range(MIN_CONTEXT_MENTIONS):
                _add_mention_with_embedding(
                    conn, entity_id, f"shared-{i}", _basis(i), content_hash=f"shared-{i}"
                )
        for i in range(MIN_CONTEXT_MENTIONS):
            _add_mention_with_embedding(
                conn, "solo", f"solo-{i}", _basis(7), content_hash=f"solo-{i}"
            )
        conn.commit()

        with caplog.at_level("WARNING"):
            result = rebuild_entity_context_vectors(conn)

        assert result["dropped_degenerate"] == 2
        assert result["centroids_written"] == 1
        assert load_context_centroid(conn, "clone-a") is None
        assert load_context_centroid(conn, "clone-b") is None
        assert load_context_centroid(conn, "solo") is not None
        assert "degenerate context centroids" in caplog.text

    def test_distinct_centroids_survive(self, conn) -> None:
        for index, entity_id in enumerate(("p1", "p2")):
            _add_entity(conn, entity_id, "person", f"Person {index}")
            for i in range(MIN_CONTEXT_MENTIONS):
                _add_mention_with_embedding(
                    conn,
                    entity_id,
                    f"rec-{entity_id}-{i}",
                    _basis(index),
                    content_hash=f"{entity_id}-{i}",
                )
        conn.commit()

        result = rebuild_entity_context_vectors(conn)

        assert result["dropped_degenerate"] == 0
        assert result["centroids_written"] == 2


class TestSelfExclusion:
    def test_is_self_entity_gets_no_row(self, conn) -> None:
        """§3.1a defect B, at the same gate as D3's person-only filter."""
        _add_entity(conn, "owner", "person", "Owner")
        _add_entity(conn, "other", "person", "Devi Raman")
        conn.execute("UPDATE entities SET is_self = 1 WHERE entity_id = 'owner'")
        for entity_id in ("owner", "other"):
            for i in range(MIN_CONTEXT_MENTIONS):
                _add_mention_with_embedding(
                    conn,
                    entity_id,
                    f"rec-{entity_id}-{i}",
                    _basis(i if entity_id == "owner" else i + 1),
                    content_hash=f"{entity_id}-{i}",
                )
        conn.commit()

        result = rebuild_entity_context_vectors(conn)

        assert load_context_centroid(conn, "owner") is None
        assert load_context_centroid(conn, "other") is not None
        # Excluded at build time, so the owner is never even a candidate.
        assert result["entities_considered"] == 1


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
            "source_sample",
            "model_name",
            "computed_at",
        }
        entity_cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)")}
        assert "centroid_blob" not in entity_cols
