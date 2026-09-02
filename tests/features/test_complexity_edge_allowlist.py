"""Edge-type allow-list guard for the Complexity influence graph (plan §3.5).

`projection.EvidenceCache` used to read every active row in `entity_edges` with
no `edge_type` filter, and `influence._add_entity_edges` feeds every row it
returns into PageRank. Any writer minting a new edge type therefore moved live
Complexity readings with no change under `features/complexity/`.

These tests hold both halves of the fix:

* `ALLOWED_EDGE_TYPES` really gates the read — an unrecognised type cannot
  reach the projection.
* A golden characterisation of the influence graph, so opting a new type IN
  shows up as a diff on this file instead of a silent shift in production
  readings.
"""

from __future__ import annotations

import sqlite3
import struct
from datetime import datetime, timedelta, timezone

import pytest

from topos.features.complexity import projection as projection_mod
from topos.features.complexity.canonical import build_canonical_index
from topos.features.complexity.influence import _InfluenceGraph
from topos.features.complexity.projection import (
    ALLOWED_EDGE_TYPES,
    AnalyticalProjection,
    ProjectionConfig,
)
from topos.features.complexity.topic_merge import build_supertopics
from topos.features.entities.edges import (
    EDGE_CO_OCCURRENCE,
    EDGE_COMMUNICATES,
    EDGE_DISCUSSES,
    EDGE_LOCATED_AT,
    EDGE_PART_OF,
)

# The five typed-edge constants from features/entities/edges.py:27. Named here
# so a rename over there fails this test rather than quietly shrinking the
# influence graph.
EDGES_PY_TYPES = frozenset(
    {
        EDGE_CO_OCCURRENCE,
        EDGE_COMMUNICATES,
        EDGE_DISCUSSES,
        EDGE_LOCATED_AT,
        EDGE_PART_OF,
    }
)

# The type M1 will mint. It must stay OUT until someone opts it in (plan D1).
FUTURE_AFFINITY_TYPE = "semantic_affinity"

# Fixed clock: the projection decays against `end`, so a golden file needs one.
FIXED_END = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _days_ago(n: float) -> str:
    return _iso(FIXED_END - timedelta(days=n))


def _f32(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            is_self INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            mention_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE entity_edges (
            edge_id TEXT PRIMARY KEY,
            src_entity_id TEXT NOT NULL,
            dst_entity_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_event_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            created_at TEXT
        );
        CREATE TABLE entity_mentions (
            mention_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT,
            canonical_table TEXT,
            surface_text TEXT,
            confidence REAL,
            event_at TEXT
        );
        CREATE TABLE topic_clusters (
            cluster_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            dimension TEXT NOT NULL DEFAULT 'memory',
            member_count INTEGER NOT NULL DEFAULT 0,
            label_terms_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE topic_cluster_members (
            member_id TEXT PRIMARY KEY,
            cluster_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            text_preview TEXT
        );
        CREATE TABLE timeline (
            event_at TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT,
            canonical_table TEXT,
            cluster_id TEXT,
            entity_ids_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (event_at, record_id)
        );
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            vector_blob BLOB,
            vector_format TEXT NOT NULL DEFAULT 'f32',
            chunk_index INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE conversation_messages (
            message_id TEXT PRIMARY KEY,
            is_from_self INTEGER DEFAULT 0
        );
        """
    )
    return conn


def _add_entity(conn: sqlite3.Connection, entity_id: str, name: str, entity_type: str) -> None:
    conn.execute(
        """
        INSERT INTO entities
        (entity_id, entity_type, canonical_name, normalized_name, mention_count)
        VALUES (?, ?, ?, ?, 1)
        """,
        (entity_id, entity_type, name, name.lower()),
    )


def _add_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    src: str,
    dst: str,
    edge_type: str,
    *,
    days_ago: float = 3.0,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_edges
        (edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
         last_event_at, valid_from, valid_to, created_at)
        VALUES (?, ?, ?, ?, 4.0, 4, ?, ?, NULL, ?)
        """,
        (edge_id, src, dst, edge_type, _days_ago(days_ago), _days_ago(30), _days_ago(30)),
    )


def _projection(conn: sqlite3.Connection) -> AnalyticalProjection:
    return AnalyticalProjection(
        conn,
        ProjectionConfig(window_days=90, end=FIXED_END),
        canonical=build_canonical_index(conn),
        supertopics=build_supertopics(conn),
    )


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------


def test_allow_list_contains_every_edges_py_type():
    """The five typed constants must never fall out of the allow-list."""
    assert EDGES_PY_TYPES <= ALLOWED_EDGE_TYPES


def test_unknown_edge_types_never_reach_the_projection():
    """Every allowed type is read; anything else is dropped at the SQL door."""
    conn = _conn()
    allowed = sorted(ALLOWED_EDGE_TYPES)
    # One distinct entity pair per edge type, so nothing merges or self-loops.
    for i, edge_type in enumerate(allowed):
        _add_entity(conn, f"a{i}", f"Alpha {i}", "person")
        _add_entity(conn, f"b{i}", f"Beta {i}", "org")
        _add_edge(conn, f"e{i}", f"a{i}", f"b{i}", edge_type)

    unknown_types = [
        FUTURE_AFFINITY_TYPE,
        "co_participation",  # analytics/messenger_graph vocabulary
        "totally_made_up_edge",
    ]
    for j, edge_type in enumerate(unknown_types):
        _add_entity(conn, f"ua{j}", f"UnknownAlpha {j}", "person")
        _add_entity(conn, f"ub{j}", f"UnknownBeta {j}", "org")
        _add_edge(conn, f"ue{j}", f"ua{j}", f"ub{j}", edge_type)
    conn.commit()

    seen = {edge.edge_type for edge in _projection(conn).edges()}

    # all five known types (and the rest of the allow-list) survive
    assert EDGES_PY_TYPES <= seen
    assert seen == set(ALLOWED_EDGE_TYPES)
    # and nothing outside it does
    for edge_type in unknown_types:
        assert edge_type not in seen


def test_semantic_affinity_stays_out_until_opted_in(monkeypatch):
    """Plan D1: M1's edge type is excluded until it is added to the list."""
    conn = _conn()
    _add_entity(conn, "p1", "Alice", "person")
    _add_entity(conn, "p2", "Bob", "person")
    _add_edge(conn, "e_aff", "p1", "p2", FUTURE_AFFINITY_TYPE)
    conn.commit()

    assert _projection(conn).edges() == []

    monkeypatch.setattr(
        projection_mod, "ALLOWED_EDGE_TYPES", ALLOWED_EDGE_TYPES | {FUTURE_AFFINITY_TYPE}
    )
    opted_in = _projection(conn).edges()
    assert [edge.edge_type for edge in opted_in] == [FUTURE_AFFINITY_TYPE]


# ---------------------------------------------------------------------------
# golden characterisation
# ---------------------------------------------------------------------------


def _seed_golden(conn: sqlite3.Connection) -> None:
    """Small deterministic graph: fixed clock, fixed weights, no randomness."""
    for entity_id, name, entity_type in [
        ("self1", "Owner", "person"),
        ("p1", "Alice", "person"),
        ("p2", "Bob", "person"),
        ("p3", "Carol", "person"),
        ("c1", "Aurora", "org"),
        ("c2", "Helio Labs", "org"),
        ("pl1", "Austin", "place"),
    ]:
        _add_entity(conn, entity_id, name, entity_type)
    conn.execute("UPDATE entities SET is_self = 1 WHERE entity_id = 'self1'")

    # One edge per typed-core relation plus the enricher/declared/fact groups,
    # each on its own pair so the golden reads unambiguously.
    for edge_id, src, dst, edge_type, days in [
        ("g1", "p1", "p2", EDGE_CO_OCCURRENCE, 2.0),
        ("g2", "p2", "p3", EDGE_COMMUNICATES, 4.0),
        ("g3", "p3", "c1", EDGE_DISCUSSES, 6.0),
        ("g4", "p1", "pl1", EDGE_LOCATED_AT, 8.0),
        ("g5", "c1", "c2", EDGE_PART_OF, 10.0),
        ("g6", "p1", "c1", "worked_on", 12.0),
        ("g7", "p2", "c2", "relates_to", 14.0),
        ("g8", "p3", "pl1", "mentions", 16.0),
        ("g9", "p2", "c1", "works_on", 18.0),
    ]:
        _add_edge(conn, edge_id, src, dst, edge_type, days_ago=days)

    # Two themes so supertopics (and their similarity edges) exist.
    v_work = [1.0, 0.0, 0.1, 0.0]
    v_life = [0.0, 1.0, 0.0, 0.1]
    # Every endpoint above needs mention evidence: the influence graph only
    # keeps edges whose BOTH nodes cleared the evidence gate.
    plan = [
        ("work", v_work, "grow_journal", "journal_entries", ["kw1", "kw2"], ["p1", "c1", "c2"]),
        ("life", v_life, "imessage", "conversation_messages", ["kl1"], ["p2", "p3", "pl1"]),
    ]
    for theme, vector, source_id, table, cluster_ids, entity_ids in plan:
        for i in range(4):
            record_id = f"r_{theme}_{i}"
            when = _days_ago(2 + 3 * i)
            conn.execute(
                "INSERT INTO timeline (event_at, record_id, source_id, canonical_table)"
                " VALUES (?, ?, ?, ?)",
                (when, record_id, source_id, table),
            )
            conn.execute(
                "INSERT INTO signal_embeddings (embedding_id, record_id, vector_blob)"
                " VALUES (?, ?, ?)",
                (f"emb_{record_id}", record_id, _f32([v + 0.01 * i for v in vector])),
            )
            for cluster_id in cluster_ids:
                conn.execute(
                    "INSERT INTO topic_cluster_members"
                    " (member_id, cluster_id, record_id, source_id) VALUES (?, ?, ?, ?)",
                    (f"m_{cluster_id}_{record_id}", cluster_id, record_id, source_id),
                )
            for entity_id in entity_ids:
                conn.execute(
                    """
                    INSERT INTO entity_mentions
                    (mention_id, entity_id, record_id, source_id, canonical_table,
                     confidence, event_at)
                    VALUES (?, ?, ?, ?, ?, 0.9, ?)
                    """,
                    (f"men_{entity_id}_{record_id}", entity_id, record_id, source_id, table, when),
                )
    conn.executemany(
        "INSERT INTO topic_clusters (cluster_id, label, dimension, member_count,"
        " label_terms_json) VALUES (?, ?, ?, ?, ?)",
        [
            ("kw1", "Aurora Work", "memory", 4, '["aurora","ship"]'),
            ("kw2", "Aurora Work", "relationships", 4, '["aurora","team"]'),
            ("kl1", "Home Life", "wellbeing", 4, '["family","rest"]'),
        ],
    )
    conn.commit()


def _influence_edges(conn: sqlite3.Connection) -> list[tuple[str, str, str, float]]:
    """Label-keyed, order-stable view of the influence graph's edge set."""
    canonical = build_canonical_index(conn)
    supertopics = build_supertopics(conn)
    projection = AnalyticalProjection(
        conn,
        ProjectionConfig(window_days=90, end=FIXED_END),
        canonical=canonical,
        supertopics=supertopics,
    )
    graph = _InfluenceGraph(conn, canonical, supertopics, projection)
    return sorted(
        (
            *sorted((graph.label_of(a), graph.label_of(b))),
            str(data["relation"]),
            round(float(data["weight"]), 6),
        )
        for a, b, data in graph.graph.edges(data=True)
    )


# Pinned 2026-07-31 with the allow-list in place. A change here means the
# influence graph moved: either a deliberate opt-in (say so in the commit and
# re-pin) or a regression.
GOLDEN_INFLUENCE_EDGES: list[tuple[str, str, str, float]] = [
    ("Alice", "Aurora", "worked_on", 2.06061),
    ("Alice", "Aurora Work", "co_occurs_in_records", 1.080914),
    ("Alice", "Austin", "located_at", 1.384146),
    ("Alice", "Bob", "co_occurrence", 0.759083),
    ("Aurora", "Aurora Work", "co_occurs_in_records", 1.080914),
    ("Aurora", "Bob", "works_on", 1.878705),
    ("Aurora", "Carol", "discusses", 1.665358),
    ("Aurora", "Helio Labs", "part_of", 0.671077),
    ("Aurora Work", "Helio Labs", "co_occurs_in_records", 1.080914),
    ("Austin", "Carol", "mentions", 0.713809),
    ("Austin", "Home Life", "co_occurs_in_records", 0.540457),
    ("Bob", "Carol", "communicates_with", 2.085488),
    ("Bob", "Helio Labs", "relates_to", 0.315489),
    ("Bob", "Home Life", "co_occurs_in_records", 0.540457),
    ("Carol", "Home Life", "co_occurs_in_records", 0.540457),
]


def test_influence_graph_matches_golden():
    conn = _conn()
    _seed_golden(conn)
    assert _influence_edges(conn) == GOLDEN_INFLUENCE_EDGES


def test_unlisted_edge_type_leaves_the_golden_untouched():
    """The guard's payoff: writing a new type cannot move readings by itself."""
    conn = _conn()
    _seed_golden(conn)
    _add_edge(conn, "g_aff", "p1", "p3", FUTURE_AFFINITY_TYPE, days_ago=1.0)
    conn.commit()
    assert _influence_edges(conn) == GOLDEN_INFLUENCE_EDGES


def test_opting_a_type_in_produces_a_visible_diff(monkeypatch):
    """And when it IS opted in, the change is a diff on this file, not silence."""
    conn = _conn()
    _seed_golden(conn)
    _add_edge(conn, "g_aff", "p1", "p3", FUTURE_AFFINITY_TYPE, days_ago=1.0)
    conn.commit()

    monkeypatch.setattr(
        projection_mod, "ALLOWED_EDGE_TYPES", ALLOWED_EDGE_TYPES | {FUTURE_AFFINITY_TYPE}
    )
    opted_in = _influence_edges(conn)
    assert opted_in != GOLDEN_INFLUENCE_EDGES
    new_edges = set(opted_in) - set(GOLDEN_INFLUENCE_EDGES)
    assert {(a, b, relation) for a, b, relation, _ in new_edges} == {
        ("Alice", "Carol", FUTURE_AFFINITY_TYPE)
    }


def test_golden_fixture_is_deterministic():
    """A golden that drifts run to run is worse than no golden at all."""
    first = _conn()
    _seed_golden(first)
    second = _conn()
    _seed_golden(second)
    assert _influence_edges(first) == _influence_edges(second)


@pytest.mark.parametrize("edge_type", sorted(EDGES_PY_TYPES))
def test_each_known_type_is_individually_readable(edge_type):
    conn = _conn()
    _add_entity(conn, "p1", "Alice", "person")
    _add_entity(conn, "c1", "Aurora", "org")
    _add_edge(conn, "e1", "p1", "c1", edge_type)
    conn.commit()
    assert [edge.edge_type for edge in _projection(conn).edges()] == [edge_type]



def test_an_undated_edge_contributes_weight_but_no_date(tmp_path):
    """part_of edges are written with no event -- last_event_at NULL, valid_from
    the recompute's clock. The projection read COALESCE(last_event_at,
    valid_from, created_at), so a rebuild tonight gave every such edge tonight
    as its activity date. Weight is real; the date was not."""
    import sqlite3
    from topos.storage.db.migrations import apply_all_migrations
    from topos.features.entities.resolver import EntityResolver

    conn = sqlite3.connect(str(tmp_path / "c.db"))
    apply_all_migrations(conn)
    r = EntityResolver(conn)
    a = r._create_entity("Org A", "organization")
    b = r._create_entity("Unit B", "organization")
    conn.commit()
    conn.execute(
        """
        INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,
                                  weight, evidence_count, valid_from, metadata_json)
        VALUES ('e1', ?, ?, 'part_of', 1.0, 1, '2026-08-30T00:00:00Z', '{}')
        """,
        (b, a),
    )
    conn.commit()
    rows = list(conn.execute(
        "SELECT last_event_at AS event_at FROM entity_edges WHERE edge_id='e1'"
    ))
    assert rows and rows[0][0] is None, "the edge has no event; the read must not invent one"
    src = projection_mod.__file__
    text = open(src, encoding="utf-8").read()
    assert "COALESCE(last_event_at, valid_from, created_at)" not in text
