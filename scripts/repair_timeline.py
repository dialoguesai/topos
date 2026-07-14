#!/usr/bin/env python3
"""Audit or repair missing timeline rows from canonical SQLite tables."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, time, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from topos.features.timeline_projection import project_canonical_timeline


def _date_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if len(value) == 10:
        parsed = datetime.combine(
            parsed.date(),
            time.max if end_of_day else time.min,
            tzinfo=timezone.utc,
        )
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db",
        default=str(Path.home() / ".topos" / "database.db"),
        help="SQLite node database",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--source-id", help="Repair one canonical source")
    scope.add_argument("--all-sources", action="store_true", help="Repair every canonical source")
    parser.add_argument("--date-from", help="Inclusive ISO date or timestamp")
    parser.add_argument("--date-to", help="Inclusive ISO date or timestamp")
    parser.add_argument(
        "--missing-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Insert absent rows without rewriting existing timeline metadata",
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes; default is dry-run")
    parser.add_argument("--backup", help="Copy the database to this path before applying")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--report", help="Write the JSON report to this path")
    return parser


def main() -> int:
    args = _parser().parse_args()
    db_path = Path(args.source_db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")
    if args.apply and not args.missing_only:
        raise SystemExit("Live repair requires --missing-only; broad rewrites are intentionally blocked")
    if args.apply and not args.backup:
        raise SystemExit("--backup is required when --apply is used")

    uri = f"file:{db_path}?mode={'rw' if args.apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        if args.apply:
            backup_path = Path(args.backup).expanduser().resolve()
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_conn = sqlite3.connect(str(backup_path))
            try:
                conn.backup(backup_conn)
            finally:
                backup_conn.close()
            conn.execute("BEGIN IMMEDIATE")
        report = project_canonical_timeline(
            conn,
            source_id=args.source_id,
            date_from=_date_bound(args.date_from, end_of_day=False),
            date_to=_date_bound(args.date_to, end_of_day=True),
            missing_only=args.missing_only,
            dry_run=not args.apply,
            commit=False,
            batch_size=args.batch_size,
        )
        if args.apply:
            conn.commit()
            verification = project_canonical_timeline(
                conn,
                source_id=args.source_id,
                date_from=_date_bound(args.date_from, end_of_day=False),
                date_to=_date_bound(args.date_to, end_of_day=True),
                missing_only=True,
                dry_run=True,
                commit=False,
                batch_size=args.batch_size,
            )
            report["remaining_missing"] = verification["totals"]["written"]
        report.update(
            {
                "database": str(db_path),
                "source_id": args.source_id,
                "date_from": args.date_from,
                "date_to": args.date_to,
                "mode": "apply" if args.apply else "dry-run",
            }
        )
    except Exception:
        if args.apply:
            conn.rollback()
        raise
    finally:
        conn.close()

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + "\n")
    return 1 if args.apply and report.get("remaining_missing", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
