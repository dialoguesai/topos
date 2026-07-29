"""Influence Threads v2.

v1 ranked pairs purely by lagged mutual information over series read from
`timeline.entity_ids_json` — a column that is empty on live nodes. v2:

1. builds an analytical influence graph (canonical entities + supertopics,
   relation-typed, decayed, evidence-weighted),
2. generates candidates with Personalized PageRank seeded from what currently
   matters (or an explicit target),
3. reconstructs the strongest time-respecting path per candidate pair,
4. scores evidence with per-source diminishing returns and independence,
5. labels every thread with an epistemic status instead of presenting every
   graph path as an "influence".

Lagged MI is retained as a direction hint computed on real weekly series.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx
import numpy as np
from sklearn.metrics import mutual_info_score

from .canonical import CanonicalIndex, build_canonical_index
from .projection import AnalyticalProjection, EvidenceCache, ProjectionConfig
from .snapshots import week_starts
from .topic_merge import SuperTopicIndex, build_supertopics

MAX_ENTITY_NODES = 150
MAX_TOPIC_NODES = 60
MAX_HOPS = 3
PATH_LENGTH_ATTENUATION = 0.7
CO_OCCURRENCE_STRENGTH = 0.5
TOPIC_SIMILARITY_RELATION_STRENGTH = 0.2
MIN_TOPIC_SIMILARITY = 0.30
# Pathless (speculative) threads keep a small PPR-derived base score instead
# of hard zero, so the speculative_similarity status is actually reachable.
SPECULATIVE_BASE = 0.05
SPECIFIC_RELATION_MIN_STRENGTH = 0.5
MI_WEEKS = 26
# Direction hints need real temporal support on both series, otherwise lagged
# MI asymmetry on sparse counts is noise.
MIN_DIRECTION_SUPPORT_WEEKS = 4
# Candidates need at least this many evidence records in the window: pure
# PPR-lift ranking favors one-mention junk entities (maximum obscurity =
# maximum lift).
MIN_CANDIDATE_RECORDS = 2

EPISTEMIC_DIRECT = "direct_evidence"
EPISTEMIC_STRONG = "strongly_supported"
EPISTEMIC_STRUCTURAL = "structural_association"
EPISTEMIC_SPECULATIVE = "speculative_similarity"


def _discretize(series: List[int]) -> List[int]:
    if not series:
        return []
    arr = np.asarray(series, dtype=float)
    if arr.max() == arr.min():
        return [1] * len(series)
    tertiles = np.quantile(arr, [0.33, 0.66])
    return [0 if v <= tertiles[0] else (1 if v <= tertiles[1] else 2) for v in arr]


def _lagged_mi(x: List[int], y: List[int]) -> Tuple[float, float, float]:
    if len(x) < 3 or len(y) < 3:
        return 0.0, 0.0, 0.0
    forward = float(mutual_info_score(_discretize(x[:-1]), _discretize(y[1:])))
    backward = float(mutual_info_score(_discretize(y[:-1]), _discretize(x[1:])))
    return max(forward, backward), forward, backward


class _InfluenceGraph:
    """Analytical influence graph plus the evidence needed to score threads."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        canonical: CanonicalIndex,
        supertopics: SuperTopicIndex,
        projection: AnalyticalProjection,
    ) -> None:
        self.canonical = canonical
        self.supertopics = supertopics
        self.projection = projection
        self.graph = nx.Graph()
        self.node_kind: Dict[str, str] = {}
        self.node_evidence: Dict[str, float] = {}
        self.node_records: Dict[str, int] = {}
        # (node_a, node_b) sorted -> list of (record_id, source_id, event_at)
        self.pair_records: Dict[Tuple[str, str], List[Tuple[str, str, Optional[datetime]]]] = {}
        self._record_topics: Dict[str, Set[str]] = {}
        self._build()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        entity_evidence = self.projection.entity_evidence()
        eligible = {
            canonical_id: item
            for canonical_id, item in entity_evidence.items()
            if (entity := self.canonical.get(canonical_id)) is not None
            and not entity.is_self
            and entity.canonical_type != "conversation"
        }
        top_entities = sorted(eligible.items(), key=lambda kv: -kv[1].weight)[:MAX_ENTITY_NODES]
        for canonical_id, item in top_entities:
            self.graph.add_node(canonical_id)
            self.node_kind[canonical_id] = "entity"
            self.node_evidence[canonical_id] = item.weight
            self.node_records[canonical_id] = item.record_count

        topic_evidence = self.projection.topic_evidence()
        top_topics = sorted(topic_evidence.items(), key=lambda kv: -kv[1].weight)[:MAX_TOPIC_NODES]
        for topic_id, item in top_topics:
            self.graph.add_node(topic_id)
            self.node_kind[topic_id] = "topic"
            self.node_evidence[topic_id] = item.weight
            self.node_records[topic_id] = item.record_count

        for st in self.supertopics.supertopics.values():
            if st.supertopic_id in self.node_kind:
                for record_id in st.record_ids:
                    self._record_topics.setdefault(record_id, set()).add(st.supertopic_id)

        self._add_entity_edges()
        self._add_co_occurrence_edges()
        self._add_topic_similarity_edges()

    def _add_edge(
        self,
        a: str,
        b: str,
        *,
        volume: float,
        relation: str,
        strength: float,
        event_at: Optional[datetime],
    ) -> None:
        """volume = semantics-free evidence mass; strength = relation semantics.

        Kept separate so path scoring is strength x saturated(volume) — folding
        strength into the accumulated weight double-counted semantics (loop-5
        review finding). `weight` (= strength x volume) is what PageRank walks.
        """
        if a == b or volume <= 0:
            return
        if not self.graph.has_node(a) or not self.graph.has_node(b):
            return
        if self.graph.has_edge(a, b):
            data = self.graph[a][b]
            data["volume"] += volume
            if strength > data["strength"]:
                data["strength"] = strength
                data["relation"] = relation
            if event_at is not None and (data["event_at"] is None or event_at > data["event_at"]):
                data["event_at"] = event_at
            data["weight"] = data["strength"] * data["volume"]
        else:
            self.graph.add_edge(
                a,
                b,
                volume=volume,
                weight=strength * volume,
                relation=relation,
                strength=strength,
                event_at=event_at,
            )

    def _add_entity_edges(self) -> None:
        for edge in self.projection.edges():
            self._add_edge(
                edge.src,
                edge.dst,
                volume=edge.volume,
                relation=edge.edge_type,
                strength=edge.strength,
                event_at=edge.event_at,
            )

    def _add_co_occurrence_edges(self) -> None:
        records = self.projection.records()
        record_entities = self.projection.record_entity_map()
        accum: Dict[Tuple[str, str], List[Tuple[str, float]]] = {}
        for record_id, entity_ids in record_entities.items():
            rec = records.get(record_id)
            if rec is None:
                continue
            topics = self._record_topics.get(record_id, set())
            present_entities = [e for e in entity_ids if self.graph.has_node(e)]
            for entity_id in present_entities:
                for topic_id in topics:
                    key = tuple(sorted((entity_id, topic_id)))
                    accum.setdefault(key, []).append((rec.source_id, rec.weight))
                    self.pair_records.setdefault(key, []).append(
                        (record_id, rec.source_id, rec.event_at)
                    )
            # entity-entity co-mentions also carry pair evidence for status checks
            for i, a in enumerate(present_entities):
                for b in present_entities[i + 1 :]:
                    key = tuple(sorted((a, b)))
                    self.pair_records.setdefault(key, []).append(
                        (record_id, rec.source_id, rec.event_at)
                    )

        from .projection import damped_source_sum

        for (a, b), contributions in accum.items():
            volume = damped_source_sum(contributions)
            times = [t for _, _, t in self.pair_records.get((a, b), []) if t is not None]
            times.sort()
            median_time = times[len(times) // 2] if times else None
            self._add_edge(
                a,
                b,
                volume=volume,
                relation="co_occurs_in_records",
                strength=CO_OCCURRENCE_STRENGTH,
                event_at=median_time,
            )

    def _add_topic_similarity_edges(self) -> None:
        for (cluster_a, cluster_b), sim in self.supertopics.similarity.items():
            if sim < MIN_TOPIC_SIMILARITY:
                continue
            st_a = self.supertopics.resolve(cluster_a)
            st_b = self.supertopics.resolve(cluster_b)
            if st_a is None or st_b is None or st_a == st_b:
                continue
            self._add_edge(
                st_a,
                st_b,
                volume=sim,
                relation="semantically_similar",
                strength=TOPIC_SIMILARITY_RELATION_STRENGTH,
                event_at=None,
            )

    # -- helpers -------------------------------------------------------------

    def label_of(self, node_id: str) -> str:
        if self.node_kind.get(node_id) == "topic":
            return self.supertopics.label_of(node_id)
        return self.canonical.name_of(node_id)

    @staticmethod
    def edge_factor(data: Dict[str, Any]) -> float:
        """Scale-free edge quality in (0, 1): semantics x saturating volume.

        Normalizing by the global max weight let bulky co-occurrence edges
        outrank explicit worked_on/communicates_with relations (loop-3
        finding); v/(1+v) keeps volume saturating so relation strength decides.
        Volume is semantics-free, so strength enters exactly once.
        """
        v = float(data.get("volume") or 0.0)
        return float(data.get("strength") or 0.0) * (v / (1.0 + v))

    def personalized_pagerank(self, seeds: Dict[str, float]) -> Dict[str, float]:
        seeds = {n: w for n, w in seeds.items() if self.graph.has_node(n) and w > 0}
        if not seeds or self.graph.number_of_edges() == 0:
            return {}
        try:
            return nx.pagerank(self.graph, personalization=seeds, weight="weight")
        except Exception:
            return {}

    def global_pagerank(self) -> Dict[str, float]:
        if self.graph.number_of_edges() == 0:
            return {}
        try:
            return nx.pagerank(self.graph, weight="weight")
        except Exception:
            return {}

    def best_time_respecting_path(
        self, source: str, target: str
    ) -> Tuple[Optional[List[str]], float, bool]:
        """Best path source -> target with non-decreasing edge times.

        Returns (path nodes, path score, time_respecting). Edges without a
        timestamp are allowed but make the path non-time-respecting. If no
        time-respecting path exists, falls back to the best unconstrained path
        (labeled accordingly).
        """

        def search(require_time_order: bool) -> Tuple[Optional[List[str]], float, bool]:
            best_path: Optional[List[str]] = None
            best_score = 0.0
            best_timed = False

            def dfs(
                node: str,
                path: List[str],
                score: float,
                last_time: Optional[datetime],
                all_timed: bool,
            ) -> None:
                nonlocal best_path, best_score, best_timed
                if score <= best_score:
                    return  # every extension only multiplies by factors <= 1
                if node == target and len(path) > 1:
                    attenuated = score * (PATH_LENGTH_ATTENUATION ** (len(path) - 2))
                    if attenuated > best_score:
                        best_score = attenuated
                        best_path = list(path)
                        best_timed = all_timed
                    return
                if len(path) > MAX_HOPS:
                    return
                for neighbor in self.graph.neighbors(node):
                    if neighbor in path:
                        continue
                    data = self.graph[node][neighbor]
                    edge_time = data.get("event_at")
                    if require_time_order and edge_time is not None and last_time is not None:
                        if edge_time < last_time:
                            continue
                    w_norm = self.edge_factor(data)
                    if w_norm <= 0:
                        continue
                    dfs(
                        neighbor,
                        path + [neighbor],
                        score * w_norm,
                        edge_time if edge_time is not None else last_time,
                        all_timed and edge_time is not None,
                    )

            dfs(source, [source], 1.0, None, True)
            return best_path, best_score, best_timed

        path, score, timed = search(require_time_order=True)
        if path is not None:
            return path, score, timed
        path, score, _ = search(require_time_order=False)
        return path, score, False

    def counterfactual_impact(
        self, seed: str, node: str, base_ppr: Dict[str, float]
    ) -> float:
        """How much the seed's PPR neighborhood changes without `node`.

        Total-variation distance between the seed's personalized PageRank with
        and without the candidate — "how much would the explanation change if
        this node were absent". 0 = redundant, 1 = load-bearing.
        """
        if seed == node or not self.graph.has_node(node) or not base_ppr:
            return 0.0
        reduced = self.graph.copy()
        reduced.remove_node(node)
        if not reduced.has_node(seed) or reduced.number_of_edges() == 0:
            return 1.0
        try:
            ppr_without = nx.pagerank(reduced, personalization={seed: 1.0}, weight="weight")
        except Exception:
            return 0.0
        keys = (set(base_ppr) | set(ppr_without)) - {node}
        base_total = sum(base_ppr.get(k, 0.0) for k in keys)
        if base_total <= 0:
            return 0.0
        return 0.5 * sum(
            abs(base_ppr.get(k, 0.0) / base_total - ppr_without.get(k, 0.0)) for k in keys
        )

    def pair_evidence(self, a: str, b: str) -> Dict[str, Any]:
        records = self.pair_records.get(tuple(sorted((a, b))), [])
        source_days: Set[Tuple[str, str]] = set()
        sources: Set[str] = set()
        times: List[datetime] = []
        for _, source_id, event_at in records:
            day = event_at.strftime("%Y-%m-%d") if event_at else "unknown"
            source_days.add((source_id or "unknown", day))
            sources.add(source_id or "unknown")
            if event_at is not None:
                times.append(event_at)
        n_groups = len(source_days)
        return {
            "record_count": len(records),
            "independent_evidence": round(1.0 + math.log(n_groups), 3) if n_groups else 0.0,
            "independent_source_count": len(sources),
            "first_seen": min(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None,
            "last_seen": max(times).strftime("%Y-%m-%dT%H:%M:%SZ") if times else None,
        }

    def has_specific_edge(self, a: str, b: str) -> bool:
        if not self.graph.has_edge(a, b):
            return False
        data = self.graph[a][b]
        return (
            data["strength"] >= SPECIFIC_RELATION_MIN_STRENGTH
            and data["relation"] != "co_occurs_in_records"
        )


def _epistemic_status(
    graph: _InfluenceGraph,
    source: str,
    target: str,
    path: Optional[List[str]],
    time_respecting: bool,
    evidence: Dict[str, Any],
) -> str:
    if path is None:
        return EPISTEMIC_SPECULATIVE
    if len(path) == 2 and graph.has_specific_edge(source, target):
        return EPISTEMIC_DIRECT
    if (
        time_respecting
        and evidence["record_count"] >= 1
        and evidence["independent_source_count"] >= 2
    ):
        return EPISTEMIC_STRONG
    return EPISTEMIC_STRUCTURAL


def _resolve_target(
    target: str, graph: _InfluenceGraph
) -> Optional[str]:
    if graph.graph.has_node(target):
        return target
    wanted = target.strip().lower()
    for node in graph.graph.nodes:
        if graph.label_of(node).strip().lower() == wanted:
            return node
    return None


def compute_influence_threads(
    conn: sqlite3.Connection,
    *,
    weeks: int = 12,
    window_days: int = 90,
    half_life_days: Optional[float] = None,
    top_k: int = 5,
    target: Optional[str] = None,
    canonical: Optional[CanonicalIndex] = None,
    supertopics: Optional[SuperTopicIndex] = None,
    cache: Optional[EvidenceCache] = None,
) -> List[Dict[str, Any]]:
    canonical = canonical or build_canonical_index(conn)
    supertopics = supertopics or build_supertopics(conn)
    projection = AnalyticalProjection(
        conn,
        ProjectionConfig(window_days=window_days, half_life_days=half_life_days),
        canonical=canonical,
        supertopics=supertopics,
        cache=cache,
    )
    graph = _InfluenceGraph(conn, canonical, supertopics, projection)
    if graph.graph.number_of_edges() == 0:
        return []

    starts = week_starts(weeks=max(weeks, MI_WEEKS))
    entity_series, topic_series = projection.weekly_series(starts)

    def series_for(node: str) -> List[int]:
        if graph.node_kind.get(node) == "topic":
            return topic_series.get(node, [])
        return entity_series.get(node, [])

    if target is not None:
        resolved = _resolve_target(str(target), graph)
        if resolved is None:
            return []
        seed_nodes = [resolved]
        candidates_per_seed = max(top_k, 5)
    else:
        # Seed with a mix of kinds: topic evidence sums over whole records and
        # dwarfs mention-based entity evidence, so a pure evidence ranking
        # would seed only topics and every thread would be a 1-hop
        # entity-topic co-occurrence (live loop-2 finding).
        ranked_topics = sorted(
            (
                (node, weight)
                for node, weight in graph.node_evidence.items()
                if graph.node_kind.get(node) == "topic"
            ),
            key=lambda kv: -kv[1],
        )
        ranked_entities = sorted(
            (
                (node, weight)
                for node, weight in graph.node_evidence.items()
                if graph.node_kind.get(node) == "entity"
            ),
            key=lambda kv: -kv[1],
        )
        entity_seeds: List[str] = []
        seen_types: Set[str] = set()
        for node, _ in ranked_entities:
            entity_type = canonical.type_of(node)
            if entity_type in seen_types and len(entity_seeds) < 3:
                continue
            entity_seeds.append(node)
            seen_types.add(entity_type)
            if len(entity_seeds) >= 3:
                break
        if len(entity_seeds) < 3:
            for node, _ in ranked_entities:
                if node not in entity_seeds:
                    entity_seeds.append(node)
                if len(entity_seeds) >= 3:
                    break
        seed_nodes = [node for node, _ in ranked_topics[:2]] + entity_seeds
        candidates_per_seed = 3

    global_pr = graph.global_pagerank()
    threads_by_seed: Dict[str, List[Dict[str, Any]]] = {}
    seen_pairs: Set[Tuple[str, str]] = set()
    for seed in seed_nodes:
        ppr = graph.personalized_pagerank({seed: 1.0})
        if not ppr:
            continue
        # Rank candidates by geometric mean of PPR and lift-over-global-
        # PageRank. Raw PPR surfaces the same generic hubs ("Personal Life")
        # from every seed; pure lift favors one-mention junk (maximum
        # obscurity = maximum lift). The blend keeps seed-specific nodes that
        # still carry real evidence; a record floor removes extraction noise.
        def rank_score(node: str, score: float) -> float:
            lift = score / max(global_pr.get(node, 1e-9), 1e-9)
            return math.sqrt(max(score, 0.0) * max(lift, 0.0))

        candidates = sorted(
            (
                (node, score, score / max(global_pr.get(node, 1e-9), 1e-9))
                for node, score in ppr.items()
                if node != seed
                and graph.node_records.get(node, 0) >= MIN_CANDIDATE_RECORDS
            ),
            key=lambda kv: -rank_score(kv[0], kv[1]),
        )[: candidates_per_seed * 3]
        max_ppr = max((score for _, score, _ in candidates), default=1.0)
        taken = 0
        for candidate, ppr_score, lift in candidates:
            if taken >= candidates_per_seed:
                break
            pair_key = tuple(sorted((seed, candidate)))
            if pair_key in seen_pairs:
                continue
            path, path_score, time_respecting = graph.best_time_respecting_path(
                candidate, seed
            )
            evidence = graph.pair_evidence(seed, candidate)
            status = _epistemic_status(
                graph, candidate, seed, path, time_respecting, evidence
            )
            series_candidate = series_for(candidate)
            series_seed = series_for(seed)
            mi, fwd, bwd = _lagged_mi(series_candidate, series_seed)
            candidate_label = graph.label_of(candidate)
            seed_label = graph.label_of(seed)
            support = min(
                sum(1 for v in series_candidate if v > 0),
                sum(1 for v in series_seed if v > 0),
            )
            if support < MIN_DIRECTION_SUPPORT_WEEKS:
                direction = "bidirectional"
            elif fwd > bwd * 1.25:
                direction = f"{candidate_label} → {seed_label}"
            elif bwd > fwd * 1.25:
                direction = f"{seed_label} → {candidate_label}"
            else:
                direction = "bidirectional"

            evidence_factor = min(1.0, evidence["independent_evidence"] / 3.0)
            ppr_norm = ppr_score / max_ppr if max_ppr > 0 else 0.0
            effective_path_score = (
                path_score if path is not None else SPECULATIVE_BASE * ppr_norm
            )
            strength = round(
                effective_path_score * (0.4 + 0.4 * evidence_factor + 0.2 * ppr_norm), 4
            )
            if strength <= 0:
                continue
            # Counterfactual reading only in target mode: the graph copy per
            # candidate is cheap at this scale but pointless for the broad
            # "what matters now" listing.
            counterfactual: Optional[float] = None
            if target is not None:
                counterfactual = round(
                    graph.counterfactual_impact(seed, candidate, ppr), 4
                )
            seen_pairs.add(pair_key)
            taken += 1
            threads_by_seed.setdefault(seed, []).append(
                {
                    "strength": strength,
                    "epistemic_status": status,
                    "time_respecting": time_respecting,
                    "source_kind": graph.node_kind.get(candidate, "entity"),
                    "source_id": candidate,
                    "source_label": candidate_label,
                    "target_kind": graph.node_kind.get(seed, "entity"),
                    "target_id": seed,
                    "target_label": seed_label,
                    "direction_hint": direction,
                    "path": [graph.label_of(n) for n in (path or [candidate, seed])],
                    "path_relations": _path_relations(graph, path),
                    "evidence": evidence,
                    "components": {
                        "path_score": round(path_score, 4),
                        "ppr": round(ppr_norm, 4),
                        "ppr_lift": round(lift, 2),
                        "evidence_factor": round(evidence_factor, 4),
                        "forward_mi": round(fwd, 4),
                        "backward_mi": round(bwd, 4),
                        **(
                            {"counterfactual_impact": counterfactual}
                            if counterfactual is not None
                            else {}
                        ),
                    },
                }
            )

    status_rank = {
        EPISTEMIC_DIRECT: 0,
        EPISTEMIC_STRONG: 1,
        EPISTEMIC_STRUCTURAL: 2,
        EPISTEMIC_SPECULATIVE: 3,
    }

    def thread_order(t: Dict[str, Any]) -> Tuple[int, float]:
        return (status_rank.get(t["epistemic_status"], 9), -t["strength"])

    for seed_threads in threads_by_seed.values():
        seed_threads.sort(key=thread_order)

    # Round-robin across seeds so one dominant seed cannot fill the whole list
    # (a global sort let hub-topic threads crowd out person/project threads).
    threads: List[Dict[str, Any]] = []
    ordered_seeds = [seed for seed in seed_nodes if seed in threads_by_seed]
    round_index = 0
    while len(threads) < top_k:
        added = False
        for seed in ordered_seeds:
            seed_threads = threads_by_seed[seed]
            if round_index < len(seed_threads):
                threads.append(seed_threads[round_index])
                added = True
                if len(threads) >= top_k:
                    break
        if not added:
            break
        round_index += 1
    threads.sort(key=thread_order)
    return threads[:top_k]


def _path_relations(graph: _InfluenceGraph, path: Optional[List[str]]) -> List[str]:
    if not path or len(path) < 2:
        return []
    relations: List[str] = []
    for a, b in zip(path, path[1:]):
        if graph.graph.has_edge(a, b):
            relations.append(str(graph.graph[a][b].get("relation", "related")))
        else:
            relations.append("related")
    return relations
