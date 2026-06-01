#!/usr/bin/env python3
"""Migrate local SQLite dataset_id values to the current per-connector scheme.

Row storage (local engine):
  {owner_user_id}:default:{sha256(topos_key)[:16]}

Install scope JSON (product / UI):
  {owner_user_id}:topos:{topos_id}

Usage:
  cd topos
  python scripts/migrate_local_dataset_ids.py --dry-run \\
    --user-id 9670043c-401a-4323-b092-c4724ca166eb \\
    --topos-key de06c687703abe038e11ff26fc1430d7 \\
    --topos-id topos_6d4174976e854ecb8f6b7ab66f1efc74

  python scripts/migrate_local_dataset_ids.py --apply ... (same flags)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(str(ROOT)))


def _device_hash(topos_key: str) -> str:
    return hashlib.sha256(topos_key.strip().encode("utf-8")).hexdigest()[:16]


def _tables_with_dataset_id(conn: sqlite3.Connection) -> list[str]:
    out: list[str] = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
        if "dataset_id" in cols:
            out.append(name)
    return out


def _count_for(conn: sqlite3.Connection, table: str, dataset_id: str | None) -> int:
    if dataset_id is None:
        row = conn.execute(
            f'''SELECT COUNT(*) FROM "{table}" WHERE dataset_id IS NULL OR TRIM(dataset_id) = '' '''
        ).fetchone()
    else:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE dataset_id = ?', (dataset_id,)).fetchone()
    return int(row[0] if row else 0)


def _migrate_install_scopes(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    topos_id: str,
    row_dataset_id: str,
    topos_scope_dataset_id: str,
    dry_run: bool,
) -> list[str]:
    logs: list[str] = []
    rows = conn.execute(
        "SELECT install_id, source_id, scope_key FROM source_runtime_installs WHERE scope_key LIKE ?",
        (f"%{user_id}%",),
    ).fetchall()
    for install_id, source_id, scope_key in rows:
        try:
            scope = json.loads(scope_key)
        except json.JSONDecodeError:
            continue
        if not isinstance(scope, dict):
            continue
        scope_user = str(scope.get("user_id") or "").strip()
        if scope_user and scope_user != user_id:
            continue
        old_dataset = str(scope.get("dataset_id") or "").strip()
        old_topos = str(scope.get("topos_id") or scope.get("app_id") or "").strip()
        if old_topos in {"manual-app-1", "app-a", "topos-a"} or old_dataset.startswith("user-a:"):
            continue
        new_scope = dict(scope)
        changed = False
        if old_dataset in {"*", row_dataset_id, f"{user_id}:default"} or (
            ":default" in old_dataset and ":topos:" not in old_dataset
        ):
            new_scope["dataset_id"] = topos_scope_dataset_id
            changed = True
        if not old_topos or old_topos == "*":
            new_scope["topos_id"] = topos_id
            new_scope.pop("app_id", None)
            changed = True
        if not changed:
            continue
        new_key = json.dumps(new_scope, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        logs.append(f"install {source_id} ({install_id}): {scope_key} -> {new_key}")
        if not dry_run:
            conn.execute(
                "UPDATE source_runtime_installs SET scope_key = ? WHERE install_id = ?",
                (new_key, install_id),
            )
    return logs


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate local SQLite dataset_id values.")
    parser.add_argument("--db-path", default=str(Path.home() / ".topos" / "database.db"))
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--topos-key", required=True, help="Engine connector / topos_key for device hash")
    parser.add_argument("--topos-id", required=True, help="Active Topos id for install scope JSON")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backfill-null",
        action="store_true",
        default=True,
        help="Set NULL/empty dataset_id to the target row dataset (default: on)",
    )
    parser.add_argument(
        "--no-backfill-null",
        action="store_false",
        dest="backfill_null",
        help="Skip NULL/empty dataset_id backfill",
    )
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("Pass --dry-run or --apply")

    db_path = Path(args.db_path).expanduser()
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    user_id = args.user_id.strip()
    topos_key = args.topos_key.strip()
    topos_id = args.topos_id.strip()
    device_hash = _device_hash(topos_key)
    legacy_row_dataset = f"{user_id}:default"
    target_row_dataset = f"{user_id}:default:{device_hash}"
    topos_scope_dataset = f"{user_id}:topos:{topos_id}"

    print(f"Database: {db_path}")
    print(f"Legacy row dataset: {legacy_row_dataset}")
    print(f"Target row dataset:  {target_row_dataset}")
    print(f"Target install scope dataset_id: {topos_scope_dataset}")
    print(f"Mode: {'dry-run' if args.dry_run else 'apply'}")

    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_name(f"{db_path.name}.bak.{stamp}")
        shutil.copy2(db_path, backup)
        print(f"Backup: {backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = _tables_with_dataset_id(conn)
        total_legacy = 0
        total_null = 0
        for table in tables:
            legacy_n = _count_for(conn, table, legacy_row_dataset)
            null_n = _count_for(conn, table, None) if args.backfill_null else 0
            if legacy_n == 0 and null_n == 0:
                continue
            total_legacy += legacy_n
            total_null += null_n
            print(f"  {table}: legacy={legacy_n}, null_backfill={null_n}")
            if args.apply:
                if legacy_n:
                    conn.execute(
                        f'UPDATE "{table}" SET dataset_id = ? WHERE dataset_id = ?',
                        (target_row_dataset, legacy_row_dataset),
                    )
                if null_n:
                    conn.execute(
                        f'''UPDATE "{table}" SET dataset_id = ? WHERE dataset_id IS NULL OR TRIM(dataset_id) = '' ''',
                        (target_row_dataset,),
                    )
                if "owner_user_id" in {c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')}:
                    conn.execute(
                        f'UPDATE "{table}" SET owner_user_id = ? WHERE dataset_id = ? AND (owner_user_id IS NULL OR TRIM(owner_user_id) = \'\')',
                        (user_id, target_row_dataset),
                    )
                if "tenant_id" in {c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')}:
                    conn.execute(
                        f'UPDATE "{table}" SET tenant_id = ? WHERE dataset_id = ? AND (tenant_id IS NULL OR TRIM(tenant_id) = \'\')',
                        (device_hash, target_row_dataset),
                    )

        # Fix obvious bad user_identity rows for this owner.
        if _count_for(conn, "user_identity", "user:default") if "user_identity" in tables else 0:
            print("  user_identity: user:default -> owner user_id")
            if args.apply:
                conn.execute(
                    'UPDATE user_identity SET dataset_id = ? WHERE dataset_id = ?',
                    (target_row_dataset, "user:default"),
                )

        install_logs = _migrate_install_scopes(
            conn,
            user_id=user_id,
            topos_id=topos_id,
            row_dataset_id=target_row_dataset,
            topos_scope_dataset_id=topos_scope_dataset,
            dry_run=args.dry_run,
        )
        if install_logs:
            print(f"Install scope updates ({len(install_logs)}):")
            for line in install_logs[:20]:
                print(f"  {line}")
            if len(install_logs) > 20:
                print(f"  ... and {len(install_logs) - 20} more")

        if args.apply:
            conn.commit()
        print(
            f"Summary: {total_legacy} legacy row(s) "
            f"{'would be ' if args.dry_run else ''}migrated, "
            f"{total_null} null row(s) "
            f"{'would be ' if args.dry_run else ''}backfilled."
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
