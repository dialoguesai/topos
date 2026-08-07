#!/usr/bin/env python3
"""Build a SQLite fixture DB that looks like a node stuck on an older baseline.

PLAN_NODE_RELEASE_MIGRATIONS M4 — upgrade-matrix fixture builder.

Two modes:

  --from-current (CI default)
      Uses the *current checkout* to apply the full migration chain, inserts a
      few synthetic coverage rows with NULL ``spec_version`` (pre-stamp era),
      and stamps ``engine.upgrade.baseline`` to ``--version``. The result is an
      "as if upgraded from X.Y.Z" database that current code can open and catch up.

Both modes seed real canonical conversation rows for a registry source plus the
matching ``timeline`` rows. That seed is load-bearing, not decoration: the
upgrade runner discovers work through ``_real_source_ids()`` (which reads
``timeline``), so a fixture without it makes every enrichment step a silent
no-op that still ledgers "done". See ``seed_canonical_source`` below.

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


# A fixture with no canonical rows is worse than no fixture: the upgrade
# runner's enrichment executor walks _real_source_ids(), which reads
# `timeline`. An empty timeline means every enrichment_reprocess step ledgers
# "done" with {"sources": {}} — steps_run counts up, the matrix goes green, and
# nothing was exercised (observed 2026-08-07). So seed a REAL registry source
# with canonical messages plus the timeline rows that advertise it.
#
# voxterm_transcripts is chosen deliberately: it is in topos.sources.registry
# (so _process_enrichment_core resolves it instead of raising ValueError ->
# "unknown_source_skipped") and its id does not start with any prefix that
# _real_source_ids skips (demo_/enrichment_lab/sanity/test/manual_enrichment).
_FIXTURE_SOURCE_ID = "voxterm_transcripts"
_FIXTURE_CONVERSATION_ID = "upgrade-fixture-c1"

# Content carries unambiguous PERSON/ORG/GPE surfaces so the NER pass has
# something to find; an extraction that returns nothing would be
# indistinguishable from an extraction that never ran.
_FIXTURE_MESSAGES = (
    (
        "upgrade-fixture-m1", "user", "self", 1,
        "Met Alice Johnson at the Berlin office to plan the Q3 migration.",
        "2026-05-01T10:00:00+00:00",
    ),
    (
        "upgrade-fixture-m2", "contact", "alice", 0,
        "Bob Carter from Acme Corp will join us in Munich next Tuesday.",
        "2026-05-01T10:05:00+00:00",
    ),
    (
        "upgrade-fixture-m3", "user", "self", 1,
        "I told Carol Nguyen that Topos ships the graph rebuild this week.",
        "2026-05-02T09:00:00+00:00",
    ),
)

# Minimal shapes matching topos.storage.canonical.conversations_tables. Written
# as plain SQL so both build modes (current checkout AND an old PyPI wheel) can
# seed identically; the migration chain then evolves them (actor_role_v1 adds
# and backfills actor_role over these rows, which is itself worth covering).
_CANONICAL_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    source_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, dataset_id)
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id TEXT NOT NULL PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    sender_type TEXT,
    sender_id TEXT,
    reply_to_message_id TEXT,
    message_type TEXT,
    event_type TEXT,
    content TEXT,
    event_at TEXT NOT NULL,
    source_id TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    is_from_self INTEGER DEFAULT 0,
    owner_user_id TEXT
);
"""


def seed_canonical_source(conn: sqlite3.Connection) -> None:
    """Create + populate canonical conversation rows for the fixture source.

    Runs BEFORE the migration chain so the fixture mirrors a real pre-1.2.0
    node: canonical data already on disk, schema evolved underneath it.
    """
    conn.executescript(_CANONICAL_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO conversations (conversation_id, dataset_id, source_id) "
        "VALUES (?, 'default', ?)",
        (_FIXTURE_CONVERSATION_ID, _FIXTURE_SOURCE_ID),
    )
    for message_id, sender_type, sender_id, is_from_self, content, event_at in _FIXTURE_MESSAGES:
        conn.execute(
            "INSERT OR REPLACE INTO conversation_messages "
            "(message_id, conversation_id, dataset_id, sender_type, sender_id, "
            " content, event_at, source_id, is_from_self, message_type) "
            "VALUES (?, ?, 'default', ?, ?, ?, ?, ?, ?, 'text')",
            (
                message_id, _FIXTURE_CONVERSATION_ID, sender_type, sender_id,
                content, event_at, _FIXTURE_SOURCE_ID, is_from_self,
            ),
        )
    conn.commit()


def seed_timeline(conn: sqlite3.Connection) -> None:
    """Advertise the fixture source in `timeline` (post-migration: it creates it).

    _real_source_ids() reads ONLY this table — without these rows the enrichment
    executors have no source list to walk.
    """
    for message_id, _st, _sid, _self, _content, event_at in _FIXTURE_MESSAGES:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO timeline "
                "(event_at, record_id, source_id, canonical_table, record_type) "
                "VALUES (?, ?, ?, 'conversation_messages', 'message')",
                (event_at, message_id, _FIXTURE_SOURCE_ID),
            )
        except sqlite3.Error as exc:
            print(f"timeline seed skipped ({exc}) for {message_id}", flush=True)
    conn.commit()


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
        # Canonical data first: a real pre-1.2.0 node has rows on disk before
        # the chain runs, and actor_role_v1's backfill then has something to
        # walk instead of an empty table.
        seed_canonical_source(conn)
        apply_all_migrations(conn)
        _seed_coverage(conn)
        seed_timeline(conn)  # `timeline` is created by the chain, so seed after
        _stamp_baseline(conn, version)
    finally:
        conn.close()

    print(
        f"from-current fixture written: {out} "
        f"(baseline={version}; schema=current; coverage rows spec_version=NULL; "
        f"{len(_FIXTURE_MESSAGES)} canonical rows for {_FIXTURE_SOURCE_ID})",
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

    # Seed with the CURRENT checkout's SQL rather than the old wheel's helpers
    # (their module paths differ across versions). actor_role and any other
    # missing columns are added by ensure_migrations_applied when the matrix
    # opens this fixture, so post-hoc seeding is equivalent here.
    conn = sqlite3.connect(str(out))
    try:
        seed_canonical_source(conn)
        seed_timeline(conn)
    finally:
        conn.close()

    print(
        f"pypi fixture written: {out} (topos-node=={version}; "
        f"{len(_FIXTURE_MESSAGES)} canonical rows for {_FIXTURE_SOURCE_ID})",
        flush=True,
    )


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
