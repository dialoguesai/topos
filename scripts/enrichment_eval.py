#!/usr/bin/env python
"""Run the enrichment evaluation harness against the local node database.

Read-only. Measures coverage, signal density, and (optionally) retrieval
precision/latency with vs without enrichment-backed semantic narrowing.

Usage:
    python scripts/enrichment_eval.py [--db PATH] [--source SOURCE_ID ...]
        [--cases PATH.json] [--out PATH.json]

Cases file format (JSON list):
    [{"query": "fundraising plans", "expected_keywords": ["fundraise"],
      "expected_record_ids": ["msg-1"]}]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from topos.evals.enrichment_eval import (  # noqa: E402
    RetrievalEvalCase,
    format_report_summary,
    report_to_json,
    run_enrichment_eval,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path.home() / ".topos" / "database.db"))
    parser.add_argument("--source", action="append", dest="sources", default=None)
    parser.add_argument("--cases", default=None, help="JSON file of retrieval eval cases")
    parser.add_argument("--table", default="ai_chat_messages")
    parser.add_argument("--out", default=None, help="Write full JSON report to this path")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    cases = None
    if args.cases:
        raw = json.loads(Path(args.cases).read_text())
        cases = [
            RetrievalEvalCase(
                query=str(c.get("query") or ""),
                expected_record_ids=[str(x) for x in (c.get("expected_record_ids") or [])],
                expected_keywords=[str(x) for x in (c.get("expected_keywords") or [])],
            )
            for c in raw
            if str(c.get("query") or "").strip()
        ]

    report = run_enrichment_eval(
        conn,
        source_ids=args.sources,
        retrieval_cases=cases,
        table=args.table,
    )
    print(format_report_summary(report))
    if args.out:
        Path(args.out).write_text(report_to_json(report) + "\n")
        print(f"Full report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
