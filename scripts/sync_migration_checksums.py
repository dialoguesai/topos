#!/usr/bin/env python3
"""Generate / verify migrations/registry_checksums.json (PLAN §4a.6).

Checksums cover shipped, non-always_run migration modules. CI fails if a
shipped migration's source hash changes (append-only invariant).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "topos" / "storage" / "db" / "migrations"
CHECKSUMS_PATH = MIGRATIONS_DIR / "registry_checksums.json"


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_checksums() -> dict:
    import inspect

    # Import after path setup so editable installs resolve.
    sys.path.insert(0, str(REPO_ROOT))
    from topos.storage.db.migrations.registry import MIGRATIONS

    out: dict[str, str] = {}
    for spec in MIGRATIONS:
        if spec.always_run:
            continue
        path = Path(inspect.getfile(spec.fn)).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"no module file for migration id {spec.id!r}")
        out[spec.id] = _hash_file(path)
    return dict(sorted(out.items()))


def write_checksums() -> Path:
    payload = {
        "$comment": (
            "SHA-256 of shipped, non-always_run migration modules. "
            "CI fails if a hash changes — migrations are append-only."
        ),
        "checksums": compute_checksums(),
    }
    CHECKSUMS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return CHECKSUMS_PATH


def check_checksums() -> None:
    if not CHECKSUMS_PATH.is_file():
        raise SystemExit(f"missing {CHECKSUMS_PATH}; run with --write")
    recorded = json.loads(CHECKSUMS_PATH.read_text(encoding="utf-8")).get("checksums") or {}
    current = compute_checksums()
    if recorded != current:
        missing = sorted(set(current) - set(recorded))
        extra = sorted(set(recorded) - set(current))
        changed = sorted(
            k for k in set(current) & set(recorded) if current[k] != recorded[k]
        )
        parts = []
        if changed:
            parts.append(f"changed={changed}")
        if missing:
            parts.append(f"missing_from_file={missing}")
        if extra:
            parts.append(f"extra_in_file={extra}")
        raise SystemExit(
            "migration registry checksums drifted "
            f"({'; '.join(parts)}). If you added a NEW migration, run "
            "`python scripts/sync_migration_checksums.py --write`. "
            "Never edit a shipped non-always_run migration in place."
        )
    print(f"checksums ok ({len(current)} ledger-guarded migrations)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate registry_checksums.json from the current registry",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if checksums drift (CI)",
    )
    args = parser.parse_args()
    if args.write:
        path = write_checksums()
        print(f"wrote {path}")
        return
    if args.check or not args.write:
        check_checksums()


if __name__ == "__main__":
    main()
