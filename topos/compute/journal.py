"""Content-free ownership journal for one local provider authority.

Unresolved operations survive process death. Never reclaim them by elapsed time.
The lock coordinates processes using this same private state directory, not
other directories or hosts. There is deliberately no automatic reset API.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sqlite3
import threading
import uuid


async def disk(call, *args):
    """Finish an in-flight disk mutation before cancellation can close its DB."""
    task = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class OwnershipJournal:
    def __init__(self, directory: Path, resource: str):
        # This first deployment supports POSIX hosts only; fail before serving.
        import fcntl

        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError("Provider state must be a private directory")
        self._lock = os.open(directory / "authority.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._db = None
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            database = directory / "ownership.sqlite3"
            fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self._db = sqlite3.connect(database, timeout=1, check_same_thread=False)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS resource (id TEXT PRIMARY KEY)")
            self._db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, epoch TEXT NOT NULL, state TEXT NOT NULL)")
            prior = self._db.execute("SELECT id FROM resource").fetchall()
            if prior and prior != [(resource,)]:
                raise ValueError("Provider state belongs to a different resource")
            self._db.execute("INSERT OR IGNORE INTO resource VALUES (?)", (resource,))
            self._db.execute("UPDATE operations SET state='uncertain'")
            self._db.commit()
            self.unresolved = self._db.execute("SELECT count(*) FROM operations").fetchone()[0]
            self.epoch = str(uuid.uuid4())
            self._mutex = threading.Lock()
        except BaseException:
            if self._db is not None:
                self._db.close()
            os.close(self._lock)
            raise

    def begin(self, operation: str):
        with self._mutex, self._db:
            self._db.execute("INSERT INTO operations VALUES (?, ?, 'executing')", (operation, self.epoch))

    def finish(self, operation: str, terminal: bool):
        with self._mutex, self._db:
            if terminal:
                self._db.execute("DELETE FROM operations WHERE id=?", (operation,))
            else:
                self._db.execute("UPDATE operations SET state='uncertain' WHERE id=?", (operation,))

    def close(self):
        self._db.close()
        os.close(self._lock)
