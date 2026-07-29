#!/usr/bin/env python3
"""Generate / verify migrations/registry_checksums.json (PLAN §4a.6).

Checksums cover shipped, non-always_run migration modules. CI fails if a
shipped migration's source hash changes (append-only invariant).

This script is intentionally dependency-free (stdlib only) so publish.yml
can verify checksums before ``uv sync`` / package install.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "topos" / "storage" / "db" / "migrations"
REGISTRY_PATH = MIGRATIONS_DIR / "registry.py"
CHECKSUMS_PATH = MIGRATIONS_DIR / "registry_checksums.json"

_MIGRATION_ID_RE = re.compile(
    r"""^MIGRATION_ID\s*=\s*["']([^"']+)["']""",
    flags=re.M,
)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_id_for_module(module_path: Path) -> str:
    text = module_path.read_text(encoding="utf-8")
    match = _MIGRATION_ID_RE.search(text)
    if not match:
        raise SystemExit(f"{module_path.name}: missing MIGRATION_ID = \"...\" assignment")
    return match.group(1)


def _id_alias_to_module() -> dict[str, Path]:
    """Map registry aliases (e.g. PHASE0_ID) → migration module paths."""
    tree = ast.parse(REGISTRY_PATH.read_text(encoding="utf-8"), filename=str(REGISTRY_PATH))
    out: dict[str, Path] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module:
            continue
        # Relative imports from this package: ``from .wiki_mvp_phase0 import ...``
        module_path = MIGRATIONS_DIR / f"{node.module}.py"
        for alias in node.names:
            if alias.name != "MIGRATION_ID":
                continue
            asname = alias.asname or alias.name
            out[asname] = module_path
    return out


def _ledger_guarded_modules() -> list[Path]:
    """Ordered module paths for non-always_run entries in ``MIGRATIONS``."""
    tree = ast.parse(REGISTRY_PATH.read_text(encoding="utf-8"), filename=str(REGISTRY_PATH))
    migrations_value = None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "MIGRATIONS" for t in node.targets)
        ):
            migrations_value = node.value
            break
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "MIGRATIONS"
        ):
            migrations_value = node.value
            break
    if migrations_value is None or not isinstance(migrations_value, ast.List):
        raise SystemExit(f"{REGISTRY_PATH}: could not find MIGRATIONS = [...]")

    alias_map = _id_alias_to_module()
    modules: list[Path] = []
    for elt in migrations_value.elts:
        if not isinstance(elt, ast.Call):
            continue
        fn = elt.func
        if not isinstance(fn, ast.Name) or fn.id != "_spec":
            continue
        always_run = False
        for kw in elt.keywords:
            if kw.arg == "always_run" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                always_run = True
        if always_run:
            continue
        if len(elt.args) < 2 or not isinstance(elt.args[1], ast.Name):
            raise SystemExit(f"{REGISTRY_PATH}: unexpected _spec form: {ast.dump(elt)}")
        id_alias = elt.args[1].id
        module_path = alias_map.get(id_alias)
        if module_path is None:
            raise SystemExit(f"{REGISTRY_PATH}: no import for migration id alias {id_alias!r}")
        if not module_path.is_file():
            raise SystemExit(f"missing migration module {module_path}")
        modules.append(module_path)
    return modules


def compute_checksums() -> dict:
    out: dict[str, str] = {}
    for module_path in _ledger_guarded_modules():
        migration_id = _migration_id_for_module(module_path)
        if migration_id in out:
            raise SystemExit(f"duplicate migration id {migration_id!r}")
        out[migration_id] = _hash_file(module_path)
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
