"""Enrolled MCP client registry — principal fabric P2.

One row per third-party client the owner has enrolled (their own Claude
Desktop, Cursor, …). Enrollment mints a per-client token

    tpk_<client_id>.<secret>

whose hash is stored at rest; the plaintext is shown exactly once, at minting.
Presenting a valid tpk token authenticates the caller as
``Principal(third_party, client_id=<id>)`` — identity, never authorization:
base access stays floored at scores_only, and any elevation is a consent
record in UMA's ledger with subject ``client:<id>`` (the "one consent ledger"
invariant in the Who's Asking doc, §03b). Revocation is a tombstone, not a
delete, so the audit trail keeps naming the client after it is gone.

The table is engine-owned operational state, created lazily on first use —
the same pattern as ``mcp_request_log`` next door, and deliberately NOT a
numbered migration: registry rows are not user data, and the lazy ensure
avoids migration-number contention across the many concurrent sessions that
share this tree.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.mcp_clients")

MCP_CLIENTS_TABLE = "mcp_clients"
TOKEN_PREFIX = "tpk_"

#: Client ids are slugs: stable, log-safe, and usable as UMA consent subjects
#: (``client:<id>``) without escaping.
_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_client_id(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(raw or "").strip().lower()).strip("-")
    if not slug or not _CLIENT_ID_RE.match(slug):
        raise ValueError(
            "client_id must be a slug of lowercase letters, digits and hyphens (max 64 chars)"
        )
    return slug


def ensure_mcp_clients_table(conn: sqlite3.Connection) -> None:
    from .core.state import commit_connection, with_db_write

    with with_db_write():
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MCP_CLIENTS_TABLE} (
                client_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                principal_class TEXT NOT NULL DEFAULT 'third_party',
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            )
            """
        )
        commit_connection(conn)


def mint_client_token(
    conn: sqlite3.Connection,
    *,
    client_id: str,
    display_name: str = "",
) -> Dict[str, Any]:
    """Enroll (or re-key) a client; returns the row plus the ONE-TIME plaintext token.

    Re-minting an existing client rotates its token and clears any revocation —
    that is the "re-enroll after cutover" path, and it is an owner action.
    """
    cid = normalize_client_id(client_id)
    name = str(display_name or "").strip() or cid
    token = f"{TOKEN_PREFIX}{cid}.{secrets.token_hex(20)}"
    ensure_mcp_clients_table(conn)
    from .core.state import commit_connection, with_db_write

    with with_db_write():
        conn.execute(
            f"""
            INSERT INTO {MCP_CLIENTS_TABLE}
                (client_id, display_name, principal_class, token_hash, created_at)
            VALUES (?, ?, 'third_party', ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                display_name = excluded.display_name,
                token_hash = excluded.token_hash,
                created_at = excluded.created_at,
                revoked_at = NULL
            """,
            (cid, name, _hash_token(token), _now()),
        )
        commit_connection(conn)
    row = get_client(conn, cid) or {}
    return {**row, "token": token}


def verify_client_token(conn: sqlite3.Connection, presented: str) -> Optional[Dict[str, Any]]:
    """Resolve a presented ``tpk_…`` bearer to its client row, or None.

    Fail closed on every branch: wrong shape, unknown id, hash mismatch, or a
    revocation tombstone all resolve to None (the caller 401s). The hash lookup
    is by client_id, so comparison is one constant-time digest check rather
    than a scan.
    """
    token = str(presented or "")
    if not token.startswith(TOKEN_PREFIX) or "." not in token:
        return None
    cid = token[len(TOKEN_PREFIX):].split(".", 1)[0]
    try:
        cid = normalize_client_id(cid)
    except ValueError:
        return None
    try:
        ensure_mcp_clients_table(conn)
        row = get_client(conn, cid)
    except Exception:  # noqa: BLE001 — DB trouble must never widen auth
        logger.warning("mcp client verify failed for %r", cid, exc_info=True)
        return None
    if not row or row.get("revoked_at"):
        return None
    if not secrets.compare_digest(str(row.get("token_hash") or ""), _hash_token(token)):
        return None
    _touch_last_used(conn, cid)
    return row


def _touch_last_used(conn: sqlite3.Connection, client_id: str) -> None:
    try:
        from .core.state import commit_connection, with_db_write

        with with_db_write():
            conn.execute(
                f"UPDATE {MCP_CLIENTS_TABLE} SET last_used_at = ? WHERE client_id = ?",
                (_now(), client_id),
            )
            commit_connection(conn)
    except Exception:  # noqa: BLE001 — telemetry must never fail auth
        logger.debug("last_used update failed for %r", client_id, exc_info=True)


def get_client(conn: sqlite3.Connection, client_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        f"SELECT client_id, display_name, principal_class, token_hash, created_at,"
        f" last_used_at, revoked_at FROM {MCP_CLIENTS_TABLE} WHERE client_id = ?",
        (client_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def list_clients(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Rows for the Settings UI — token hashes never leave the engine."""
    ensure_mcp_clients_table(conn)
    cur = conn.execute(
        f"SELECT client_id, display_name, principal_class, created_at, last_used_at,"
        f" revoked_at FROM {MCP_CLIENTS_TABLE} ORDER BY created_at"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def revoke_client(conn: sqlite3.Connection, client_id: str) -> bool:
    cid = normalize_client_id(client_id)
    ensure_mcp_clients_table(conn)
    from .core.state import commit_connection, with_db_write

    with with_db_write():
        cur = conn.execute(
            f"UPDATE {MCP_CLIENTS_TABLE} SET revoked_at = ? "
            f"WHERE client_id = ? AND revoked_at IS NULL",
            (_now(), cid),
        )
        commit_connection(conn)
    return cur.rowcount > 0
