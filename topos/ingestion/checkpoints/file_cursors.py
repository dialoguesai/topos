"""Per-file byte-offset cursors for the transcript readers (CONTRACTS §2.6, §2.7b).

The `ingestion_checkpoints` row for a source holds one ``last_record_id``;
Claude Code alone has about ten thousand session files, each append-only, so
"where did I stop in this file" is its own table keyed by the path's hash. The
path itself is never stored: it embeds the home folder and the project name.

Readers call ``get`` before reading a file and ``put`` after. A file deleted
since its cursor keeps the cursor (no rewind); a file re-created shorter than
its offset is read from the start again, and the canonical "first writer wins"
rule makes the second pass a no-op.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional, Union

from ...storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.ingestion.checkpoints.file_cursors")

TABLE = "ingestion_file_cursors"

# Kept byte-identical in storage/db/migrations/ingestion_file_cursors_v1.py:
# the migration module is checksummed and cannot import from here.
DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    source_id TEXT NOT NULL,
    path_hash TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    offset INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, path_hash)
)
"""


@dataclass(frozen=True)
class FileCursor:
    size: int
    mtime: float
    offset: int
    record_count: int


def path_hash(path: Union[str, "os.PathLike[str]"]) -> str:
    """sha256 of the path string as given; callers pass absolute paths."""
    return hashlib.sha256(os.fspath(path).encode("utf-8")).hexdigest()


def _table_present(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row)


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the table when the migration has not (bare test connections, L6 fakes)."""
    if _table_present(conn):
        return
    # DDL takes SQLite's write lock at execute time — gate it with the commit.
    with with_db_write():
        if _table_present(conn):
            return
        conn.execute(DDL)
        commit_connection(conn)


class FileCursorStore:
    """``get``/``put`` of one cursor per (source_id, path)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_table(conn)

    def get(self, source_id: str, path: Union[str, "os.PathLike[str]"]) -> Optional[FileCursor]:
        row = self.conn.execute(
            f"SELECT size, mtime, offset, record_count FROM {TABLE} WHERE source_id = ? AND path_hash = ?",
            (source_id, path_hash(path)),
        ).fetchone()
        if not row:
            return None
        size, mtime, offset, record_count = row
        return FileCursor(size=int(size or 0), mtime=float(mtime or 0.0), offset=int(offset or 0),
                          record_count=int(record_count or 0))

    def put(
        self,
        source_id: str,
        path: Union[str, "os.PathLike[str]"],
        *,
        size: int,
        mtime: float,
        offset: int,
        record_count: int,
    ) -> None:
        with with_db_write():
            self.conn.execute(
                f"""
                INSERT INTO {TABLE} (source_id, path_hash, size, mtime, offset, record_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source_id, path_hash) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    offset = excluded.offset,
                    record_count = excluded.record_count,
                    updated_at = excluded.updated_at
                """,
                (source_id, path_hash(path), int(size), float(mtime), int(offset), int(record_count)),
            )
            commit_connection(self.conn)
