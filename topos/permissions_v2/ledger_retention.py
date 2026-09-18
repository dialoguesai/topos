"""Retention for the node's per-read ledger rows (design §7 F3/F4).

Every recipient read admits one `p2a_requests` row carrying the whole signed envelope
(~2.8 KB), kept forever: several GB per million reads. The envelope is needed only
until the request is checkpointed or the envelope expires; replay protection needs
only the request id, which `admit` checks first. So once `expires_at + SKEW` has
passed, the row keeps `request_id`, `envelope_hash` and `status` and drops the
envelope: a hash-only replay tombstone. The tombstone itself is kept, so a node whose
clock steps back still refuses a replay. Receipts are the owner's audit and are never
touched here.

The work rides on each admission, inside its transaction, at most BATCH rows, found
through a partial index on the envelope's own expiry. No daemon, no numbered migration:
the ledger is a node-private file and the index is created with its other tables.
"""
from __future__ import annotations

SKEW = 300
BATCH = 32
EXPIRY_INDEX = ("CREATE INDEX IF NOT EXISTS p2a_requests_expiry ON p2a_requests(json_extract(envelope_json,'$.expires_at')) "
                "WHERE envelope_json<>''")


def compact_expired(conn, *, now: int) -> int:
    """Drop the envelopes of at most BATCH requests expired before `now - SKEW`. Returns rows compacted."""
    return conn.execute(
        "UPDATE p2a_requests SET envelope_json='' WHERE rowid IN (SELECT rowid FROM p2a_requests "
        "WHERE envelope_json<>'' AND json_extract(envelope_json,'$.expires_at') < ? LIMIT ?)",
        (now - SKEW, BATCH)).rowcount
