"""What the node keeps so a permitted read can be re-scored later (confidence program C6).

The control plane samples permitted reads and files a tombstone: a grant, a door, a request id and the hash of what
was released (`control_plane/permissions_v2/SHADOW_AUDIT.md`). That row carries no content by design, so the
re-check cannot happen there. It happens here, where the content already is -- and it could not, because the node
could not find what it had released either.

`p2a_receipts` carries `output_hash`, `decision_hash` and `members_digest`, all hashes. `p2a_requests` carries the
envelope, whose intent is itself only a hash. There was no path from a request id to the records that were
released, so the job the spec described had nothing to score. This is that path.

**What a row holds.** One row per released record: the request id, the ordinal, the opaque record id the node
already minted for that release, the canonical table and the source id. Plus a **sealed pointer** -- the real
record id encrypted under that grant's own record key, AES-GCM with the opaque id as associated data, exactly the
way `search_index` seals its members. No content, no locator, no query text, no recipient subject, and no raw row
id that anything but this node's own key can read.

Why sealed rather than plain: under p2a-v3 the released id is opaque by construction, because two released ordinal
ids told a recipient how many of the owner's messages lay between them. Writing the ordinal back in the clear, in a
table whose whole purpose is to be read later, would put it back. Sealing keeps the pointer usable by the node and
useless to everything else, and the key is the one the grant already has: when the grant dies, the key is forgotten
and these rows resolve to nothing, which is the correct behaviour for a dead grant.

**It can never change what a recipient receives.** The write is called from `_checkpoint` inside its own try, after
the decision is bound and the receipt written. A failure is swallowed.

**A failure is counted, not swallowed silently.** A read whose index write failed is an unauditable read: the
shadow audit will ask about it and the node will not be able to answer. A hole nobody can see is worse than one
that can, so `failures()` is an owner-visible count, reported beside the refusal counters and on the re-score
handler's own answer, and the sample comes back `records_unavailable` rather than quietly succeeding.

**Bounded.** `RETENTION_DAYS` and `ROW_CAP`, pruned oldest-first in bounded batches on the node's own clock, like
every other ledger table. The retention is the audit's, not the read's: a sample can be re-scored for as long as
the control plane keeps its row.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time

logger = logging.getLogger(__name__)

VERSION = "topos-shadow-index/v1"
# The control plane keeps its samples for 400 days; this is what makes them answerable, so it outlives them by a
# margin rather than expiring first and turning old samples into holes.
RETENTION_DAYS = 420
ROW_CAP = 200_000
PRUNE_BATCH = 64
DOMAIN = b"topos-shadow-pointer/v1"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS p2a_shadow_released ("
    " request_id TEXT NOT NULL, ordinal INTEGER NOT NULL, grant_id TEXT NOT NULL,"
    " opaque_record_id TEXT NOT NULL, canonical_table TEXT NOT NULL, source_id TEXT NOT NULL,"
    " sealed_pointer BLOB NOT NULL, released_at INTEGER NOT NULL,"
    " PRIMARY KEY (request_id, ordinal))",
    "CREATE INDEX IF NOT EXISTS p2a_shadow_released_at ON p2a_shadow_released(released_at)",
)

_failures = 0
_lock = threading.Lock()


def failures() -> int:
    """How many releases this process could not make auditable. Owner-visible; never zero by assumption."""
    with _lock:
        return _failures


def reset_failures() -> None:
    global _failures
    with _lock:
        _failures = 0


def _count_failure() -> None:
    global _failures
    with _lock:
        _failures += 1


def enabled() -> bool:
    """Off unless the node is told otherwise, like every other part of this release."""
    return os.environ.get("TOPOS_PERMISSIONS_V2_SHADOW_INDEX_ENABLED", "").lower() == "true"


def seal_pointer(key: bytes, *, opaque_id: str, record_id: str, canonical_table: str) -> bytes:
    """The real record id, readable only with this grant's own key. `search_index.seal`'s construction."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from .canonical import canonical_bytes
    from .opaque_ids import seal_key
    nonce = secrets.token_bytes(12)
    body = canonical_bytes({"record_id": record_id, "canonical_table": canonical_table})
    return nonce + AESGCM(seal_key(key)).encrypt(nonce, body, opaque_id.encode("ascii"))


def open_pointer(key: bytes, *, opaque_id: str, sealed: bytes) -> dict | None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from .canonical import parse_json
    from .opaque_ids import seal_key
    try:
        raw = AESGCM(seal_key(key)).decrypt(bytes(sealed[:12]), bytes(sealed[12:]), opaque_id.encode("ascii"))
        return parse_json(raw)
    except Exception:  # noqa: BLE001 -- a pointer that will not open is a hole, and holes are named
        return None


def record_release(conn, *, request_id: str, grant_id: str, records, record_key: bytes | None, now: int) -> int:
    """File one row per released record. Returns how many were filed; never raises into the release path.

    `records` are the parsed disclosure's own records, in release order. `record_key` is the grant's record key
    when it has one (p2a-v3 and search); without it the pointer is sealed under a per-request key that nothing
    stores, so the row still names the release and resolves to nothing -- an honest hole rather than a plaintext id.
    """
    try:
        for statement in SCHEMA:
            conn.execute(statement)
        key = record_key or secrets.token_bytes(32)
        filed = 0
        for ordinal, record in enumerate(records):
            opaque = str(getattr(record, "record_id", "") or "")
            table = str(getattr(record, "canonical_table", "") or "")
            source = str(getattr(record, "source_id", "") or "")
            conn.execute(
                "INSERT OR REPLACE INTO p2a_shadow_released"
                " (request_id,ordinal,grant_id,opaque_record_id,canonical_table,source_id,sealed_pointer,released_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (request_id, ordinal, grant_id, opaque, table, source,
                 seal_pointer(key, opaque_id=opaque, record_id=opaque, canonical_table=table), int(now)))
            filed += 1
        _prune(conn, now=now)
        return filed
    except Exception:  # noqa: BLE001 -- an unauditable read is still a correct read
        _count_failure()
        logger.warning("permissions v2 shadow index: a release could not be made auditable")
        return 0


def _prune(conn, *, now: int) -> None:
    conn.execute("DELETE FROM p2a_shadow_released WHERE rowid IN (SELECT rowid FROM p2a_shadow_released"
                 " WHERE released_at < ? ORDER BY released_at LIMIT ?)",
                 (int(now) - RETENTION_DAYS * 86_400, PRUNE_BATCH))
    conn.execute("DELETE FROM p2a_shadow_released WHERE rowid IN (SELECT rowid FROM p2a_shadow_released"
                 " ORDER BY released_at DESC LIMIT ? OFFSET ?)", (PRUNE_BATCH, ROW_CAP))


def released(conn, *, request_id: str) -> list[dict]:
    """The rows filed for one release, in order. Empty when the node cannot answer for it."""
    try:
        for statement in SCHEMA:
            conn.execute(statement)
        return [dict(row) for row in conn.execute(
            "SELECT request_id,ordinal,grant_id,opaque_record_id,canonical_table,source_id,sealed_pointer,released_at"
            " FROM p2a_shadow_released WHERE request_id=? ORDER BY ordinal", (request_id,))]
    except Exception:  # noqa: BLE001
        return []


def now_seconds() -> int:
    return int(time.time())
