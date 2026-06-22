#!/usr/bin/env python3
"""Benchmark vector search latency across dataset sizes."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import time
from pathlib import Path

from topos.enrichment.job_writer import write_signal_records
from topos.features.signal.vector_codec import normalize_vector
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied


def _seed(conn: sqlite3.Connection, count: int) -> None:
    bundle = AdapterFactory.create("local_database", conn=conn)
    batch = []
    for i in range(count):
        vector = normalize_vector([random.random() for _ in range(3)])
        batch.append(
            {
                "record_id": f"bench-{i}",
                "source_id": "bench",
                "model": "bench-model",
                "provider": "bench",
                "vector": vector,
                "dims": 3,
                "chunk_index": 0,
            }
        )
        if len(batch) >= 200:
            write_signal_records("embeddings", batch, adapters=bundle, conn=conn)
            batch = []
    if batch:
        write_signal_records("embeddings", batch, adapters=bundle, conn=conn)


def _bench(conn: sqlite3.Connection, *, repeats: int = 20) -> dict[str, float]:
    from topos.storage.adapters.sqlite.vector_search import search_similar

    query = normalize_vector([0.9, 0.1, 0.0])
    timings: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        search_similar(conn, query, limit=20)
        timings.append((time.perf_counter() - start) * 1000)
    return {
        "p50_ms": statistics.median(timings),
        "p95_ms": sorted(timings)[max(0, int(len(timings) * 0.95) - 1)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="1000", help="Comma-separated vector counts")
    parser.add_argument("--output", default="-")
    args = parser.parse_args()
    report: dict[str, object] = {"backend": "brute_force_or_ann", "runs": []}
    for size_str in args.sizes.split(","):
        size = int(size_str.strip())
        db_path = Path(f"/tmp/topos_vector_bench_{size}.db")
        if db_path.exists():
            db_path.unlink()
        conn = sqlite3.connect(str(db_path))
        ensure_migrations_applied(conn)
        _seed(conn, size)
        stats = _bench(conn)
        conn.close()
        report["runs"].append({"size": size, **stats})
    payload = json.dumps(report, indent=2)
    if args.output == "-":
        print(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
