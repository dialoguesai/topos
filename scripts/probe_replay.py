"""Weekly retrieval drift probes against the live database.

Replays a FROZEN probe set read-only, recording per-probe top-10 record ids,
top-3 similarities, latency, and which backend served (ann vs brute force).
Appends one JSON line per run to eval_reports/probe_history.jsonl and reports
rank churn (top-10 Jaccard) against the previous run.

This is the drift instrument the 2026-07-06 audit called for: without it,
retrieval quality decay as data accumulates is invisible until users feel it.
Observability only — always exits 0 unless the DB can't be opened.

Usage:
    python scripts/probe_replay.py [--db ~/.topos/database.db] [--limit 10]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Frozen: comparability across runs is the whole point. Add probes at the
# END with a version bump; never edit existing ones.
PROBE_SET_VERSION = 1
FROZEN_PROBES = [
    "what have I been working on recently",
    "who do I talk to the most",
    "how do I spend my money",
    "what are my plans for next week",
    "how is my health and sleep",
    "what topics am I researching",
    "where did I work before",
    "what did I write in my journal last week",
]

HISTORY_PATH = REPO_ROOT / "eval_reports" / "probe_history.jsonl"


def _code_sha() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


def _previous_run(history_path: Path) -> Optional[Dict[str, Any]]:
    if not history_path.exists():
        return None
    last = None
    with history_path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


def _jaccard(a: List[str], b: List[str]) -> Optional[float]:
    sa, sb = set(a), set(b)
    if not (sa | sb):
        return None
    return len(sa & sb) / len(sa | sb)


def run_probes(conn: sqlite3.Connection, *, limit: int = 10) -> Dict[str, Any]:
    from topos.engine.backends.huggingface import (
        HuggingFaceAdapter,
        active_embedding_model,
        apply_embedding_prefix,
    )
    from topos.storage.adapters.sqlite.vector_search import (
        last_search_backend,
        search_similar,
    )

    adapter = HuggingFaceAdapter()
    model = active_embedding_model()
    probes_out: List[Dict[str, Any]] = []
    for probe in FROZEN_PROBES:
        text = apply_embedding_prefix([probe], model_name=model, input_role="query")[0]
        emb = adapter.run_inference({"texts": [text]}, {"subtype": "embedding", "model": model})
        vector = (emb.get("vectors") or [[]])[0]
        if not vector:
            probes_out.append({"probe": probe, "error": "embedding_failed"})
            continue
        t0 = time.time()
        hits, _total = search_similar(
            conn, [float(x) for x in vector], model=model, limit=limit
        )
        elapsed_ms = round((time.time() - t0) * 1000.0, 1)
        probes_out.append(
            {
                "probe": probe,
                "top_ids": [str(h.get("record_id")) for h in hits[:limit]],
                "top3_sim": [round(float(h.get("similarity") or 0.0), 4) for h in hits[:3]],
                "ms": elapsed_ms,
                "backend": last_search_backend(),
            }
        )
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "probe_set_version": PROBE_SET_VERSION,
        "model": model,
        "code_sha": _code_sha(),
        "probes": probes_out,
    }


def compare_with_previous(run: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not previous or previous.get("probe_set_version") != run.get("probe_set_version"):
        return {"rank_churn": None, "compared_to": None}
    prev_by_probe = {p.get("probe"): p for p in previous.get("probes", [])}
    overlaps: List[float] = []
    for probe in run["probes"]:
        prev = prev_by_probe.get(probe.get("probe"))
        if not prev or "top_ids" not in probe or "top_ids" not in prev:
            continue
        overlap = _jaccard(probe["top_ids"], prev["top_ids"])
        if overlap is not None:
            probe["top10_jaccard_vs_prev"] = round(overlap, 3)
            overlaps.append(overlap)
    mean_overlap = round(sum(overlaps) / len(overlaps), 3) if overlaps else None
    return {
        "rank_churn": round(1 - mean_overlap, 3) if mean_overlap is not None else None,
        "compared_to": previous.get("ts"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path.home() / ".topos" / "database.db"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--history", default=str(HISTORY_PATH))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1
    # Read-only: probes must never mutate the live node.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        from topos.storage.db.connection_tuning import load_sqlite_vec

        load_sqlite_vec(conn)  # ANN path works read-only when the table exists
        run = run_probes(conn, limit=args.limit)
    finally:
        conn.close()

    history_path = Path(args.history).expanduser()
    run["comparison"] = compare_with_previous(run, _previous_run(history_path))
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a") as fh:
        fh.write(json.dumps(run) + "\n")

    ok = [p for p in run["probes"] if "top_ids" in p]
    backends = {p.get("backend") for p in ok}
    latencies = sorted(p["ms"] for p in ok)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
    print(
        json.dumps(
            {
                "probes_ok": len(ok),
                "probes_total": len(run["probes"]),
                "backends": sorted(b for b in backends if b),
                "latency_p95_ms": p95,
                "rank_churn_vs_prev": run["comparison"]["rank_churn"],
                "history": str(history_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
