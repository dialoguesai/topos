"""Messenger graph importance and community detection (Sprint 02)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

import logging

import networkx as nx

from .messenger_directed import create_directed_tables
from .messenger_graph import extract_messenger_graph
from ..storage.db.write_gate import batched_writes, commit_connection, with_db_write

logger = logging.getLogger(__name__)

MESSENGER_SOCIAL_EDGES_TABLE = "messenger_social_edges"
MESSENGER_PARTICIPANT_IMPORTANCE_TABLE = "messenger_participant_importance"
MESSENGER_COMMUNITIES_TABLE = "messenger_communities"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_scope(source_ids: Optional[Sequence[str]]) -> str:
    if not source_ids:
        return "all"
    normalized = sorted({str(s).strip() for s in source_ids if str(s).strip()})
    return ",".join(normalized) if normalized else "all"


def ensure_messenger_analytics_tables(conn: Any) -> None:
    """Create Sprint 02 derived messenger analytics tables."""
    # DDL takes SQLite's write lock at execute time — gate it with the commit
    # (write_gate lock-order inversion).
    with with_db_write():
        _create_messenger_analytics_tables(conn)
        commit_connection(conn)


def _add_column_if_missing(conn: Any, table: str, column: str, col_type: str) -> None:
    """Idempotent additive column. Safe to call on every ensure."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _create_messenger_analytics_tables(conn: Any) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_SOCIAL_EDGES_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            source_scope TEXT NOT NULL DEFAULT 'all',
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            weight REAL NOT NULL,
            edge_type TEXT,
            edge_type_counts_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, source_scope, source_id, target_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_SOCIAL_EDGES_TABLE}_dataset_period
        ON {MESSENGER_SOCIAL_EDGES_TABLE}(dataset_id, period_key, source_scope)
        """
    )
    # Per-edge connector provenance. Deliberately an additive ALTER here rather
    # than a registry migration: these three tables are created by this function
    # and appear nowhere in storage/db/migrations/registry.py, so they are already
    # feature-owned. Routing this through the registry would bump user_version
    # past what an installed engine understands and fence the node out of every
    # write — which is exactly what happened on 2026-08-25.
    _add_column_if_missing(conn, MESSENGER_SOCIAL_EDGES_TABLE, "source_counts_json", "TEXT")

    # L1 — the directed half. Same site, same gate, same reasoning about migrations.
    create_directed_tables(conn)

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            source_scope TEXT NOT NULL DEFAULT 'all',
            participant_id TEXT NOT NULL,
            centrality_degree REAL NOT NULL,
            centrality_betweenness REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, source_scope, participant_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}_dataset_period
        ON {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}(dataset_id, period_key, source_scope)
        """
    )

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_COMMUNITIES_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            source_scope TEXT NOT NULL DEFAULT 'all',
            participant_id TEXT NOT NULL,
            community_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, source_scope, participant_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_COMMUNITIES_TABLE}_dataset_period
        ON {MESSENGER_COMMUNITIES_TABLE}(dataset_id, period_key, source_scope)
        """
    )


def build_networkx_graph(
    period_payload: Dict[str, Any],
    exclude: Optional[Set[str]] = None,
) -> nx.Graph:
    """Build an undirected weighted graph from Sprint 01 period payload.

    `exclude` drops nodes and every edge touching them — used to remove the OWNER
    before centrality and community detection. The owner sits inside every
    conversation they are part of, so co-participation makes them adjacent to
    nearly everyone: they are the most central node in every period measured
    (degree 0.375-0.582), and the partition collapses around them.

    Measured on the live corpus 2026-08-26, removing that single node:

        period    communities        largest community
        2026-04   34 ->  53          35% -> 15%
        2026-05   40 ->  63          34% ->  6%
        2026-06   34 ->  54          37% -> 17%
        2026-07   31 ->  48          34% -> 18%
        2026-08   12 ->  37          58% -> 20%

    August's "community" of 58% of all participants was the owner's star, not a
    group of people who know each other. Every brokerage, bridge and circle number
    computed with the ego present describes the owner's own reach.
    """
    exclude = exclude or set()
    graph = nx.Graph()
    for node in period_payload.get("nodes", []):
        node_id = str(node.get("id") or "").strip()
        if not node_id or node_id in exclude:
            continue
        graph.add_node(node_id, **node)

    for edge in period_payload.get("edges", []):
        source_id = str(edge.get("source") or "").strip()
        target_id = str(edge.get("target") or "").strip()
        if not source_id or not target_id or source_id == target_id:
            continue
        if source_id in exclude or target_id in exclude:
            continue
        weight = float(edge.get("weight") or 0.0)
        graph.add_edge(source_id, target_id, weight=max(weight, 0.0))
    return graph


def compute_importance_and_communities(
    period_payload: Dict[str, Any],
    exclude: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Compute centrality metrics and Louvain communities for one period.

    `exclude` (the owner) is removed from the GRAPH but never from the stored
    EDGES: the edges are a factual record of who talked to whom and the owner's
    are the most important ones there, while centrality and communities are an
    interpretation of structure BETWEEN other people. Those are different
    questions and only the second one needs the ego gone.
    """
    graph = build_networkx_graph(period_payload, exclude=exclude)
    node_ids = list(graph.nodes())
    if not node_ids:
        return {"importance": {}, "communities": {}, "graph": graph}

    degree = nx.degree_centrality(graph)
    # Use unweighted betweenness to keep metric stable with strength-based edges.
    betweenness = nx.betweenness_centrality(graph, weight=None, normalized=True)

    if graph.number_of_edges() > 0:
        try:
            from community import community_louvain  # type: ignore

            communities = community_louvain.best_partition(graph, weight="weight", random_state=42)
        except Exception:
            # Fallback keeps pipeline functional if python-louvain is unavailable at runtime.
            communities = {}
            for idx, component in enumerate(nx.connected_components(graph)):
                for node_id in component:
                    communities[node_id] = idx
    else:
        communities = {node_id: idx for idx, node_id in enumerate(sorted(node_ids))}

    importance: Dict[str, Dict[str, float]] = {}
    for node_id in node_ids:
        importance[node_id] = {
            "centrality_degree": float(degree.get(node_id, 0.0)),
            "centrality_betweenness": float(betweenness.get(node_id, 0.0)),
        }
    return {
        "importance": importance,
        "communities": {k: int(v) for k, v in communities.items()},
        "graph": graph,
    }


def _owner_participant_ids(conn: Any) -> Set[str]:
    """Every contact id that IS the owner.

    Plural on purpose: the owner can hold more than one contact row. Live on this
    machine there are two — the canonical id and a `test-dataset:`-prefixed
    duplicate left by the phone-only matching in the dataset unification — and
    excluding only one of them would leave the ego in the graph under its other
    identity, which looks exactly like the fix working while it has not.
    """
    try:
        return {
            str(r[0])
            for r in conn.execute("SELECT contact_id FROM contacts WHERE is_self=1").fetchall()
            if r and r[0]
        }
    except Exception:  # noqa: BLE001 — a missing or older contacts table must not break analytics
        return set()


def _persist_period_results(
    conn: Any,
    *,
    dataset_id: str,
    period_key: str,
    source_scope: str,
    period_payload: Dict[str, Any],
    importance: Dict[str, Dict[str, float]],
    communities: Dict[str, int],
) -> Dict[str, int]:
    now = _utc_now()
    edge_rows = []
    for edge in period_payload.get("edges", []):
        source_id = str(edge.get("source") or "").strip()
        target_id = str(edge.get("target") or "").strip()
        if not source_id or not target_id:
            continue
        edge_rows.append(
            (
                dataset_id,
                period_key,
                source_scope,
                source_id,
                target_id,
                float(edge.get("weight") or 0.0),
                str(edge.get("edge_type") or ""),
                json.dumps(edge.get("edge_type_counts") or {}, ensure_ascii=False),
                json.dumps(edge.get("source_counts") or {}, ensure_ascii=False),
                now,
                now,
            )
        )

    importance_rows = []
    for participant_id, metrics in importance.items():
        importance_rows.append(
            (
                dataset_id,
                period_key,
                source_scope,
                participant_id,
                float(metrics.get("centrality_degree", 0.0)),
                float(metrics.get("centrality_betweenness", 0.0)),
                now,
                now,
            )
        )

    community_rows = []
    for participant_id, community_id in communities.items():
        community_rows.append(
            (
                dataset_id,
                period_key,
                source_scope,
                participant_id,
                int(community_id),
                now,
                now,
            )
        )
    # Rows are built above, outside the hold; only the deletes + inserts (one
    # commit at exit) hold the gate.
    with batched_writes(conn):
        conn.execute(
            f"""
            DELETE FROM {MESSENGER_SOCIAL_EDGES_TABLE}
            WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
            """,
            (dataset_id, period_key, source_scope),
        )
        conn.execute(
            f"""
            DELETE FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
            WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
            """,
            (dataset_id, period_key, source_scope),
        )
        conn.execute(
            f"""
            DELETE FROM {MESSENGER_COMMUNITIES_TABLE}
            WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
            """,
            (dataset_id, period_key, source_scope),
        )
        if edge_rows:
            conn.executemany(
                f"""
                INSERT INTO {MESSENGER_SOCIAL_EDGES_TABLE}
                (
                    dataset_id, period_key, source_scope, source_id, target_id,
                    weight, edge_type, edge_type_counts_json, source_counts_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                edge_rows,
            )
        if importance_rows:
            conn.executemany(
                f"""
                INSERT INTO {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
                (
                    dataset_id, period_key, source_scope, participant_id,
                    centrality_degree, centrality_betweenness, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                importance_rows,
            )
        if community_rows:
            conn.executemany(
                f"""
                INSERT INTO {MESSENGER_COMMUNITIES_TABLE}
                (
                    dataset_id, period_key, source_scope, participant_id,
                    community_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                community_rows,
            )
    return {
        "edges_written": len(edge_rows),
        "importance_written": len(importance_rows),
        "communities_written": len(community_rows),
    }


def compute_and_persist_messenger_analytics(
    *,
    dataset_id: str,
    conn: Optional[Any] = None,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    source_ids: Optional[Sequence[str]] = None,
    period_granularity: str = "month",
    cumulative: bool = False,
) -> Dict[str, Any]:
    """Run Sprint 01 extraction + Sprint 02 metrics and persist derived analytics."""
    if conn is not None:
        db = conn
    else:
        from ..core.state import get_db_connection

        db = get_db_connection()
    if db is None:
        raise RuntimeError("Database connection not available")

    ensure_messenger_analytics_tables(db)
    extraction = extract_messenger_graph(
        dataset_id=dataset_id,
        conn=db,
        start_ts=start_ts,
        end_ts=end_ts,
        source_ids=source_ids,
        period_granularity=period_granularity,
        cumulative=cumulative,
    )

    scope = _source_scope(source_ids)
    ego = _owner_participant_ids(db)
    periods_out: List[Dict[str, Any]] = []
    totals = {"edges_written": 0, "importance_written": 0, "communities_written": 0}

    for period_payload in extraction.get("periods", []):
        period_key = str(period_payload.get("period_key") or "")
        if not period_key:
            continue
        computed = compute_importance_and_communities(period_payload, exclude=ego)
        writes = _persist_period_results(
            db,
            dataset_id=dataset_id,
            period_key=period_key,
            source_scope=scope,
            period_payload=period_payload,
            importance=computed["importance"],
            communities=computed["communities"],
        )
        for key in totals:
            totals[key] += writes[key]
        periods_out.append(
            {
                "period_key": period_key,
                **writes,
                "nodes_count": len(period_payload.get("nodes", [])),
                "edges_count": len(period_payload.get("edges", [])),
            }
        )

    # L1 — the directed lane, computed inside the pass that already runs.
    #
    # Deliberately NOT a new trigger and NOT a second rebuild lifecycle. Two of the three
    # existing messenger triggers call this function synchronously, and prod CP is a single
    # uvicorn worker where added synchronous work starves every tenant. Converging the
    # messenger lane onto graph_materialization_state's debounce + flock + subprocess is real
    # work with its own failure modes; doing it as a side effect of L1 would put an
    # unreviewed refactor on the critical path of a single-worker service.
    #
    # A failure here must not lose the undirected results already computed above — the
    # directed lane is additive, and a partial answer beats no answer.
    directed_totals = {"directed_edges_written": 0, "dyads_written": 0}
    try:
        directed_totals = _compute_directed_lane(db, dataset_id, source_ids)
    except Exception as exc:  # noqa: BLE001 — additive lane, never fails the pass
        logger.warning("directed lane failed for %s: %s", dataset_id, exc)
        directed_totals["error"] = str(exc)[:200]
    totals.update(directed_totals)

    return {
        "dataset_id": dataset_id,
        "period_granularity": period_granularity,
        "source_scope": scope,
        "periods": periods_out,
        "totals": totals,
    }


def _compute_directed_lane(db: Any, dataset_id: str, source_ids: Optional[Sequence[str]]) -> Dict[str, int]:
    """Extract, persist and roll up L1's directed edges for one dataset."""
    from .messenger_directed import (DEFAULT_SESSION_GAP_SECONDS, build_dyad_stats,
                                     extract_directed_dyadic_edges, persist_directed_edges,
                                     persist_dyad_stats, rows_for_persist)

    # Safe standalone: the orchestrator ensures tables before calling, but this function is
    # also the unit under test and a future caller should not have to know the order.
    with with_db_write():
        create_directed_tables(db)
        commit_connection(db)

    # A single connector filter narrows the pass; several would need one pass each, and the
    # rollup is lifetime-wide either way, so the filter only applies to the edge lane.
    only = str(source_ids[0]) if source_ids and len(source_ids) == 1 else None
    acc = extract_directed_dyadic_edges(db, dataset_id, connector=only)
    from .messenger_directed import attach_affect
    rows = rows_for_persist(acc, dataset_id, DEFAULT_SESSION_GAP_SECONDS,
                            affect=attach_affect(db, dataset_id, acc))
    # BOTH computations complete BEFORE either persist. The first version persisted edges,
    # then computed and persisted dyads: a failure in the rollup left the two tables
    # describing different corpora while totals reported neither — a split brain the
    # adversarial pass demonstrated. Compute everything, then write; a failure now leaves
    # the previous consistent state intact.
    dyad_rows = build_dyad_stats(db, dataset_id)
    periods = sorted({r[1] for r in rows})
    edges = persist_directed_edges(db, dataset_id, rows, periods=periods)
    dyads = persist_dyad_stats(db, dataset_id, dyad_rows)
    # L1-8: fill the nullable person-id columns where identity is unambiguous. Runs after
    # persistence because it reads the tables it fills; abstains on ambiguity.
    from .messenger_directed import backfill_person_ids
    ident = backfill_person_ids(db, dataset_id)
    # G3: the calibrated warmth band is the authoritative label and is stored with the
    # dyad, so every reader — including ones that never import the kernel — gets ONE
    # answer to "what state is this relationship".
    banded = 0
    try:
        from ..features.derivation.social_kernels import _dyad_rows, compute_warmth
        from ..storage.db.write_gate import batched_writes

        bands = compute_warmth(_dyad_rows(db, dataset_id))
        with batched_writes(db):
            for b in bands:
                cur = db.execute(
                    "UPDATE messenger_dyad_stats SET warmth_band=? WHERE dataset_id=?"
                    " AND (a_key=? OR b_key=?) AND involves_self=1",
                    (b["warmth_band"], dataset_id, b["peer_key"], b["peer_key"]))
                banded += cur.rowcount
    except Exception as exc:  # noqa: BLE001 — labelling must not fail the lane
        logger.warning("warmth banding failed for %s: %s", dataset_id, exc)
    return {"directed_edges_written": edges, "dyads_written": dyads,
            "person_ids_resolved": ident.get("resolved", 0), "warmth_banded": banded}
