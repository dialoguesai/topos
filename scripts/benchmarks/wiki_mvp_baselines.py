#!/usr/bin/env python3
"""Wiki MVP performance baseline capture (EN-P4-S1-04)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topos.query.pipeline import QueryPipelineOrchestrator  # noqa: E402
from topos.query.manifest import ScopeResolutionManifest  # noqa: E402
from topos.storage.adapters.fakes import (  # noqa: E402
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)
from topos.storage.adapters.factory import AdapterBundle  # noqa: E402


def _make_bundle() -> AdapterBundle:
    canonical = InMemoryCanonicalStore()
    for i in range(100):
        canonical.upsert(
            "conversation_messages",
            {"record_id": f"m{i}", "content": f"message {i}", "sender_id": "u1"},
        )
    signal = InMemorySignalFeatureStore()
    signal.put_summary({"dimension": "relationship", "summary_text": "baseline summary"})
    vector = InMemoryVectorIndex()
    for i in range(50):
        vector.upsert({"embedding_id": f"e{i}", "record_id": f"m{i}"}, vector=[0.1, 0.2])
    graph = InMemoryGraphEdgeStore()
    graph.upsert_node({"node_id": "n1"})
    graph.upsert_edge({"edge_id": "e1", "src_node_id": "n1", "dst_node_id": "n2"})
    return AdapterBundle(
        canonical=canonical,
        signal=signal,
        vector=vector,
        graph=graph,
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _messages_manifest() -> ScopeResolutionManifest:
    return ScopeResolutionManifest(
        scope_id="messages:read",
        primary_dimensions=["messages"],
        canonical_tables=["conversation_messages"],
        access_mode_ceiling="raw",
    )


async def _bench_queries(profile: str) -> dict:
    bundle = _make_bundle()
    orch = QueryPipelineOrchestrator(adapters=bundle)
    manifest = _messages_manifest()
    timings = []
    for _ in range(5):
        t0 = time.perf_counter()
        await orch.execute(
            query_text="show recent messages",
            scope_id="messages:read",
            access_mode="raw",
            manifest=manifest,
            query_session_id="bench",
        )
        timings.append((time.perf_counter() - t0) * 1000)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1] if timings else 0.0
    return {
        "profile": profile,
        "query_raw_p95_ms": round(p95, 2),
        "vector_metadata_count": len(bundle.vector.list_metadata(limit=500).items),
        "graph_edge_count": len(bundle.graph.list_graph(limit_edges=500)["edges"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "hosted"], default="local")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    import asyncio

    report = asyncio.run(_bench_queries(args.profile))
    report["ingest_1k_messages_s"] = None  # capture on staging with real ingest
    report["embed_throughput_msg_s"] = None

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
