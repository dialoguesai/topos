#!/usr/bin/env python3
"""Build a SQLite fixture DB that looks like a node stuck on an older baseline.

PLAN_NODE_RELEASE_MIGRATIONS M4 — upgrade-matrix fixture builder.

Two modes:

  --from-current (CI default)
      Uses the *current checkout* to apply the full migration chain, inserts a
      few synthetic coverage rows with NULL ``spec_version`` (pre-stamp era),
      and stamps ``engine.upgrade.baseline`` to ``--version``. The result is an
      "as if upgraded from X.Y.Z" database that current code can open and catch up.

  (default / PyPI mode)
      Creates a venv, ``pip install topos-node==VERSION``, and boots enough of
      that package to apply migrations. Prefer this for nightly when old wheels
      remain installable; fall back to ``--from-current`` when wheels are heavy
      or flaky.

Usage:
  python scripts/build_upgrade_fixture.py --from-current --version 1.3.2 \\
      --out /tmp/upgrade-fixture.db
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Seed rows deliberately omit spec_version (NULL) so stale-predicate paths stay
# exercisable after ensure_migrations_applied adds the column.
_SEED_SQL = """
INSERT OR REPLACE INTO message_entities (
    entity_id, record_id, source_id, entity_text, model, provider, payload_json
) VALUES (
    'fixture-e1', 'fixture-m1', 'fixture_src', 'Alice', 'fixture', 'test', '{}'
);
INSERT OR REPLACE INTO message_topics (
    topic_id, record_id, source_id, topic, model, provider, payload_json
) VALUES (
    'fixture-t1', 'fixture-m1', 'fixture_src', 'work', 'fixture', 'test', '{}'
);
INSERT OR REPLACE INTO message_emotions (
    emotion_id, record_id, source_id, model, provider, payload_json
) VALUES (
    'fixture-em1', 'fixture-m1', 'fixture_src', 'fixture', 'test', '{}'
);
INSERT OR REPLACE INTO entities (
    entity_id, entity_type, canonical_name, normalized_name, is_self
) VALUES (
    'fixture-ent1', 'person', 'Alice', 'alice', 0
);
"""


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=env)


def _stamp_baseline(conn: sqlite3.Connection, version: str) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO engine_config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("engine.upgrade.baseline", version),
    )
    conn.commit()


def _seed_coverage(conn: sqlite3.Connection) -> None:
    """Insert synthetic rows; leave spec_version unset/NULL when the column exists."""
    # Prefer INSERT without spec_version so NULL stamps survive if the column is present.
    for stmt in _SEED_SQL.strip().split(";"):
        sql = stmt.strip()
        if not sql:
            continue
        try:
            conn.execute(sql)
        except sqlite3.Error as exc:
            # Older schemas may lack a table; skip non-critical seeds.
            print(f"seed skipped ({exc}): {sql[:60]}...", flush=True)
    # Explicit NULL if column exists (idempotent).
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(message_entities)").fetchall()
        }
        if "spec_version" in cols:
            conn.execute(
                "UPDATE message_entities SET spec_version=NULL "
                "WHERE entity_id LIKE 'fixture-%'"
            )
    except sqlite3.Error:
        pass
    conn.commit()


def build_from_current(version: str, out: Path) -> None:
    """Apply current-checkout migrations, seed rows, stamp baseline to *version*."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from topos.storage.db.migrations import apply_all_migrations

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(str(out))
    try:
        apply_all_migrations(conn)
        _seed_coverage(conn)
        _stamp_baseline(conn, version)
    finally:
        conn.close()

    print(
        f"from-current fixture written: {out} "
        f"(baseline={version}; schema=current; coverage rows spec_version=NULL)",
        flush=True,
    )
    print(
        "Note: nightly can use PyPI mode (omit --from-current) when "
        f"topos-node=={version} wheels remain installable.",
        flush=True,
    )


_PYPI_BOOT_SCRIPT = textwrap.dedent(
    """\
    import os
    import sqlite3
    import sys

    db_path = sys.argv[1]
    version = sys.argv[2]
    os.environ["TOPOS_DATABASE_PATH"] = db_path
    os.environ.setdefault("TOPOS_SKIP_UPDATE_CHECK", "1")
    os.environ.setdefault("TOPOS_UPGRADE_RUNNER", "off")

    conn = sqlite3.connect(db_path)
    try:
        # Prefer package migration APIs when present.
        try:
            from topos.storage.db.migrations import apply_all_migrations
            apply_all_migrations(conn)
        except Exception:
            from topos.storage.db.migrations import ensure_migrations_applied
            ensure_migrations_applied(conn)
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS engine_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            '''
        )
        conn.execute(
            "INSERT INTO engine_config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("engine.upgrade.baseline", version),
        )
        # Best-effort coverage seed (schema may differ by version).
        try:
            conn.execute(
                "INSERT OR REPLACE INTO message_entities "
                "(entity_id, record_id, source_id, entity_text, model, provider, payload_json) "
                "VALUES ('fixture-e1', 'fixture-m1', 'fixture_src', 'Alice', 'fixture', 'test', '{}')"
            )
        except Exception as exc:
            print(f"pypi seed skipped: {exc}", flush=True)
        conn.commit()
    finally:
        conn.close()
    print("pypi_fixture_ok", flush=True)
    """
)


def build_from_pypi(version: str, out: Path) -> None:
    """Install topos-node==version in a venv and seed a DB via that package."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    with tempfile.TemporaryDirectory(prefix="topos-upgrade-fixture-") as tmp:
        tmp_path = Path(tmp)
        venv_dir = tmp_path / "venv"
        _run([sys.executable, "-m", "venv", str(venv_dir)])
        python = venv_dir / "bin" / "python"
        if not python.exists():
            python = venv_dir / "Scripts" / "python.exe"

        # Prefer uv pip when available (faster); fall back to python -m pip.
        uv = shutil.which("uv")
        if uv:
            _run([uv, "pip", "install", "--python", str(python), f"topos-node=={version}"])
        else:
            _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            _run([str(python), "-m", "pip", "install", f"topos-node=={version}"])

        db_tmp = tmp_path / "fixture.db"
        env = os.environ.copy()
        env["TOPOS_SKIP_UPDATE_CHECK"] = "1"
        env["TOPOS_UPGRADE_RUNNER"] = "off"
        _run(
            [str(python), "-c", _PYPI_BOOT_SCRIPT, str(db_tmp), version],
            env=env,
        )
        shutil.copy2(db_tmp, out)

    print(f"pypi fixture written: {out} (topos-node=={version})", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        metavar="X.Y.Z",
        help="Baseline version to stamp (support floor / prior release)",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output path for the fixture SQLite database",
    )
    parser.add_argument(
        "--from-current",
        action="store_true",
        help=(
            "CI-friendly mode: apply current checkout migrations + synthetic "
            "NULL spec_version rows, then stamp baseline to --version "
            "(recommended when installing old PyPI wheels is heavy/flaky)"
        ),
    )
    args = parser.parse_args(argv)

    version = str(args.version).strip().lstrip("v")
    if not version:
        raise SystemExit("--version is required")

    out = args.out.expanduser().resolve()
    if args.from_current:
        build_from_current(version, out)
    else:
        try:
            build_from_pypi(version, out)
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                f"PyPI fixture build failed for topos-node=={version} "
                f"(exit {exc.returncode}). Retry with --from-current for CI."
            ) from exc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
