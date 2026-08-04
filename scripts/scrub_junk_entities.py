#!/usr/bin/env python3
"""One-shot C4 residual scrub: remove already-minted ≤3-char junk entities.

Dry-run by default. Pass --apply to mutate the live spine.

  uv run python scripts/scrub_junk_entities.py
  uv run python scripts/scrub_junk_entities.py --apply
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
        "--apply",
        action="store_true",
        help="Actually delete junk entities (default is dry-run)",
    )
    args = parser.parse_args()

    from topos.core.state import get_db_connection
    from topos.features.lifecycle.derived_scrub import purge_junk_minted_entities

    conn = get_db_connection()
    if conn is None:
        print(json.dumps({"error": "no database connection"}), file=sys.stderr)
        return 1

    report = purge_junk_minted_entities(conn, dry_run=not args.apply)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
