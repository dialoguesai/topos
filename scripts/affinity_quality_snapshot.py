"""Read-only affinity quality scorecard against a node DB.

Emits a JSON snapshot of:
  - derived_spec hashes (comparability gate for before/after)
  - latest affinity_recompute_log calibration row
  - population / centroid coverage
  - automated contamination rates on active semantic_affinity edges
    (alias collisions, shared-source pairs, near-identical centroids,
    co-occurrence residuals)
  - a pairs_sample with empty ``label`` fields for optional human labeling

Optionally appends to eval_reports/affinity_quality_history.jsonl and
prints a diff vs the previous run (edge-set Jaccard + rate deltas).

Observability only — always exits 0 unless the DB can't be opened.
Never writes to the node database.

Usage:
    python scripts/affinity_quality_snapshot.py [--db ~/.topos/database.db]
    python scripts/affinity_quality_snapshot.py --db copy.db --no-history
    python scripts/affinity_quality_snapshot.py --db copy.db --out /tmp/snap.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HISTORY_PATH = REPO_ROOT / "eval_reports" / "affinity_quality_history.jsonl"


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


def _summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    metrics = snapshot.get("pair_metrics") or {}
    calibration = snapshot.get("calibration") or {}
    latest = calibration.get("latest_recompute") or {}
    comparison = snapshot.get("comparison") or {}
    pop = snapshot.get("population") or {}
    return {
        "status": snapshot.get("status"),
        "status_note": snapshot.get("status_note"),
        "active_edges": metrics.get("active_edges"),
        "alias_collision_rate": metrics.get("alias_collision_rate"),
        "shared_source_rate": metrics.get("shared_source_rate"),
        "near_identity_rate": metrics.get("near_identity_rate"),
        "co_occurrence_residual_rate": metrics.get("co_occurrence_residual_rate"),
        "configured_percentile": calibration.get("configured_percentile"),
        "resolved_cosine": latest.get("resolved_cosine"),
        "floor_cosine": latest.get("floor_cosine"),
        "population": {
            "context_centroids": pop.get("context_centroids"),
            "people_significant": pop.get("people_significant"),
            "people_dual_floor_eligible": pop.get("people_dual_floor_eligible"),
            "centroid_coverage_of_significant": pop.get(
                "centroid_coverage_of_significant"
            ),
            "centroid_coverage_of_eligible": pop.get("centroid_coverage_of_eligible"),
            "centroid_coverage_of_all_people": pop.get(
                "centroid_coverage_of_all_people"
            ),
        },
        "derived_spec_live": (snapshot.get("derived_spec") or {}).get("live"),
        "edge_set_jaccard_vs_prev": comparison.get("edge_set_jaccard"),
        "honest_claims": snapshot.get("honest_claims"),
        "code_sha": snapshot.get("code_sha"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path.home() / ".topos" / "database.db"))
    parser.add_argument(
        "--history",
        default=str(HISTORY_PATH),
        help="JSONL history path (append one line per run)",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not append to history (still prints the snapshot)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Optional path to write the full JSON snapshot",
    )
    parser.add_argument("--sample-pairs", type=int, default=20)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print the full snapshot instead of the compact summary",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    from topos.features.entities.affinity_quality import (
        build_affinity_quality_snapshot,
        compare_snapshots,
    )

    # Read-only at the connection level — never mutate the live node.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        snapshot = build_affinity_quality_snapshot(
            conn, sample_pairs=args.sample_pairs
        )
    finally:
        conn.close()

    snapshot["code_sha"] = _code_sha()
    snapshot["db_path"] = str(db_path)

    history_path = Path(args.history).expanduser()
    if not args.no_history:
        previous = _previous_run(history_path)
        snapshot["comparison"] = compare_snapshots(snapshot, previous)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a") as fh:
            fh.write(json.dumps(snapshot, default=str) + "\n")
        snapshot["history"] = str(history_path)
    else:
        snapshot["comparison"] = {"compared_to": None, "edge_set_jaccard": None}

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(snapshot, indent=2, default=str) + "\n")
        snapshot["out"] = str(out_path)

    payload = snapshot if args.full else _summary(snapshot)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
