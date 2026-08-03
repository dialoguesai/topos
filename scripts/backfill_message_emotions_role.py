#!/usr/bin/env python3
"""Stamp role on legacy message_emotions rows (Wave B5).

Idempotent: only updates WHERE role IS NULL. Migration
``message_emotions_role_backfill_v1`` runs the same function once at migrate
time; this CLI is for re-runs / ops visibility.

  uv run python scripts/backfill_message_emotions_role.py
  uv run python scripts/backfill_message_emotions_role.py --dry-run
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
        help="Count NULL roles without updating",
    )
    args = parser.parse_args()

    from topos.core.state import get_db_connection
    from topos.storage.db.migrations.message_emotions_role_backfill_v1 import (
        backfill_message_emotions_role,
    )

    conn = get_db_connection()
    if conn is None:
        print(json.dumps({"error": "no database connection"}), file=sys.stderr)
        return 1

    if args.dry_run:
        try:
            nulls = conn.execute(
                "SELECT COUNT(*) FROM message_emotions WHERE role IS NULL"
            ).fetchone()
            total = conn.execute("SELECT COUNT(*) FROM message_emotions").fetchone()
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "null_roles": int(nulls[0]) if nulls else 0,
                    "total_rows": int(total[0]) if total else 0,
                },
                indent=2,
            )
        )
        return 0

    report = backfill_message_emotions_role(conn)
    conn.commit()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
