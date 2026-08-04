#!/usr/bin/env python3
"""Stamp authored_by_owner on legacy entity_mentions rows (Wave B6 / P3.1).

Idempotent: only updates WHERE authored_by_owner IS NULL. Migration
``entity_mentions_authored_v1`` runs the same function once at migrate
time; this CLI is for re-runs / ops visibility.

  uv run python scripts/backfill_entity_mentions_authored.py
  uv run python scripts/backfill_entity_mentions_authored.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count NULL authored_by_owner without updating",
    )
    args = parser.parse_args()

    from topos.core.state import get_db_connection
    from topos.storage.db.migrations.entity_mentions_authored_v1 import (
        backfill_entity_mentions_authored,
    )

    conn = get_db_connection()
    if conn is None:
        print(json.dumps({"error": "no database connection"}), file=sys.stderr)
        return 1

    if args.dry_run:
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(entity_mentions)").fetchall()
            }
            if "authored_by_owner" not in cols:
                print(
                    json.dumps(
                        {
                            "dry_run": True,
                            "column_present": False,
                            "null_authored_by_owner": None,
                            "total_rows": None,
                        },
                        indent=2,
                    )
                )
                return 0
            nulls = conn.execute(
                "SELECT COUNT(*) FROM entity_mentions WHERE authored_by_owner IS NULL"
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM entity_mentions").fetchone()
            authored = conn.execute(
                "SELECT COUNT(*) FROM entity_mentions WHERE authored_by_owner = 1"
            ).fetchone()
            not_authored = conn.execute(
                "SELECT COUNT(*) FROM entity_mentions WHERE authored_by_owner = 0"
            ).fetchone()
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "column_present": True,
                    "null_authored_by_owner": int(nulls[0]) if nulls else 0,
                    "authored": int(authored[0]) if authored else 0,
                    "not_authored": int(not_authored[0]) if not_authored else 0,
                    "total_rows": int(total[0]) if total else 0,
                },
                indent=2,
            )
        )
        return 0

    report = backfill_entity_mentions_authored(conn)
    conn.commit()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
