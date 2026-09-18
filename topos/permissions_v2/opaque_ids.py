"""Opaque per-grant record ids for recipient views, and the key store behind them.

A canonical message id such as `imessage:<ROWID>` is a counter over the owner's
whole store: two released ids reveal how many messages lie between them (design
§6.4, channel 11). A recipient view therefore carries

    "r." + hex(HMAC-SHA256(k_grant, DOMAIN + canonical_bytes({grant_id, table, source_id, dataset_id, record_id})))

k_grant is 32 random bytes per grant id, kept in a node-private SQLite file.
Ids are stable within a grant (across index rebuilds and assignment
generations) and unrelated across grants. Revoking a grant deletes its key, so
a later re-activation of the same grant id gets fresh ids. Losing the file
changes ids, never widens anything. The key never leaves the node.

The same key also derives the AES-GCM key that seals the witness fact ids in a
search index file, so a reader of that file alone cannot use them.

This module is shared by design: the p2a message view's opaque ids (bookkeeping
stream) must import it rather than fork it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
from pathlib import Path

from .canonical import PolicyError, canonical_bytes

DOMAIN = b"topos-p2c-record-id/v1\n"
SEAL_DOMAIN = b"topos-p2c-index-seal/v1\n"
KEY_FILE = "keys.db"


def private_directory(path: Path) -> Path:
    """Create or verify a 0700 directory that is not a symlink."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise PolicyError("private_directory_required")
    return path


def private_file(path: Path) -> Path:
    """Create or verify a 0600 regular file that is not a symlink."""
    path = Path(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise PolicyError("private_file_required")
    return path


def opaque_record_id(key: bytes, *, grant_id: str, table: str, source_id: str | None, dataset_id: str | None,
                     record_id: str) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise PolicyError("record_key_invalid")
    body = canonical_bytes({"grant_id": grant_id, "table": table, "source_id": source_id,
                            "dataset_id": dataset_id, "record_id": record_id})
    return "r." + hmac.new(key, DOMAIN + body, hashlib.sha256).hexdigest()


def seal_key(key: bytes) -> bytes:
    """A distinct AES-256 key for sealing index fields, derived from the id key."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise PolicyError("record_key_invalid")
    return hmac.new(key, SEAL_DOMAIN, hashlib.sha256).digest()


class RecordKeys:
    """One random key per grant id in `<root>/keys.db`, 0600 inside a 0700 directory."""

    def __init__(self, root: Path):
        self.root = private_directory(root)
        self.path = private_file(self.root / KEY_FILE)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS p2c_record_keys (grant_id TEXT PRIMARY KEY, key BLOB NOT NULL)")

    def _db(self):
        private_file(self.path)
        conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        conn.execute("PRAGMA journal_mode=DELETE")

        class _Tx:
            def __enter__(self_inner):
                conn.execute("BEGIN IMMEDIATE")
                return conn

            def __exit__(self_inner, kind, *_):
                try:
                    conn.execute("COMMIT" if kind is None else "ROLLBACK")
                finally:
                    conn.close()
        return _Tx()

    def get(self, grant_id: str, *, create: bool) -> bytes | None:
        with self._db() as db:
            row = db.execute("SELECT key FROM p2c_record_keys WHERE grant_id=?", (grant_id,)).fetchone()
            if row is not None:
                if not isinstance(row[0], bytes) or len(row[0]) != 32:
                    raise PolicyError("record_key_invalid")
                return row[0]
            if not create:
                return None
            key = secrets.token_bytes(32)
            db.execute("INSERT INTO p2c_record_keys VALUES (?, ?)", (grant_id, key))
            return key

    def delete(self, grant_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM p2c_record_keys WHERE grant_id=?", (grant_id,))
