"""Structural clarity over the canonical, evidence-weighted graph.

v2 changes: the graph is built from the analytical projection (canonical
endpoints, relation-strength x decay x evidence weights, self node excluded),
communities are connectivity-refined after Louvain (the known failure mode
Leiden fixes: communities that are internally disconnected), and stability is
measured across seeds instead of assumed.
"""

from __future__ import annotations

import itertools
import sqlite3
from typing import Any, Dict, List, Optional, Set

import networkx as nx

from .canonical import CanonicalIndex
from .entropy import clamp_score, normalized_entropy
from .projection import AnalyticalProjection, EvidenceCache, ProjectionConfig
from .topic_merge import SuperTopicIndex

MIN_EDGES_FOR_WINDOW = 25
STABILITY_SEEDS = (42, 7, 1337)


def _build_graph(projection: AnalyticalProjection, canonical: CanonicalIndex) -> nx.Graph:
    g = nx.Graph()
    for edge in projection.edges():
        src_entity = canonical.get(edge.src)
        dst_entity = canonical.get(edge.dst)
        if src_entity is None or dst_entity is None:
            continue
        if src_entity.is_self or dst_entity.is_self:
            continue
        score = edge.score
        if score <= 0:
            continue
        if g.has_edge(edge.src, edge.dst):
            g[edge.src][edge.dst]["weight"] += score
        else:
            g.add_edge(edge.src, edge.dst, weight=score)
    return g


def _refined_communities(g: nx.Graph, seed: int) -> List[Set[str]]:
    """Louvain + split any internally disconnected community."""
    communities = nx.community.louvain_communities(g, weight="weight", seed=seed)
    refined: List[Set[str]] = []
    for community in communities:
        sub = g.subgraph(community)
        for component in nx.connected_components(sub):
            refined.append(set(component))
    return refined


def _partition_agreement(a: List[Set[str]], b: List[Set[str]]) -> float:
    """Fraction of node pairs (sampled via co-membership maps) that agree."""
    label_a: Dict[str, int] = {}
    for i, community in enumerate(a):
        for node in community:
            label_a[node] = i
    label_b: Dict[str, int] = {}
    for i, community in enumerate(b):
        for node in community:
            label_b[node] = i
    nodes = sorted(set(label_a) & set(label_b))
    if len(nodes) < 2:
        return 1.0
    agree = 0
    total = 0
    for x, y in itertools.combinations(nodes[:400], 2):
        same_a = label_a[x] == label_a[y]
        same_b = label_b[x] == label_b[y]
        agree += 1 if same_a == same_b else 0
        total += 1
    return agree / total if total else 1.0


def compute_structure_clarity(
    conn: sqlite3.Connection,
    *,
    canonical: Optional[CanonicalIndex] = None,
    supertopics: Optional[SuperTopicIndex] = None,
    window_days: Optional[int] = 90,
    half_life_days: Optional[float] = None,
    cache: Optional[EvidenceCache] = None,
) -> Dict[str, Any]:
    from .canonical import build_canonical_index
    from .topic_merge import build_supertopics

    canonical = canonical or build_canonical_index(conn)
    supertopics = supertopics or build_supertopics(conn)
    cache = cache or EvidenceCache(conn)

    projection = AnalyticalProjection(
        conn,
        ProjectionConfig(window_days=window_days, half_life_days=half_life_days),
        canonical=canonical,
        supertopics=supertopics,
        cache=cache,
    )
    graph = _build_graph(projection, canonical)
    used_window: Optional[int] = window_days
    if graph.number_of_edges() < MIN_EDGES_FOR_WINDOW and window_days is not None:
        lifetime = AnalyticalProjection(
            conn,
            ProjectionConfig(window_days=None, half_life_days=half_life_days),
            canonical=canonical,
            supertopics=supertopics,
            cache=cache,
        )
        lifetime_graph = _build_graph(lifetime, canonical)
        if lifetime_graph.number_of_edges() > graph.number_of_edges():
            graph = lifetime_graph
            used_window = None

    n_nodes = graph.number_of_nodes()
    if n_nodes < 2:
        return {
            "score": 0.0,
            "interpretation": "Not enough graph structure yet to measure clarity.",
            "sub_metrics": {"node_count": n_nodes, "edge_count": graph.number_of_edges()},
        }

    partitions: List[List[Set[str]]] = []
    modularity = 0.0
    try:
        for seed in STABILITY_SEEDS:
            partitions.append(_refined_communities(graph, seed))
        modularity = float(
            nx.community.modularity(graph, partitions[0], weight="weight")
        )
    except Exception:
        partitions = []

    stability = 1.0
    if len(partitions) >= 2:
        agreements = [
            _partition_agreement(partitions[0], other) for other in partitions[1:]
        ]
        stability = sum(agreements) / len(agreements)

    degrees = [float(d) for _, d in graph.degree(weight="weight")]
    degree_entropy_norm = normalized_entropy(degrees)

    components = list(nx.connected_components(graph))
    largest = max((len(c) for c in components), default=0)
    giant_fraction = largest / n_nodes if n_nodes else 0.0
    giant_penalty = max(0.0, (giant_fraction - 0.85) * 60.0)

    modularity_norm = clamp_score(modularity * 100.0, hi=100.0)
    degree_clarity = clamp_score(100.0 * (1.0 - degree_entropy_norm))
    raw = 0.45 * modularity_norm + 0.30 * degree_clarity + 20.0 * stability - giant_penalty
    score = clamp_score(raw)

    if score >= 70:
        interpretation = "Your knowledge graph has distinct, stable communities and readable structure."
    elif score >= 45:
        interpretation = "Some structure is emerging — entity resolution and enrichment may sharpen boundaries."
    else:
        interpretation = "Graph connections look diffuse — resolve entities or add richer sources."

    return {
        "score": round(score, 1),
        "interpretation": interpretation,
        "sub_metrics": {
            "node_count": n_nodes,
            "edge_count": graph.number_of_edges(),
            "modularity": round(modularity, 4),
            "community_count": len(partitions[0]) if partitions else 0,
            "partition_stability": round(stability, 4),
            "degree_entropy_normalized": round(degree_entropy_norm, 4),
            "giant_component_fraction": round(giant_fraction, 4),
            "window_days": used_window,
        },
    }
