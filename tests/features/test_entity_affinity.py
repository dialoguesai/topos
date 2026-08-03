"""M1 §3.2-§3.4a — latent semantic-affinity edges.

Five things these tests exist to hold shut, in the order the plan raises them:

1. ``semantic_affinity`` is symmetric, so it must canonicalise src/dst — or a
   rebuild writes A->B where the last wrote B->A and duplicates accumulate.
2. The caps are the design. Mutual-top-N, co-occurrence suppression and the
   hard ceiling each drop pairs that would otherwise drown the precise ones.
3. The floor comes from the node's OWN distribution, with an absolute backstop
   that makes "no affinity edges" reachable.
4. The rebuild is a snapshot, not a fold: twice over unchanged inputs is the
   same active set plus one superseded revision per edge.
5. §2.2's trap: affinity must NOT rediscover aliases. A pair the consolidation
   sweep flags as a merge candidate gets no affinity edge.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from topos.features.entities.affinity import (
    AFFINITY_FLOOR_ABS,
    AFFINITY_SPEC_VERSION,
    ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE,
    rebuild_affinity_edges,
)
from topos.features.entities.edges import EDGE_SEMANTIC_AFFINITY
from topos.features.signal.vector_codec import encode_f32
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

_DIMS = 8


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "affinity.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _unit(components: dict) -> list:
    """Unit vector with the given ``axis: value`` components."""
    vector = [0.0] * _DIMS
    for axis, value in components.items():
        vector[axis] = value
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def _tilted(axis: int, along: float) -> list:
    """Unit vector at cosine ``along`` from e0, tilted into its own axis.

    Two such vectors on distinct axes have cosine ``along_a * along_b`` with
    each other, which is what lets these tests state an exact cosine matrix
    instead of hoping an angle lands where they want it.
    """
    return _unit({0: along, axis: math.sqrt(max(0.0, 1.0 - along * along))})


def _add_person(conn, entity_id: str, name: str, *, mentions: int = 6) -> None:
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,
                              mention_count, is_self)
        VALUES (?, 'person', ?, ?, ?, 0)
        """,
        (entity_id, name, name.lower(), mentions),
    )


def _add_centroid(
    conn,
    entity_id: str,
    vector,
    *,
    mention_sample: int = 10,
    source_sample: int | None = None,
) -> None:
    """A stored centroid. ``source_sample`` defaults to the mention count.

    The default is deliberate: these tests are about edge construction, not
    about the §3.1a floors, so the uninteresting case is an entity whose
    records each came from their own document.
    """
    conn.execute(
        """
        INSERT INTO entity_context_vectors
            (entity_id, centroid_blob, mention_sample, source_sample,
             model_name, computed_at)
        VALUES (?, ?, ?, ?, 'test-model', '2026-07-31T00:00:00Z')
        """,
        (
            entity_id,
            encode_f32(vector),
            mention_sample,
            mention_sample if source_sample is None else source_sample,
        ),
    )


def _add_edge(conn, src: str, dst: str, edge_type: str) -> None:
    a, b = (src, dst) if src < dst else (dst, src)
    conn.execute(
        """
        INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,
                                  weight, evidence_count, valid_from)
        VALUES (?, ?, ?, ?, 1.0, 1, '2026-07-01T00:00:00Z')
        """,
        (f"edg-{a}-{b}-{edge_type}", a, b, edge_type),
    )


def _seed_headroom(conn, count: int) -> None:
    """Non-affinity active edges, purely to raise the §3.4 ceiling.

    The ceiling is a ratio against other active edges, so a graph with no other
    edges admits no affinity edges at all. Tests that are not about the ceiling
    have to buy headroom first.
    """
    for i in range(count):
        conn.execute(
            """
            INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,
                                      weight, evidence_count, valid_from)
            VALUES (?, ?, ?, 'discusses', 1.0, 1, '2026-07-01T00:00:00Z')
            """,
            (f"edg-filler-{i}", f"filler-a-{i}", f"filler-b-{i}"),
        )


def _active_pairs(conn) -> set:
    return {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM entity_edges "
            "WHERE edge_type = ? AND valid_to IS NULL",
            (EDGE_SEMANTIC_AFFINITY,),
        )
    }


class TestSymmetry:
    def test_src_dst_are_canonically_ordered(self, conn) -> None:
        """§3.2: without this a rebuild writes B->A over yesterday's A->B."""
        _add_person(conn, "zeta", "Zeta Person")
        _add_person(conn, "alpha", "Alpha Person")
        _add_centroid(conn, "zeta", _unit({0: 1.0}))
        _add_centroid(conn, "alpha", _unit({0: 0.99, 1: 0.1}))
        _seed_headroom(conn, 10)
        conn.commit()

        rebuild_affinity_edges(conn, percentile=0.0)

        assert _active_pairs(conn) == {("alpha", "zeta")}

    def test_rebuild_never_accumulates_a_mirrored_duplicate(self, conn) -> None:
        _add_person(conn, "zeta", "Zeta Person")
        _add_person(conn, "alpha", "Alpha Person")
        _add_centroid(conn, "zeta", _unit({0: 1.0}))
        _add_centroid(conn, "alpha", _unit({0: 0.99, 1: 0.1}))
        _seed_headroom(conn, 10)
        conn.commit()

        for _ in range(3):
            rebuild_affinity_edges(conn, percentile=0.0)

        assert len(_active_pairs(conn)) == 1


class TestMutualTopN:
    """§3.4: BOTH endpoints must rank the other; one-directional is not enough."""

    def _seed_asymmetric(self, conn) -> None:
        # cos(A,B)=0.9, cos(A,C)=0.8, cos(B,C)=0.72 — so A ranks B first, B
        # ranks A first (mutual), but C ranks A first while A ranks C second.
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_person(conn, "c", "Cy")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _add_centroid(conn, "c", _tilted(2, 0.8))
        _seed_headroom(conn, 20)
        conn.commit()

    def test_one_directional_top_n_match_produces_no_edge(self, conn) -> None:
        self._seed_asymmetric(conn)

        result = rebuild_affinity_edges(conn, topn=1, percentile=0.0)

        # C ranks A in its top-1 but A does not reciprocate, so no A-C edge.
        assert _active_pairs(conn) == {("a", "b")}
        assert result["edges_written"] == 1

    def test_widening_top_n_admits_the_reciprocated_pair(self, conn) -> None:
        """The same graph at topn=2 — proves the exclusion above was mutuality."""
        self._seed_asymmetric(conn)

        rebuild_affinity_edges(conn, topn=2, percentile=0.0)

        assert _active_pairs(conn) == {("a", "b"), ("a", "c"), ("b", "c")}


class TestCoOccurrenceSuppression:
    def test_pair_with_an_active_co_occurrence_edge_is_skipped(self, conn) -> None:
        """§3.4: co-mention already found them; affinity would double-count."""
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_person(conn, "c", "Cy")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _add_centroid(conn, "c", _tilted(2, 0.8))
        _add_edge(conn, "a", "b", "co_occurrence")
        _seed_headroom(conn, 20)
        conn.commit()

        result = rebuild_affinity_edges(conn, topn=8, percentile=0.0)

        assert ("a", "b") not in _active_pairs(conn)
        assert _active_pairs(conn) == {("a", "c"), ("b", "c")}
        assert result["suppressed_co_occurring"] == 1

    def test_a_closed_co_occurrence_edge_does_not_suppress(self, conn) -> None:
        """Only ACTIVE co-mention is redundant; an ended one is history."""
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _add_edge(conn, "a", "b", "co_occurrence")
        conn.execute(
            "UPDATE entity_edges SET valid_to='2026-01-01T00:00:00Z' "
            "WHERE edge_type='co_occurrence'"
        )
        _seed_headroom(conn, 20)
        conn.commit()

        rebuild_affinity_edges(conn, percentile=0.0)

        assert _active_pairs(conn) == {("a", "b")}


class TestCeiling:
    #: Unrelated names on purpose. "Person 0"/"Person 1" share a token and score
    #: 0.86 on token-set similarity, so the §3.1a merge-candidate suppression
    #: would eat the whole fixture before the ceiling ever got a say.
    _NAMES = [
        "Devi Raman",
        "Kwame Osei",
        "Lucia Ferrari",
        "Hiroshi Tanaka",
        "Marguerite Yourcenar",
        "Bogdan Petrescu",
        "Ngozi Adeyemi",
        "Soren Lindqvist",
    ]

    def _seed_dense(self, conn, *, people: int, headroom: int) -> None:
        """Every pair mutually ranked and comfortably above the floor."""
        for i in range(people):
            entity_id = f"p{i:02d}"
            _add_person(conn, entity_id, self._NAMES[i])
            # Small distinct tilts: all pair cosines >= 0.9 * 0.9 = 0.81.
            _add_centroid(conn, entity_id, _tilted(1 + (i % (_DIMS - 1)), 0.9 + i * 0.001))
        _seed_headroom(conn, headroom)
        conn.commit()

    def test_ceiling_truncates_and_logs_rather_than_writing_past_it(self, conn) -> None:
        self._seed_dense(conn, people=6, headroom=4)

        result = rebuild_affinity_edges(conn, percentile=0.0)

        # 6 people, mutual-top-8 over 5 candidates each => all 15 pairs qualify.
        # 4 other active edges * 0.5 => ceiling of 2.
        assert result["pairs_considered"] == 15
        assert result["ceiling"] == 2
        assert result["ceiling_hit"] is True
        assert result["edges_written"] == 2
        assert len(_active_pairs(conn)) == 2

        log = conn.execute(
            "SELECT pairs_considered, edges_written, ceiling, ceiling_hit "
            "FROM affinity_recompute_log ORDER BY recompute_id DESC LIMIT 1"
        ).fetchone()
        assert log == (15, 2, 2, 1)

    def test_truncation_keeps_the_strongest_pairs(self, conn) -> None:
        self._seed_dense(conn, people=6, headroom=4)

        rebuild_affinity_edges(conn, percentile=0.0)

        kept = [
            row[0]
            for row in conn.execute(
                "SELECT weight FROM entity_edges WHERE edge_type=? AND valid_to IS NULL",
                (EDGE_SEMANTIC_AFFINITY,),
            )
        ]
        all_scores = sorted(
            (
                row[0]
                for row in conn.execute(
                    "SELECT weight FROM entity_edges WHERE edge_type=?",
                    (EDGE_SEMANTIC_AFFINITY,),
                )
            ),
            reverse=True,
        )
        assert sorted(kept, reverse=True) == all_scores[: len(kept)]

    def test_a_graph_with_no_other_edges_admits_no_affinity_edges(self, conn) -> None:
        """The ratio is against other edges, so affinity cannot bootstrap itself."""
        self._seed_dense(conn, people=4, headroom=0)

        result = rebuild_affinity_edges(conn, percentile=0.0)

        assert result["ceiling"] == 0
        assert result["ceiling_hit"] is True
        assert _active_pairs(conn) == set()

    def test_below_the_ceiling_nothing_is_truncated(self, conn) -> None:
        self._seed_dense(conn, people=4, headroom=40)

        result = rebuild_affinity_edges(conn, percentile=0.0)

        assert result["ceiling_hit"] is False
        assert result["edges_written"] == 6
        log_hit = conn.execute(
            "SELECT ceiling_hit FROM affinity_recompute_log ORDER BY recompute_id DESC LIMIT 1"
        ).fetchone()[0]
        assert log_hit == 0


class TestFloor:
    def test_backstop_makes_zero_edges_expressible(self, conn) -> None:
        """§3.4a: a node whose whole distribution sits below 0.35 gets nothing."""
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_person(conn, "c", "Cy")
        _add_centroid(conn, "a", _tilted(1, 0.3))
        _add_centroid(conn, "b", _tilted(2, 0.3))
        _add_centroid(conn, "c", _tilted(3, 0.3))
        _seed_headroom(conn, 40)
        conn.commit()

        result = rebuild_affinity_edges(conn)

        # The percentile would have admitted the top of this distribution; the
        # backstop is the only thing standing between it and a full edge set.
        assert result["resolved_cosine"] < AFFINITY_FLOOR_ABS
        assert result["floor_cosine"] == AFFINITY_FLOOR_ABS
        assert result["edges_written"] == 0
        assert _active_pairs(conn) == set()

    def test_floor_is_the_percentile_when_it_exceeds_the_backstop(self, conn) -> None:
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_person(conn, "c", "Cy")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _add_centroid(conn, "c", _tilted(2, 0.8))
        _seed_headroom(conn, 40)
        conn.commit()

        result = rebuild_affinity_edges(conn, percentile=100.0)

        # P=100 resolves to the distribution's maximum (0.9), which is well
        # above the backstop, so only the single tightest pair survives.
        assert result["floor_cosine"] == pytest.approx(0.9, abs=1e-6)
        assert _active_pairs(conn) == {("a", "b")}

    def test_percentile_comes_from_engine_config_so_it_is_per_node(self, conn) -> None:
        from topos.core.state import set_engine_config_value

        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _seed_headroom(conn, 40)
        conn.commit()
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_AFFINITY_PERCENTILE, "42.5")

        result = rebuild_affinity_edges(conn)

        assert result["percentile"] == pytest.approx(42.5)

    def test_log_records_the_resolved_cosine_as_the_drift_signal(self, conn) -> None:
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _seed_headroom(conn, 40)
        conn.commit()

        rebuild_affinity_edges(conn, percentile=99.5)

        row = conn.execute(
            "SELECT percentile, resolved_cosine, floor_cosine "
            "FROM affinity_recompute_log ORDER BY recompute_id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == pytest.approx(99.5)
        assert row[1] == pytest.approx(0.9, abs=1e-6)
        assert row[2] == pytest.approx(0.9, abs=1e-6)

    def test_every_rebuild_appends_exactly_one_log_row(self, conn) -> None:
        _seed_headroom(conn, 4)
        conn.commit()

        for _ in range(3):
            rebuild_affinity_edges(conn)

        assert conn.execute("SELECT COUNT(*) FROM affinity_recompute_log").fetchone()[0] == 3


class TestWeightSemantics:
    def test_weight_is_the_bounded_cosine_and_evidence_is_the_lesser_sample(
        self, conn
    ) -> None:
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_centroid(conn, "a", _unit({0: 1.0}), mention_sample=31)
        _add_centroid(conn, "b", _tilted(1, 0.9), mention_sample=7)
        _seed_headroom(conn, 40)
        conn.commit()

        rebuild_affinity_edges(conn, percentile=0.0)

        weight, evidence, metadata = conn.execute(
            "SELECT weight, evidence_count, metadata_json FROM entity_edges "
            "WHERE edge_type=? AND valid_to IS NULL",
            (EDGE_SEMANTIC_AFFINITY,),
        ).fetchone()
        assert 0.0 <= weight <= 1.0
        assert weight == pytest.approx(0.9, abs=1e-6)
        assert evidence == 7

        import json

        payload = json.loads(metadata)
        assert payload["kind"] == "affinity"
        assert payload["cosine"] == pytest.approx(0.9, abs=1e-5)
        assert payload["spec_version"] == AFFINITY_SPEC_VERSION

    def test_writer_does_not_fold_evidence_across_rebuilds(self, conn) -> None:
        """A snapshot, not an accumulator: update_edge's semantics must not leak."""
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_centroid(conn, "a", _unit({0: 1.0}), mention_sample=9)
        _add_centroid(conn, "b", _tilted(1, 0.9), mention_sample=9)
        _seed_headroom(conn, 40)
        conn.commit()

        for _ in range(4):
            rebuild_affinity_edges(conn, percentile=0.0)

        weight, evidence = conn.execute(
            "SELECT weight, evidence_count FROM entity_edges "
            "WHERE edge_type=? AND valid_to IS NULL",
            (EDGE_SEMANTIC_AFFINITY,),
        ).fetchone()
        assert weight == pytest.approx(0.9, abs=1e-6)
        assert evidence == 9


class TestRebuildIdempotency:
    def test_twice_yields_the_same_active_set_and_one_superseded_revision(
        self, conn
    ) -> None:
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_person(conn, "c", "Cy")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _add_centroid(conn, "c", _tilted(2, 0.8))
        _seed_headroom(conn, 40)
        conn.commit()

        first = rebuild_affinity_edges(conn, percentile=0.0)
        before = _active_pairs(conn)
        second = rebuild_affinity_edges(conn, percentile=0.0)

        assert before == _active_pairs(conn)
        assert first["edges_written"] == second["edges_written"] == len(before)
        assert second["edges_superseded"] == len(before)

        closed = conn.execute(
            "SELECT src_entity_id, dst_entity_id, COUNT(*) FROM entity_edges "
            "WHERE edge_type=? AND valid_to IS NOT NULL "
            "GROUP BY src_entity_id, dst_entity_id",
            (EDGE_SEMANTIC_AFFINITY,),
        ).fetchall()
        assert {(row[0], row[1]) for row in closed} == before
        assert all(row[2] == 1 for row in closed)

    def test_history_is_kept_not_deleted(self, conn) -> None:
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_centroid(conn, "a", _unit({0: 1.0}))
        _add_centroid(conn, "b", _tilted(1, 0.9))
        _seed_headroom(conn, 40)
        conn.commit()
        rebuild_affinity_edges(conn, percentile=0.0)

        # The pair falls apart: b's centroid swings away.
        conn.execute(
            "UPDATE entity_context_vectors SET centroid_blob=? WHERE entity_id='b'",
            (encode_f32(_tilted(1, 0.1)),),
        )
        conn.commit()
        rebuild_affinity_edges(conn, percentile=0.0)

        assert _active_pairs(conn) == set()
        closed = conn.execute(
            "SELECT COUNT(*) FROM entity_edges WHERE edge_type=? AND valid_to IS NOT NULL",
            (EDGE_SEMANTIC_AFFINITY,),
        ).fetchone()[0]
        assert closed == 1


class TestAntiAliasRegression:
    def test_no_affinity_edge_between_a_flagged_merge_candidate_pair(self, conn) -> None:
        """§2.2: identical NAME embeddings, divergent CONTEXT centroids.

        This is the exact shape of the trap. "Sara" and "Sarah Chen" are a
        short-form alias pair — the consolidation sweep flags them — and they
        carry an IDENTICAL ``entities.embedding_blob``, which is what a
        name-embedding cosine would see. Their mention CONTEXTS are orthogonal.
        An affinity implementation that reached for ``embedding_blob`` instead
        of ``entity_context_vectors`` would emit a 1.0 edge here, and that edge
        would be an alias suggestion wearing an affinity label. The third
        person proves the graph is otherwise productive: real affinity edges do
        get written in this same run.
        """
        from topos.features.entities.consolidation import propose_merges

        _add_person(conn, "sarah", "Sarah Chen")
        _add_person(conn, "sara", "Sara")
        _add_person(conn, "other", "Devi Raman")
        name_vector = encode_f32(_unit({5: 1.0}))
        for entity_id in ("sarah", "sara", "other"):
            conn.execute(
                "UPDATE entities SET embedding_blob=? WHERE entity_id=?",
                (name_vector, entity_id),
            )
        # Contexts: the two "Chen"s live in unrelated parts of the corpus, and
        # each is close to a third person who is not a merge candidate.
        _add_centroid(conn, "sarah", _unit({0: 1.0}))
        _add_centroid(conn, "sara", _unit({1: 1.0}))
        _add_centroid(conn, "other", _tilted(1, 0.7))
        _seed_headroom(conn, 40)
        conn.commit()

        # No model is loaded here: every entity already has an embedding_blob,
        # so ensure_name_embeddings finds nothing to embed.
        proposed = propose_merges(conn, use_embeddings=True)
        flagged = {
            tuple(sorted(row))
            for row in conn.execute(
                "SELECT subject_entity_id, candidate_entity_id FROM entity_review "
                "WHERE kind='merge'"
            )
        }
        assert proposed["total"] >= 1
        assert ("sara", "sarah") in flagged

        rebuild_affinity_edges(conn, percentile=0.0)

        active = _active_pairs(conn)
        assert flagged & active == set()
        assert ("sara", "sarah") not in active
        assert active == {("other", "sarah"), ("other", "sara")}


    def test_alias_pair_with_near_identical_contexts_still_gets_no_edge(
        self, conn
    ) -> None:
        """§3.1a defect B, in the live shape that exposed it.

        At the shipped floor BOTH surviving pairs on the node were this:
        ``Jonny ↔ Jonny Johnson`` at 0.682 and ``Jonny Johnson ↔ draftin1`` at
        0.759 — the owner and their own unconsolidated aliases. Two names for
        one person are mentioned in one person's contexts, so their CENTROIDS
        agree, which is exactly why an orthogonal-context fixture cannot test
        this: the previous test passes on cosine alone. Here the alias pair is
        the single strongest pair in the graph and must still write nothing.
        """
        _add_person(conn, "jonny", "Jonny")
        _add_person(conn, "jonny_johnson", "Jonny Johnson")
        _add_person(conn, "other", "Devi Raman")
        _add_centroid(conn, "jonny", _unit({0: 1.0}))
        _add_centroid(conn, "jonny_johnson", _tilted(1, 0.99))
        _add_centroid(conn, "other", _tilted(2, 0.6))
        _seed_headroom(conn, 40)
        conn.commit()

        result = rebuild_affinity_edges(conn, percentile=0.0)

        active = _active_pairs(conn)
        assert ("jonny", "jonny_johnson") not in active
        assert result["suppressed_merge_candidates"] == 1
        # The graph is otherwise productive: suppression is targeted, not a
        # blanket refusal to write edges.
        assert active

    def test_a_dismissed_merge_review_stops_suppressing(self, conn) -> None:
        """The owner saying "not the same person" makes the pair legitimate."""
        _add_person(conn, "ana", "Ana Silva")
        _add_person(conn, "anahi", "Anahi Silva")
        _add_centroid(conn, "ana", _unit({0: 1.0}))
        _add_centroid(conn, "anahi", _tilted(1, 0.95))
        _seed_headroom(conn, 40)
        conn.commit()

        suppressed = rebuild_affinity_edges(conn, percentile=0.0)
        assert suppressed["suppressed_merge_candidates"] == 1
        assert _active_pairs(conn) == set()

        # Renaming removes the name-similarity finding; the dismissed review
        # row is what must not resurrect the suppression.
        conn.execute(
            "UPDATE entities SET canonical_name='Marguerite Yourcenar', "
            "normalized_name='marguerite yourcenar' WHERE entity_id='anahi'"
        )
        conn.execute(
            """
            INSERT INTO entity_review
                (review_id, surface_text, candidate_entity_id, score, kind,
                 subject_entity_id, reason, status)
            VALUES ('rev-dismissed', 'Anahi Silva', 'ana', 0.85, 'merge',
                    'anahi', 'fuzzy:token_set_0.85', 'dismissed')
            """
        )
        conn.commit()

        allowed = rebuild_affinity_edges(conn, percentile=0.0)

        assert allowed["suppressed_merge_candidates"] == 0
        assert ("ana", "anahi") in _active_pairs(conn)


class TestEvidenceCount:
    def test_evidence_counts_source_documents_not_mention_records(self, conn) -> None:
        """§3.1a: a mention count is not evidence.

        ``draftin1`` carried 38 mention records drawn from ONE document. An
        edge reporting 38 there would be reporting a re-read count as
        corroboration.
        """
        _add_person(conn, "a", "Ana")
        _add_person(conn, "b", "Bo")
        _add_centroid(conn, "a", _unit({0: 1.0}), mention_sample=38, source_sample=4)
        _add_centroid(conn, "b", _tilted(1, 0.9), mention_sample=31, source_sample=9)
        _seed_headroom(conn, 40)
        conn.commit()

        rebuild_affinity_edges(conn, percentile=0.0)

        evidence = conn.execute(
            "SELECT evidence_count FROM entity_edges WHERE edge_type=? AND valid_to IS NULL",
            (EDGE_SEMANTIC_AFFINITY,),
        ).fetchone()[0]
        assert evidence == 4


class TestComplexityIsolation:
    def test_semantic_affinity_is_not_in_the_complexity_allow_list(self) -> None:
        """D1: affinity edges stay out of the influence graph in the first pass."""
        from topos.features.complexity.projection import ALLOWED_EDGE_TYPES

        assert EDGE_SEMANTIC_AFFINITY not in ALLOWED_EDGE_TYPES


class TestEmptyNode:
    def test_no_centroids_still_logs_and_writes_nothing(self, conn) -> None:
        _seed_headroom(conn, 10)
        conn.commit()

        result = rebuild_affinity_edges(conn)

        assert result["entities_considered"] == 0
        assert result["pairs_considered"] == 0
        assert result["edges_written"] == 0
        assert conn.execute("SELECT COUNT(*) FROM affinity_recompute_log").fetchone()[0] == 1
