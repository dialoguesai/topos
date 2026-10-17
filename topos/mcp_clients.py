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
MCP_CLIENT_ELEVATIONS_TABLE = "mcp_client_elevations"
TOKEN_PREFIX = "tpk_"

# --------------------------------------------------------------- agents
# The owner's own approved coding agents share the table but not the namespace
# (AI-tool on-ramp, CONTRACTS §1.1): row ``client_id = "agent-<client>"``,
# ``principal_class = "owner_agent"``, and a LABEL ``tpa_<client>.<40 hex>``
# whose hash is stored exactly like a client token's. The two never cross: a
# ``tpk_`` token never resolves to an agent row, a ``tpa_`` label never
# resolves to a third party, and Connected apps never lists, revokes or mints
# an ``agent-*`` row. It is a label, not a lock — it names the agent for audit
# and revoke — so re-minting a revoked one needs a fresh approval.
AGENT_LABEL_PREFIX = "tpa_"
AGENT_CLIENT_ID_PREFIX = "agent-"
AGENT_PRINCIPAL_CLASS = "owner_agent"
#: Canonical clients and their display names; any other slug is accepted and
#: shown as itself (the name is recorded as claimed).
AGENT_DISPLAY_NAMES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "gemini": "Gemini",
}

#: Elevation ceiling (owner decision, Who's Asking §06): a consented client may
#: reach `facts`, never `facts_all` — special-class content (health, beliefs,
#: admin) stays owner-first-party only. Requests for more are clamped, loudly.
ELEVATION_CEILING = "facts"

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
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MCP_CLIENT_ELEVATIONS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                resolution TEXT NOT NULL DEFAULT 'facts',
                status TEXT NOT NULL DEFAULT 'pending',
                note TEXT,
                requested_at TEXT NOT NULL,
                decided_at TEXT,
                expires_at TEXT
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
    if cid.startswith(AGENT_CLIENT_ID_PREFIX):
        raise ValueError(f"client_id prefix {AGENT_CLIENT_ID_PREFIX!r} is reserved for approved agents")
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
    if row.get("principal_class") == AGENT_PRINCIPAL_CLASS:
        return None  # an agent row is never a tpk client, whatever its hash says
    if not secrets.compare_digest(str(row.get("token_hash") or ""), _hash_token(token)):
        return None
    _touch_last_used(conn, cid)
    return row


def _agent_ids(client: str) -> tuple:
    """``(row client_id, short client name)`` for an agent, or ValueError."""
    short = normalize_client_id(client)
    if short.startswith(AGENT_CLIENT_ID_PREFIX):
        raise ValueError("name the agent by its client, not by its row id")
    return normalize_client_id(f"{AGENT_CLIENT_ID_PREFIX}{short}"), short


def mint_agent_label(conn: sqlite3.Connection, *, client: str, approved: bool = False) -> Dict[str, Any]:
    """Enroll (or re-key) an approved agent; returns the row plus the ONE-TIME plaintext ``label``.

    Writes nothing to disk — the label FILE (``~/.topos/agents/<client>.token``)
    is the CLI's. A revoked agent is re-minted only with ``approved=True``, which
    the CLI sets after a fresh approval (``--approved``) or a TTY confirmation;
    otherwise ``agents add`` would silently un-revoke (rev-1 security 12). The
    hash is never returned: the caller has the label itself, once.
    """
    cid, short = _agent_ids(client)
    ensure_mcp_clients_table(conn)
    existing = get_client(conn, cid)
    if existing is not None:
        if existing.get("principal_class") != AGENT_PRINCIPAL_CLASS:
            raise ValueError(f"{cid!r} is not an agent row")
        if existing.get("revoked_at") and not approved:
            raise ValueError(f"agent {short!r} was disconnected; reconnecting it needs a fresh approval")
    name = AGENT_DISPLAY_NAMES.get(short, short)
    label = f"{AGENT_LABEL_PREFIX}{short}.{secrets.token_hex(20)}"
    from .core.state import commit_connection, with_db_write

    with with_db_write():
        conn.execute(
            f"""
            INSERT INTO {MCP_CLIENTS_TABLE}
                (client_id, display_name, principal_class, token_hash, created_at)
            VALUES (?, ?, '{AGENT_PRINCIPAL_CLASS}', ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                display_name = excluded.display_name,
                token_hash = excluded.token_hash,
                created_at = excluded.created_at,
                revoked_at = NULL
            """,
            (cid, name, _hash_token(label), _now()),
        )
        commit_connection(conn)
    row = dict(get_client(conn, cid) or {})
    row.pop("token_hash", None)
    return {**row, "client": short, "label": label}


def verify_agent_label(conn: sqlite3.Connection, presented: str) -> Optional[Dict[str, Any]]:
    """Resolve a presented ``tpa_…`` label to its agent row, or None.

    Fail closed on every branch, as ``verify_client_token`` does: wrong prefix
    (a ``tpk_`` token never matches), wrong shape, unknown agent, a row that is
    not ``owner_agent``, a revocation tombstone, or a hash mismatch. Read from
    the table on every call — revocation is checked per request, cached nowhere.
    """
    label = str(presented or "")
    if not label.startswith(AGENT_LABEL_PREFIX) or "." not in label:
        return None
    short = label[len(AGENT_LABEL_PREFIX):].split(".", 1)[0]
    try:
        cid, _ = _agent_ids(short)
    except ValueError:
        return None
    try:
        ensure_mcp_clients_table(conn)
        row = get_client(conn, cid)
    except Exception:  # noqa: BLE001 — DB trouble must never widen auth
        logger.warning("agent label verify failed for %r", cid, exc_info=True)
        return None
    if not row or row.get("revoked_at") or row.get("principal_class") != AGENT_PRINCIPAL_CLASS:
        return None
    if not secrets.compare_digest(str(row.get("token_hash") or ""), _hash_token(label)):
        return None
    _touch_last_used(conn, cid)
    return row


def revoke_agent(conn: sqlite3.Connection, *, client: str) -> bool:
    """Tombstone an agent's row; the next request with its label is refused."""
    cid, _ = _agent_ids(client)
    ensure_mcp_clients_table(conn)
    from .core.state import commit_connection, with_db_write

    with with_db_write():
        cur = conn.execute(
            f"UPDATE {MCP_CLIENTS_TABLE} SET revoked_at = ? "
            f"WHERE client_id = ? AND principal_class = '{AGENT_PRINCIPAL_CLASS}' AND revoked_at IS NULL",
            (_now(), cid),
        )
        commit_connection(conn)
    return cur.rowcount > 0


def list_agents(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The agents, revoked ones included (tombstones), never a hash."""
    ensure_mcp_clients_table(conn)
    cur = conn.execute(
        f"SELECT client_id, display_name, created_at, last_used_at, revoked_at"
        f" FROM {MCP_CLIENTS_TABLE} WHERE principal_class = '{AGENT_PRINCIPAL_CLASS}' ORDER BY created_at"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for row in rows:
        row["client"] = str(row["client_id"])[len(AGENT_CLIENT_ID_PREFIX):]
    return rows


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
    """Rows for the Settings UI — token hashes never leave the engine. Agents
    (``principal_class = 'owner_agent'``) have their own list, ``list_agents``."""
    ensure_mcp_clients_table(conn)
    cur = conn.execute(
        f"SELECT client_id, display_name, principal_class, created_at, last_used_at,"
        f" revoked_at FROM {MCP_CLIENTS_TABLE} WHERE principal_class != '{AGENT_PRINCIPAL_CLASS}'"
        f" ORDER BY created_at"
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
            f"WHERE client_id = ? AND principal_class != '{AGENT_PRINCIPAL_CLASS}' AND revoked_at IS NULL",
            (_now(), cid),
        )
        commit_connection(conn)
    return cur.rowcount > 0


# --------------------------------------------------------------- elevations
# Consent records for a client to receive fact CONTENT above the scores_only
# floor. UMA-shaped lifecycle (pending -> approved/denied -> revoked; expiry),
# subject ``client:<id>``; the engine's node-local ledger because the engine is
# the enforcement point — the CP/FE surface reads it by relay proxy, and every
# decision is mirrored into the UMA audit tables via record_uma_request.


def request_elevation(
    conn: sqlite3.Connection,
    *,
    client_id: str,
    scope_id: str,
    note: str = "",
) -> Dict[str, Any]:
    """File a pending elevation request. Idempotent per (client, scope): an
    existing pending row is returned rather than duplicated, and an ACTIVE
    approval short-circuits (nothing to ask for)."""
    cid = normalize_client_id(client_id)
    scope = str(scope_id or "").strip()
    if not scope:
        raise ValueError("scope_id is required")
    ensure_mcp_clients_table(conn)
    client = get_client(conn, cid)
    if client is None or client.get("principal_class") == AGENT_PRINCIPAL_CLASS:
        # An agent reads by ruling D1, not by a consent record: nothing to elevate.
        raise ValueError(f"unknown client {cid!r} — enroll it first")
    existing = active_elevation(conn, client_id=cid, scope_id=scope)
    if existing is not None:
        return existing
    cur = conn.execute(
        f"SELECT id FROM {MCP_CLIENT_ELEVATIONS_TABLE} "
        f"WHERE client_id = ? AND scope_id = ? AND status = 'pending'",
        (cid, scope),
    )
    row = cur.fetchone()
    if row is not None:
        return get_elevation(conn, int(row[0]))  # type: ignore[return-value]
    from .core.state import commit_connection, with_db_write

    with with_db_write():
        cur = conn.execute(
            f"INSERT INTO {MCP_CLIENT_ELEVATIONS_TABLE}"
            f" (client_id, scope_id, resolution, status, note, requested_at)"
            f" VALUES (?, ?, ?, 'pending', ?, ?)",
            (cid, scope, ELEVATION_CEILING, str(note or "")[:500], _now()),
        )
        commit_connection(conn)
    return get_elevation(conn, int(cur.lastrowid))  # type: ignore[return-value]


def decide_elevation(
    conn: sqlite3.Connection,
    *,
    request_id: int,
    approve: bool,
    expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Owner decision on a pending request. Approval is per-scope and expiring;
    the resolution is clamped to ELEVATION_CEILING no matter what was asked."""
    row = get_elevation(conn, int(request_id))
    if row is None:
        raise ValueError(f"no elevation request {request_id}")
    if row["status"] != "pending":
        raise ValueError(f"request {request_id} already {row['status']}")
    status = "approved" if approve else "denied"
    from .core.state import commit_connection, with_db_write

    with with_db_write():
        conn.execute(
            f"UPDATE {MCP_CLIENT_ELEVATIONS_TABLE}"
            f" SET status = ?, decided_at = ?, expires_at = ?, resolution = ?"
            f" WHERE id = ?",
            (status, _now(), str(expires_at or "") or None, ELEVATION_CEILING, int(request_id)),
        )
        commit_connection(conn)
    return get_elevation(conn, int(request_id))  # type: ignore[return-value]


def revoke_elevation(conn: sqlite3.Connection, *, client_id: str, scope_id: str = "") -> int:
    """Revoke approved elevations for a client (one scope, or all when omitted).
    Tombstones, not deletes — the consent history stays legible."""
    cid = normalize_client_id(client_id)
    ensure_mcp_clients_table(conn)
    from .core.state import commit_connection, with_db_write

    clause = "client_id = ? AND status = 'approved'"
    args: list = [cid]
    if str(scope_id or "").strip():
        clause += " AND scope_id = ?"
        args.append(str(scope_id).strip())
    with with_db_write():
        cur = conn.execute(
            f"UPDATE {MCP_CLIENT_ELEVATIONS_TABLE} SET status = 'revoked', decided_at = ?"
            f" WHERE {clause}",
            (_now(), *args),
        )
        commit_connection(conn)
    return cur.rowcount


def get_elevation(conn: sqlite3.Connection, request_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        f"SELECT id, client_id, scope_id, resolution, status, note, requested_at,"
        f" decided_at, expires_at FROM {MCP_CLIENT_ELEVATIONS_TABLE} WHERE id = ?",
        (int(request_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def list_elevations(conn: sqlite3.Connection, *, client_id: str = "") -> List[Dict[str, Any]]:
    ensure_mcp_clients_table(conn)
    clause, args = "", []
    if str(client_id or "").strip():
        clause = " WHERE client_id = ?"
        args = [normalize_client_id(client_id)]
    cur = conn.execute(
        f"SELECT id, client_id, scope_id, resolution, status, note, requested_at,"
        f" decided_at, expires_at FROM {MCP_CLIENT_ELEVATIONS_TABLE}{clause}"
        f" ORDER BY requested_at",
        args,
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def active_elevation(
    conn: sqlite3.Connection,
    *,
    client_id: str,
    scope_id: str,
) -> Optional[Dict[str, Any]]:
    """The approved, unexpired elevation covering this (client, scope), or None.

    Fail closed on every branch — a revoked client has no active elevations no
    matter what the rows say, and expiry is checked at read time so no reaper
    is load-bearing. A grant for scope '*' covers every scope.
    """
    try:
        cid = normalize_client_id(client_id)
    except ValueError:
        return None
    scope = str(scope_id or "").strip()
    try:
        ensure_mcp_clients_table(conn)
        client = get_client(conn, cid)
        if client is None or client.get("revoked_at"):
            return None
        cur = conn.execute(
            f"SELECT id, client_id, scope_id, resolution, status, note, requested_at,"
            f" decided_at, expires_at FROM {MCP_CLIENT_ELEVATIONS_TABLE}"
            f" WHERE client_id = ? AND status = 'approved' AND scope_id IN (?, '*')",
            (cid, scope),
        )
        cols = [d[0] for d in cur.description]
        now = _now()
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            exp = str(row.get("expires_at") or "")
            if exp and exp <= now:
                continue
            return row
    except Exception:  # noqa: BLE001 — DB trouble must never widen disclosure
        logger.warning("active_elevation lookup failed for %r", client_id, exc_info=True)
    return None
