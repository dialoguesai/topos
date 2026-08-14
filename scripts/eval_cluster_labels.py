"""Relabel-only A/B for topic-cluster labels: isolated prompt vs contrastive.

Clustering is NOT re-run. Two k-means passes over the same corpus agree only
to ARI ~0.52, so a before/after that re-clusters is measuring the seed, not
the prompt. This loads the clusters that already exist, hands the SAME
clusters to both prompt styles and the same local model, and reports:

  * distinct labels (the duplication the isolated prompt produces)
  * max duplication count for one label
  * labels duplicated across more than one signal dimension
  * ten before/after pairs

Never touches the live database: the source is opened read-only and copied
into the system temp dir with the sqlite backup API; every write lands on the
copy. Nothing here restarts or signals a running node.

    python scripts/eval_cluster_labels.py                  # all clusters, both arms
    python scripts/eval_cluster_labels.py --limit 40       # bounded
    python scripts/eval_cluster_labels.py --arms contrastive
    python scripts/eval_cluster_labels.py --dry-run        # print prompts, no model
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

ARMS = ("isolated", "contrastive")


def log(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", flush=True)


def copy_database(source: Path, dest: Path) -> Dict[str, Any]:
    """sqlite backup API from a read-only handle — the live file is never opened rw."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    dst = sqlite3.connect(str(dest))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return {"copied_to": str(dest), "size_mb": round(dest.stat().st_size / 1e6, 1)}


def label_metrics(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """rows: [{"label": ..., "dimension": ...}] — one per cluster."""
    counts = Counter(str(r["label"]).strip().lower() for r in rows)
    dims: Dict[str, set] = defaultdict(set)
    for row in rows:
        dims[str(row["label"]).strip().lower()].add(str(row["dimension"] or ""))
    duplicated = {label: n for label, n in counts.items() if n > 1}
    cross_dimension = {
        label: sorted(dims[label]) for label in duplicated if len(dims[label]) > 1
    }
    return {
        "clusters": len(rows),
        "distinct_labels": len(counts),
        "max_duplication": max(counts.values()) if counts else 0,
        "duplicated_labels": len(duplicated),
        "clusters_carrying_a_duplicate_label": sum(duplicated.values()),
        "labels_spanning_multiple_dimensions": len(cross_dimension),
        "top_duplicates": [
            {"label": label, "count": n, "dimensions": sorted(dims[label])}
            for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            if n > 1
        ],
        "cross_dimension_duplicates": [
            {"label": label, "dimensions": d} for label, d in sorted(cross_dimension.items())
        ][:10],
    }


def build_runner(model: Optional[str]) -> Callable[[str], str]:
    """Same engine path the node uses (ollama via the local engine client)."""
    from topos.features.signal import cluster_labels

    if model:
        import os

        os.environ["TOPOS_CLUSTER_LABEL_MODEL"] = model
    calls = {"n": 0, "seconds": 0.0}

    def _complete(prompt: str) -> str:
        t0 = time.time()
        out = cluster_labels._complete_via_engine(prompt)
        calls["n"] += 1
        calls["seconds"] += time.time() - t0
        return out

    _complete.stats = calls  # type: ignore[attr-defined]
    return _complete


def reset_to_term_labels(clusters: List[Dict[str, Any]]) -> int:
    """Put each cluster back to the deterministic label a recompute starts from.

    Clusters on disk already carry an LLM label, and the isolated prompt feeds
    the cluster's current label back in as a "frequent term" — scoring it
    against its own previous answer would be a rigged control. The term label
    the labeler preserved in metadata is the honest starting point for both
    arms.
    """
    reset = 0
    for cluster in clusters:
        metadata = dict(cluster.get("metadata") or {})
        term_label = str(metadata.pop("term_label", "") or "")
        metadata.pop("label_model", None)
        metadata.pop("label_style", None)
        if term_label:
            cluster["label"] = term_label
            reset += 1
        cluster["metadata"] = metadata
    return reset


def run_arm(
    arm: str,
    clusters: List[Dict[str, Any]],
    runner: Callable[[str], str],
    *,
    timeout_sec: float,
) -> Dict[str, Any]:
    """Label a deep copy of the clusters with one prompt style."""
    import copy

    from topos.features.signal.cluster_labels import apply_llm_cluster_labels

    working = copy.deepcopy(clusters)
    t0 = time.time()
    relabeled = apply_llm_cluster_labels(
        working,
        complete=runner,
        mode="on",
        timeout_sec=timeout_sec,
        contrastive=(arm == "contrastive"),
    )
    rows = [
        {
            "cluster_id": str(c.get("cluster_id")),
            "label": str(c.get("label") or ""),
            "dimension": str(c.get("primary_dimension") or c.get("dimension") or ""),
            "member_count": int(c.get("member_count") or 0),
        }
        for c in working
    ]
    return {
        "arm": arm,
        "relabeled": relabeled,
        "seconds": round(time.time() - t0, 1),
        "rows": rows,
        "metrics": label_metrics(rows),
    }


def print_metrics(title: str, metrics: Dict[str, Any]) -> None:
    print(f"\n{title}")
    print(f"  clusters                          {metrics['clusters']}")
    print(f"  distinct labels                   {metrics['distinct_labels']}")
    print(f"  max duplication of one label      {metrics['max_duplication']}")
    print(f"  labels used more than once        {metrics['duplicated_labels']}")
    print(f"  clusters carrying a dup label     {metrics['clusters_carrying_a_duplicate_label']}")
    print(f"  dup labels spanning >1 dimension  {metrics['labels_spanning_multiple_dimensions']}")
    for entry in metrics["top_duplicates"]:
        print(f"    x{entry['count']:<3} {entry['label']!r} across {entry['dimensions']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-db", default=str(Path.home() / ".topos" / "database.db"))
    parser.add_argument(
        "--workdir",
        default="",
        help="defaults to a fresh system temp dir (never inside the repo or ~/.topos)",
    )
    parser.add_argument("--limit", type=int, default=0, help="clusters by size; 0 = all")
    parser.add_argument("--dimensions", default="", help="comma-separated dimension filter")
    parser.add_argument("--model", default="", help="override TOPOS_CLUSTER_LABEL_MODEL")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--samples", type=int, default=10, help="before/after pairs to print")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print one prompt per arm and exit — no model calls",
    )
    parser.add_argument("--report", default="", help="write the JSON report here")
    args = parser.parse_args()

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="topos-labeleval-"))
    copy_path = workdir / "labels_eval.db"
    log("copy", f"{args.source_db} -> {copy_path} (read-only source)")
    log("copy", str(copy_database(Path(args.source_db), copy_path)))

    from topos.features.signal.cluster_labels import (
        build_contrastive_label_prompt,
        build_label_prompt,
        compute_distinguishing_terms,
        labeling_order,
        sibling_labels,
    )
    from topos.features.signal.topic_clustering import load_clusters_with_members

    conn = sqlite3.connect(str(copy_path))
    try:
        dimensions = [d.strip() for d in args.dimensions.split(",") if d.strip()]
        clusters = load_clusters_with_members(
            conn, limit=args.limit or None, dimensions=dimensions or None
        )
    finally:
        conn.close()
    if not clusters:
        log("load", "no clusters on this database")
        return 1
    log(
        "load",
        f"{len(clusters)} clusters, "
        f"{sum(len(c['members']) for c in clusters)} members, "
        f"dimensions={sorted({str(c.get('dimension')) for c in clusters})}",
    )

    baseline_rows = [
        {
            "cluster_id": str(c.get("cluster_id")),
            "label": str(c.get("label") or ""),
            "dimension": str(c.get("primary_dimension") or c.get("dimension") or ""),
            "member_count": int(c.get("member_count") or 0),
        }
        for c in clusters
    ]
    on_disk = label_metrics(baseline_rows)
    print_metrics("ON DISK (labels the running node produced)", on_disk)

    reset = reset_to_term_labels(clusters)
    log("reset", f"{reset}/{len(clusters)} clusters restored to their term label for both arms")

    if args.dry_run:
        distinguishing = compute_distinguishing_terms(clusters)
        first = labeling_order(clusters)[0]
        print("\n===== isolated prompt =====\n")
        print(build_label_prompt(clusters[first]))
        print("\n===== contrastive prompt =====\n")
        print(
            build_contrastive_label_prompt(
                clusters[first],
                distinguishing_terms=distinguishing[first],
                siblings=sibling_labels(first, clusters, distinguishing, {}),
            )
        )
        return 0

    runner = build_runner(args.model or None)
    arms = [a.strip() for a in args.arms.split(",") if a.strip() in ARMS]
    results: Dict[str, Any] = {}
    for arm in arms:
        log(arm, f"labeling {len(clusters)} clusters…")
        results[arm] = run_arm(arm, clusters, runner, timeout_sec=args.timeout)
        log(
            arm,
            f"relabeled {results[arm]['relabeled']}/{len(clusters)} "
            f"in {results[arm]['seconds']}s",
        )
        print_metrics(f"ARM: {arm}", results[arm]["metrics"])

    if len(arms) == 2:
        by_id = {r["cluster_id"]: r for r in results["contrastive"]["rows"]}
        term_labels = {str(c["cluster_id"]): str(c.get("label") or "") for c in clusters}
        print(f"\nSAMPLE before/after ({args.samples} largest clusters)")
        shown = 0
        for row in sorted(results["isolated"]["rows"], key=lambda r: -r["member_count"]):
            after = by_id.get(row["cluster_id"])
            if not after:
                continue
            print(f"  [{row['dimension']}] n={row['member_count']}")
            print(f"      term        {term_labels.get(row['cluster_id'], '')!r}")
            print(f"      isolated    {row['label']!r}")
            print(f"      contrastive {after['label']!r}")
            shown += 1
            if shown >= args.samples:
                break

    report = {
        "source_db": args.source_db,
        "copy": str(copy_path),
        "model": args.model or None,
        "clusters": len(clusters),
        "clusters_reset_to_term_label": reset,
        "on_disk": on_disk,
        "arms": {
            arm: {
                "relabeled": results[arm]["relabeled"],
                "seconds": results[arm]["seconds"],
                "metrics": results[arm]["metrics"],
                "labels": results[arm]["rows"],
            }
            for arm in results
        },
        "model_calls": getattr(runner, "stats", {}),
    }
    report_path = Path(args.report) if args.report else workdir / "label_eval_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log("report", str(report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
