#!/usr/bin/env python3
"""Open an upgrade fixture with *current* checkout code and assert catch-up.

PLAN_NODE_RELEASE_MIGRATIONS M4 — upgrade matrix runner.

  1. ensure_migrations_applied
  2. run_pending_upgrades (auto steps; consent → pending_consent)
  3. Assert: no failed ledger rows; baseline == shipped OR only pending_consent
     remaining; schema has spec_version when current code ships that migration

Exit non-zero on failure.

Usage:
  python scripts/run_upgrade_matrix.py --db /tmp/upgrade-fixture.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _shipped_version() -> str:
    from topos.__version__ import __version__

    return __version__


def _has_spec_version_migration() -> bool:
    try:
        from topos.storage.db.migrations.enrichment_spec_version_v1 import (  # noqa: F401
            MIGRATION_ID,
        )

        return True
    except ImportError:
        return False


def _assert_spec_version_column(conn: sqlite3.Connection) -> None:
    if not _has_spec_version_migration():
        print("spec_version migration not in this build; skipping column assert")
        return
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(message_entities)").fetchall()
    }
    if "spec_version" not in cols:
        raise AssertionError(
            "message_entities missing spec_version after ensure_migrations_applied"
        )
    print("ok: message_entities.spec_version present")


def _failed_ledger(conn: sqlite3.Connection) -> list[tuple]:
    try:
        return list(
            conn.execute(
                "SELECT version, step_id, status, detail_json FROM derivation_ledger "
                "WHERE status='failed'"
            ).fetchall()
        )
    except sqlite3.Error:
        return []


def _pending_consent(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT step_id FROM derivation_ledger WHERE status='pending_consent'"
        ).fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return []


def run_matrix(db_path: Path) -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # Avoid accidental live-DB side effects / heavy runners during CI.
    os.environ.setdefault("TOPOS_SKIP_UPDATE_CHECK", "1")
    os.environ.setdefault("TOPOS_DATABASE_PATH", str(db_path))
    # Run upgrades inline (not background / UI-grace).
    os.environ["TOPOS_UPGRADE_RUNNER"] = "on"
    os.environ.setdefault("TOPOS_UPGRADE_UI_GRACE_SECONDS", "0")

    from topos.storage.db.migrations import ensure_migrations_applied
    from topos.upgrades.runner import (
        plan_upgrade,
        read_baseline,
        run_pending_upgrades,
    )

    if not db_path.is_file():
        raise SystemExit(f"fixture DB not found: {db_path}")

    shipped = _shipped_version()
    conn = sqlite3.connect(str(db_path))
    try:
        print(f"ensure_migrations_applied on {db_path} (shipped={shipped})")
        ensure_migrations_applied(conn, skip_backup=True)
        _assert_spec_version_column(conn)

        plan = plan_upgrade(conn, shipped=shipped)
        print(
            f"plan: baseline={plan.get('baseline')!r} → shipped={plan.get('shipped')!r} "
            f"steps={len(plan.get('steps') or [])} fresh={plan.get('fresh_install')}"
        )

        result = run_pending_upgrades(conn, shipped=shipped)
        print(f"run_pending_upgrades: {result}")

        failed = _failed_ledger(conn)
        if failed:
            raise AssertionError(f"failed derivation_ledger rows: {failed}")

        baseline = read_baseline(conn)
        consent = _pending_consent(conn)
        if baseline == shipped:
            print(f"ok: baseline advanced to shipped ({shipped})")
        elif consent and baseline != shipped:
            # Consent-gated steps block baseline advance — acceptable outcome.
            print(
                f"ok: baseline={baseline!r} with pending_consent={consent} "
                f"(shipped={shipped})"
            )
        else:
            raise AssertionError(
                f"baseline {baseline!r} != shipped {shipped!r} and no "
                f"pending_consent remaining"
            )

        # Non-consent pending/running leftover is a failure.
        try:
            stuck = list(
                conn.execute(
                    "SELECT version, step_id, status FROM derivation_ledger "
                    "WHERE status IN ('pending', 'running', 'failed')"
                ).fetchall()
            )
        except sqlite3.Error:
            stuck = []
        if stuck:
            raise AssertionError(f"unresolved ledger rows after upgrade: {stuck}")

        print("upgrade_matrix_ok")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="Path to fixture SQLite database (will be mutated in place)",
    )
    args = parser.parse_args(argv)
    try:
        run_matrix(args.db.expanduser().resolve())
    except AssertionError as exc:
        print(f"upgrade_matrix_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"upgrade_matrix_error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
