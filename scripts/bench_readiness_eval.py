#!/usr/bin/env python3
"""Replay portable readiness contracts; optionally audit an immutable owner snapshot.

Outputs contain case IDs and aggregate counts only. No ingestion, inference, network,
or live database access is required. Snapshot audits do not run readiness extraction.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from topos.features.derivation.bench_readiness import assess_readiness


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests/features/derivation/fixtures/bench_readiness_cases.json")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--snapshot-sha256")
    parser.add_argument("--as-of", help="Explicit timezone-aware snapshot reference instant")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    catalog = json.loads(args.cases.read_text())
    results = []
    for case in catalog["cases"]:
        result = assess_readiness(
            **{key: catalog[key] for key in ("person_id", "purpose_id", "dataset_id")},
            observations=case["observations"], coverage=case["coverage"],
            as_of=datetime.fromisoformat(catalog["as_of"]))
        passed = result["status"] == case["expected_status"]
        if case.get("expected_reason"):
            passed = passed and case["expected_reason"] in result["reason_codes"]
        results.append({"id": case["id"], "passed": passed, "status": result["status"],
                        "reason_codes": result["reason_codes"]})
    report = {"schema": "bench-readiness-evaluation-1", "policy_version": "bench-readiness-1",
              "scope": "Normalized-observation contracts; not extraction or generated-answer quality",
              "catalog_sha256": digest(args.cases), "contract_reference_instant": catalog["as_of"],
              "cases": results, "passed": sum(r["passed"] for r in results), "total": len(results),
              "baseline_promoted": False}
    if args.snapshot:
        if not args.snapshot_sha256 or not args.as_of:
            parser.error("snapshot requires --snapshot-sha256 and --as-of")
        now = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if now.tzinfo is None:
            parser.error("--as-of must have a timezone")
        snapshot = args.snapshot.resolve()
        if digest(snapshot) != args.snapshot_sha256:
            parser.error("snapshot digest does not match")
        conn = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # Evaluation isolation only; these patches cannot alter normal node startup.
            with patch("topos.storage.db.migrations.ensure_migrations_applied", lambda c: None), \
                 patch("topos.core.state.get_db_connection", lambda: conn):
                from topos.features.derivation.social_bench import build_bench_slate
                datasets = [r[0] for r in conn.execute("SELECT DISTINCT dataset_id FROM messenger_dyad_stats ORDER BY dataset_id")]
                slates = [build_bench_slate(conn, dataset_id=ds, now=now) for ds in datasets]
                facts = {pack: conn.execute("SELECT COUNT(*) FROM signal_objects WHERE ontology_id=? AND valid_to IS NULL", (pack,)).fetchone()[0]
                         for pack in ("net.capability", "net.character")}
            report["snapshot"] = {"sha256": args.snapshot_sha256, "reference_instant": args.as_of,
                "active_outward_facts": facts,
                "datasets": [{"dataset_ordinal": index + 1, "purposes": len(slate["roles"]),
                    "candidates": sum(len(role["candidates"]) for role in slate["roles"]),
                    "ready_candidates": slate["coverage"]["ready_candidate_count"],
                    "readiness_adapter": slate["coverage"]["readiness_adapter"],
                    "qualification_reasons": dict(Counter(reason for role in slate["roles"]
                        for candidate in role["candidates"] for reason in candidate["bench_qualification"]["reason_codes"]))}
                    for index, slate in enumerate(slates)],
                "inference": "No active outward facts or readiness adapter is not proof that raw source evidence is absent."}
        finally:
            conn.close()
        if digest(snapshot) != args.snapshot_sha256:
            raise RuntimeError("Snapshot changed during audit")
    content = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(content)
    else:
        print(content, end="")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
