"""Disclosure Decision Record (plan §A.3).

Per-response instrumentation of *what the firewall disclosed, what it dropped, and how long
each stage took*. One record powers minimality scoring, the "what got sparser" dense evals,
the latency waterfall, and slide numbers — without re-deriving from raw responses.

The record is attached to the query audit event (internal; not returned to a grantee) and,
under a debug flag, surfaced on the pipeline result for the eval harness.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Context-packet keys that carry disclosable artifacts. Lists are counted by length; `graph`
# is counted by nodes + edges. Kept explicit so a new artifact type is a deliberate addition.
_LIST_ARTIFACT_KEYS = ("rows", "summaries", "scores", "semantic_hits", "topic_clusters", "messages", "facts")


def now_ms() -> float:
    """Monotonic wall-clock in milliseconds for stage timing."""
    return time.perf_counter() * 1000.0


def count_artifacts(packet: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Count disclosable artifacts per type in a context packet (0-safe, only non-empty types)."""
    if not isinstance(packet, dict):
        return {}
    counts: Dict[str, int] = {}
    for key in _LIST_ARTIFACT_KEYS:
        value = packet.get(key)
        if isinstance(value, list) and value:
            counts[key] = len(value)
    graph = packet.get("graph")
    if isinstance(graph, dict):
        n = len(graph.get("nodes") or []) + len(graph.get("edges") or [])
        if n:
            counts["graph"] = n
    return counts


def diff_dropped(
    artifacts_in: Dict[str, int],
    artifacts_out: Dict[str, int],
    *,
    reasons: List[str],
) -> List[Dict[str, Any]]:
    """Per-artifact-type reductions from retrieval → post-disclosure, annotated with the
    filters that ran (the authoritative "what happened", from FilteredContext.filters_applied)."""
    dropped: List[Dict[str, Any]] = []
    for key, in_count in artifacts_in.items():
        out_count = artifacts_out.get(key, 0)
        if in_count > out_count:
            dropped.append(
                {
                    "artifact_type": key,
                    "count": in_count - out_count,
                    "reasons": list(reasons),
                }
            )
    return dropped


@dataclass
class StageTimings:
    """Accumulates per-stage durations (ms). Unset stages are omitted from the record."""

    retrieval_ms: Optional[float] = None
    deterministic_filter_ms: Optional[float] = None
    minimizer_ms: Optional[float] = None
    game_layer_ms: Optional[float] = None
    inference_ms: Optional[float] = None
    negotiation_round_ms: Optional[float] = None
    total_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, float]:
        return {k: round(v, 2) for k, v in self.__dict__.items() if v is not None}


@dataclass
class DisclosureDecisionRecord:
    tier: str
    mode: str
    scope: str
    artifacts_in: Dict[str, int] = field(default_factory=dict)
    artifacts_out: Dict[str, int] = field(default_factory=dict)
    dropped: List[Dict[str, Any]] = field(default_factory=list)
    filters_applied: List[str] = field(default_factory=list)
    minimizer: Dict[str, Any] = field(default_factory=lambda: {"ran": False})
    backstop_hits: List[str] = field(default_factory=list)
    timings: StageTimings = field(default_factory=StageTimings)
    # Which quality tier actually served retrieval (ann vs brute-force scan,
    # rerank ran or degraded) — silent fallbacks were invisible before.
    retrieval: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "mode": self.mode,
            "scope": self.scope,
            "artifacts_in": dict(self.artifacts_in),
            "artifacts_out": dict(self.artifacts_out),
            "dropped": list(self.dropped),
            "filters_applied": list(self.filters_applied),
            "minimizer": dict(self.minimizer),
            "backstop_hits": list(self.backstop_hits),
            "timings": self.timings.to_dict(),
            "retrieval": dict(self.retrieval),
        }


def build_disclosure_decision_record(
    *,
    tier: str,
    mode: str,
    scope: str,
    retrieval_packet: Optional[Dict[str, Any]],
    filtered_packet: Optional[Dict[str, Any]],
    filters_applied: List[str],
    timings: StageTimings,
    minimizer: Optional[Dict[str, Any]] = None,
    backstop_hits: Optional[List[str]] = None,
    retrieval: Optional[Dict[str, Any]] = None,
) -> DisclosureDecisionRecord:
    artifacts_in = count_artifacts(retrieval_packet)
    artifacts_out = count_artifacts(filtered_packet)
    return DisclosureDecisionRecord(
        tier=tier,
        mode=mode,
        scope=scope,
        artifacts_in=artifacts_in,
        artifacts_out=artifacts_out,
        dropped=diff_dropped(artifacts_in, artifacts_out, reasons=filters_applied),
        filters_applied=list(filters_applied),
        minimizer=minimizer or {"ran": False},
        backstop_hits=list(backstop_hits or []),
        timings=timings,
        retrieval=dict(retrieval or {}),
    )
