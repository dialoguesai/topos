"""Point-in-time copy of the owner's database, so an eval can write freely.

The query quality/latency evals are only meaningful against real data — a
seeded fixture cannot tell you whether retrieval finds YOUR notes. But running
them against ``~/.topos/database.db`` directly makes them writers: each turn
persists a ``query_artifacts`` row, and on 2026-08-19 the whole table was 97%
harness sessions with every timed row synthetic, which meant
``scripts/query_latency_percentiles.py`` was reporting the test suite's latency
as the owner's.

A snapshot keeps the data and drops the write-back: the eval reads the same
records and its artifacts land in a throwaway file that gets deleted.

    snap=$(python scripts/snapshot_owner_db.py)
    TOPOS_DATABASE_PATH="$snap" pytest tests -m qq_eval -q
    rm -f "$snap" "$snap"-wal "$snap"-shm

Uses SQLite's ONLINE BACKUP API rather than ``cp``. The node is normally running
and the database is in WAL mode, so a byte copy of the main file can miss
committed pages that still live in the -wal — a torn snapshot that reads as
missing data, which in an eval reads as a retrieval regression. ``backup()``
takes a consistent image of a live database by design.

Prints the snapshot path on stdout and nothing else, so it can be captured
directly by a shell.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def default_source() -> Path:
    # Not `Path(os.environ.get(..., "")) or default`: Path("") is Path("."),
    # which is truthy, so the fallback would never fire.
    override = os.environ.get("TOPOS_SOURCE_DATABASE_PATH")
    return Path(override) if override else Path.home() / ".topos" / "database.db"


def snapshot(source: Path, dest: Path | None = None) -> Path:
    if not source.is_file():
        raise SystemExit(f"no database at {source}")
    if dest is None:
        handle, name = tempfile.mkstemp(prefix="topos-owner-snapshot-", suffix=".db")
        os.close(handle)
        dest = Path(name)
        dest.unlink()  # backup() wants to create it

    # mode=ro: this script must never be the reason the owner's database changes,
    # not even by SQLite recovering a hot journal on open.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--dest", type=Path, default=None)
    args = parser.parse_args(argv)

    dest = snapshot(args.source or default_source(), args.dest)
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
