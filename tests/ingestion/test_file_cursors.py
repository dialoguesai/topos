"""Per-file byte-offset cursors for the transcript readers (CONTRACTS §2.6, §2.7b).

protects: the ``ingestion_file_cursors`` table shape and its ledger step; ``get``/``put``
keyed by (source_id, sha256(path)); a store on a bare connection creates its own table
(the readers' tests and L6's fakes never run the migration runner).
"""

from __future__ import annotations

import hashlib
import sqlite3

from topos.ingestion.checkpoints.file_cursors import TABLE, FileCursor, FileCursorStore
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.registry import MIGRATIONS, max_migration_order

EXPECTED_COLUMNS = [
    ("source_id", "TEXT"),
    ("path_hash", "TEXT"),
    ("size", "INTEGER"),
    ("mtime", "REAL"),
    ("offset", "INTEGER"),
    ("record_count", "INTEGER"),
    ("updated_at", "TEXT"),
]


def _columns(conn: sqlite3.Connection) -> list:
    return [(row[1], row[2], row[5]) for row in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()]


def test_file_cursors_is_a_registered_ledger_step_at_the_head() -> None:
    spec = next(s for s in MIGRATIONS if s.id == "ingestion_file_cursors_v1")
    assert spec.order == 84 and not spec.always_run
    assert max_migration_order() >= spec.order


def test_migration_creates_the_contract_table(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    apply_all_migrations(conn)
    columns = _columns(conn)
    assert [(name, decl) for name, decl, _pk in columns] == EXPECTED_COLUMNS
    assert [name for name, _decl, pk in columns if pk] == ["source_id", "path_hash"]
    assert conn.execute(
        "SELECT count(*) FROM wiki_schema_migrations WHERE migration_id='ingestion_file_cursors_v1'"
    ).fetchone()[0] == 1
    conn.close()


def test_store_round_trips_by_source_and_hashed_absolute_path(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    store = FileCursorStore(conn)
    path = str(tmp_path / "proj" / "session.jsonl")
    assert store.get("claude_code_sessions", path) is None
    store.put("claude_code_sessions", path, size=1024, mtime=1_760_000_000.5, offset=900, record_count=7)
    cursor = store.get("claude_code_sessions", path)
    assert cursor == FileCursor(size=1024, mtime=1_760_000_000.5, offset=900, record_count=7)
    # Another source, same path: its own row.
    assert store.get("codex_sessions", path) is None
    store.put("claude_code_sessions", path, size=2048, mtime=1_760_000_100.0, offset=2048, record_count=9)
    assert store.get("claude_code_sessions", path) == FileCursor(2048, 1_760_000_100.0, 2048, 9)
    row = conn.execute(f"SELECT source_id, path_hash, updated_at FROM {TABLE}").fetchall()
    assert row == [("claude_code_sessions", hashlib.sha256(path.encode("utf-8")).hexdigest(), row[0][2])]
    assert row[0][2]
    assert path not in str(conn.execute(f"SELECT * FROM {TABLE}").fetchall())
    conn.close()


def test_store_on_a_migrated_database_uses_the_migrated_table(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    apply_all_migrations(conn)
    store = FileCursorStore(conn)
    store.put("cursor_sessions", "/x/y.jsonl", size=1, mtime=2.0, offset=1, record_count=0)
    assert store.get("cursor_sessions", "/x/y.jsonl") == FileCursor(1, 2.0, 1, 0)
    assert _columns(conn) and [(n, d) for n, d, _ in _columns(conn)] == EXPECTED_COLUMNS
    conn.close()
