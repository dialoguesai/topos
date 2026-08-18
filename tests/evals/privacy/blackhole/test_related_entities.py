"""`related_entities` as a black-hole leak surface.

Found on a live node on 2026-08-17 by the install QA's b19 probe: one cluster
served a protected entity's name in `metadata.related_entities` while its label
— the surface everyone had thought about — was clean.

Two things make this surface different from the label, and both argue for
refusing at the producer rather than redacting on the way out:

  * it is not only displayed. `fact_materializer` resolves each name into an
    entity node and a `discusses` edge, so a protected name here becomes a
    SECOND surface in the graph that no read-side filter on the cluster API
    would ever reach;
  * it is a list, so dropping an element leaves a coherent list — there is no
    "the artifact is the name" problem that forces the label's hand.

So: the producer does not mint it, and the graph lane refuses it again on the
way in, because one forgetful producer should not be able to put a protected
entity into the graph.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle.blackhole import blackholed_name_terms
from topos.features.signal.topic_clustering import (
    _load_related_entities,
    _names_protected_entity,
)
from tests.evals.privacy.blackhole.corpus import (
    BH_ALIAS_A,
    BH_ID,
    BH_CANONICAL,
    OK_CANONICAL,
    build_blackhole_corpus,
)

pytestmark = [pytest.mark.bhlr, pytest.mark.private]


@pytest.fixture()
def corpus_conn(tmp_path):
    db_path = str(tmp_path / "bh.db")
    build_blackhole_corpus(db_path)
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _seed_mentions(conn: sqlite3.Connection, record_id: str, names: list[str]) -> None:
    """One record whose extracted entities include a protected name."""
    for i, name in enumerate(names):
        conn.execute(
            """
            INSERT INTO message_entities
                (entity_id, record_id, source_id, entity_text, payload_json)
            VALUES (?,?,?,?,'{}')
            """,
            (f"me-{record_id}-{i}", record_id, "src-1", name),
        )
    conn.commit()


class TestProducerRefusal:
    def test_a_protected_name_is_never_minted_into_related_entities(self, corpus_conn):
        _seed_mentions(corpus_conn, "rec-1", [BH_CANONICAL, OK_CANONICAL, OK_CANONICAL])

        related = _load_related_entities(corpus_conn, [{"record_id": "rec-1", "source_id": "src-1"}])

        assert OK_CANONICAL in related, "the unprotected entity must still be there"
        assert BH_CANONICAL not in related
        assert not any(_names_protected_entity(n, blackholed_name_terms(corpus_conn)) for n in related)

    def test_an_alias_is_refused_too(self, corpus_conn):
        """Protection is by name, not by row: an alias names the same person."""
        _seed_mentions(corpus_conn, "rec-2", [BH_ALIAS_A, OK_CANONICAL])

        related = _load_related_entities(corpus_conn, [{"record_id": "rec-2", "source_id": "src-1"}])

        assert BH_ALIAS_A not in related
        assert OK_CANONICAL in related

    def test_the_list_still_fills_to_ten_when_it_can(self, corpus_conn):
        """Filtering must not cost a slot: the cap applies AFTER the refusal,
        or protecting one entity silently shortens every list that mentions it."""
        names = [f"Neighbour {i} Zz{i}qq" for i in range(12)]
        _seed_mentions(corpus_conn, "rec-3", [BH_CANONICAL, *names])

        related = _load_related_entities(corpus_conn, [{"record_id": "rec-3", "source_id": "src-1"}])

        assert len(related) == 10
        assert BH_CANONICAL not in related

    def test_no_protection_configured_is_not_an_error(self, tmp_path):
        """A node with the feature absent must still build its lists."""
        db_path = str(tmp_path / "plain.db")
        from topos.storage.db.migrations import apply_all_migrations

        conn = sqlite3.connect(db_path)
        apply_all_migrations(conn)
        _seed_mentions(conn, "rec-4", ["Ada Lovelace"])
        try:
            assert _load_related_entities(conn, [{"record_id": "rec-4", "source_id": "src-1"}]) == [
                "Ada Lovelace"
            ]
        finally:
            conn.close()


class TestTheChainDownstream:
    """Why the producer, and not a filter on the way out.

    ``entity_edges`` is already a guarded surface: ``BlackholeGuard`` filters
    both ends by ``entity_id`` on read. That guard cannot see a bare NAME
    sitting in a cluster payload — nothing ties the string back to the
    protected entity at read time — and `fact_materializer` turns those names
    into `discusses` edges. Refusing at the producer is what keeps the name out
    of the payload, so the edge is never minted in the first place.
    """

    def test_a_refused_name_never_reaches_the_graph(self, corpus_conn):
        from topos.features.entities.fact_materializer import (
            materialize_signal_objects_to_graph,
        )

        _seed_mentions(corpus_conn, "rec-5", [BH_CANONICAL, OK_CANONICAL])
        related = _load_related_entities(
            corpus_conn, [{"record_id": "rec-5", "source_id": "src-1"}]
        )
        # The payload the cluster pass would store, built by the real producer.
        corpus_conn.execute(
            """
            INSERT INTO signal_objects
                (object_id, signal_dimension, object_type, object_key, payload_json,
                 valid_from, created_at, updated_at)
            VALUES ('so-rel-1', 'interests', 'top_topics', 'clu-rel-1', ?,
                    '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')
            """,
            (json.dumps({"tag": "neighbourhood", "related_entities": related}),),
        )
        corpus_conn.commit()

        materialize_signal_objects_to_graph(corpus_conn)

        discusses = [
            r[0]
            for r in corpus_conn.execute(
                """
                SELECT e.canonical_name
                FROM entity_edges AS ed
                JOIN entities AS e ON e.entity_id = ed.dst_entity_id
                WHERE ed.edge_type = 'discusses'
                  AND json_extract(ed.metadata_json, '$.source_object_id') = 'so-rel-1'
                """
            )
        ]
        assert OK_CANONICAL in discusses, "the topic must still reach its real subjects"
        assert BH_CANONICAL not in discusses


class TestRebuildWithdrawal:
    """The other direction of time.

    The producer keeps the name out of clusters minted from here on. Clusters
    minted BEFORE the entity was protected still carry it, and blackholing is
    supposed to act on what already exists — so the rebuild withdraws it, the
    same shape the label already had.
    """

    def _cluster_with(self, conn, cluster_id, metadata):
        conn.execute(
            """
            INSERT INTO topic_clusters
                (cluster_id, dimension, label, centroid_preview, metadata_json,
                 created_at, updated_at)
            VALUES (?, 'interests', 'neighbourhood', '', ?, datetime('now'), datetime('now'))
            """,
            (cluster_id, json.dumps(metadata)),
        )
        conn.commit()

    def _metadata(self, conn, cluster_id):
        row = conn.execute(
            "SELECT metadata_json FROM topic_clusters WHERE cluster_id=?", (cluster_id,)
        ).fetchone()
        return json.loads(row[0] or "{}")

    def test_rebuild_takes_the_name_out_of_an_existing_cluster(self, corpus_conn):
        from topos.features.lifecycle.blackhole_rebuild import rebuild_for_blackhole

        self._cluster_with(
            corpus_conn,
            "clu-old-1",
            {
                "related_entities": [BH_CANONICAL, OK_CANONICAL],
                "query_aliases": [BH_CANONICAL.lower(), "neighbourhood"],
            },
        )

        rebuild_for_blackhole(corpus_conn, BH_ID)

        meta = self._metadata(corpus_conn, "clu-old-1")
        assert BH_CANONICAL not in meta["related_entities"]
        assert OK_CANONICAL in meta["related_entities"], "only the protected name goes"
        assert not any(BH_CANONICAL.lower() in a for a in meta["query_aliases"]), (
            "query_aliases is derived from related_entities and leaks the same name"
        )
        assert "neighbourhood" in meta["query_aliases"]

    def test_a_clean_cluster_is_left_alone(self, corpus_conn):
        from topos.features.lifecycle.blackhole_rebuild import rebuild_for_blackhole

        self._cluster_with(
            corpus_conn, "clu-old-2", {"related_entities": [OK_CANONICAL], "query_aliases": ["x"]}
        )
        before = self._metadata(corpus_conn, "clu-old-2")

        rebuild_for_blackhole(corpus_conn, BH_ID)

        assert self._metadata(corpus_conn, "clu-old-2") == before
