"""Local pushers: apps on this computer that write sources into the node directly, with no account.

The browser extension, voxterm and the ChatGPT capture app post their rows to the
control plane today, which does four things before relaying them to the node:
it stamps the writer (``owner_write_stamp.py`` rule C: the owner's attested
capture app on a source it captures), it checks which sources the app may write
(``routes/ingestion.py``: the permission's scope and the app's declared
sources), it dedupes redeliveries by ``write_id`` and takes a batch, and it
answers CORS for the extension's own origin. A node that runs with no account
has no control plane in front of it, so the node does the same four things
itself here before any local pusher ships (``audits/2026-11-05-local-first-onramp/SCOPE.md``,
"Safety steps the CP does today that a local path must do itself").

**Enrolment is the owner's act, on a terminal** (``topos-node pushers add``,
``cli/pushers_cmd.py``), the pattern the coding agents use: the owner says yes
once on a TTY, names the sources the app may write and the browser-extension
origins it calls from, and the app gets its own key

    tpw_<app>.<40 hex>

shown exactly once; its hash is the row, in the same ``mcp_clients`` table the
agents and the enrolled MCP clients use, under ``principal_class =
'local_pusher'`` and the row id ``pusher-<app>``. The three namespaces never
cross: a ``tpk_`` token never resolves to a pusher row, a ``tpw_`` key never
resolves to a third-party client or an agent, and Connected apps neither lists
nor revokes a pusher.

**What the key is worth.** Presented over loopback TCP on the pusher doors
(``api/local_pushers.py``) it writes the app's allowlisted sources as the
owner's attested capture app: rows carry ``writer_class = owner_app`` and
``writer_app_id = <app>``, exactly what the control plane's stamp gives the
same extension on an account node, so the two paths produce the same rows and
``local_identity.rescope_to_account`` moves them like any other. It opens
nothing else: ``auth.require_api_key`` and ``auth.resolve_request_principal``
refuse it, so a leaked key reaches the batch door and nothing more, and the
allowlist caps even that to the sources the owner named. A pusher never reads.

**Origins.** CORS is answered for the exact extension origins the owner enrolled
(``chrome-extension://<32 chars>``, ``moz-extension://<uuid>``,
``safari-web-extension://<uuid>``). A wildcard is refused at enrolment; there is
no ``chrome-extension://*`` anywhere.

Revocation is a tombstone, checked on every request and cached nowhere. The
table is created lazily, as ``mcp_clients`` is.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

logger = logging.getLogger("topos.local_pushers")

PUSHER_KEY_PREFIX = "tpw_"
PUSHER_CLIENT_ID_PREFIX = "pusher-"
PUSHER_PRINCIPAL_CLASS = "local_pusher"
#: The channel a pusher's principal is minted on (``auth.resolve_local_pusher``). A
#: writer-class reader (``features/provenance/writer_class.writer_app_for_principal``)
#: names the app for this channel as it does for a stamped relay write.
PUSHER_CHANNEL = "local_pusher"
PUSHER_GRANTS_TABLE = "local_pusher_grants"

#: The owner's first-party capture apps, by the id the control plane stamps
#: (``control_plane/config.OWNER_CAPTURE_APP_DEFAULT``). Any other slug is an app
#: of the owner's own naming; it is enrolled the same way.
PUSHER_DISPLAY_NAMES = {
    "browser-history-plugin": "Dialogues Browser History",
    "chatgpt-shadow-extension": "ChatGPT capture",
    "voxterm": "voxterm",
}

#: An extension's own origin: one of the three extension schemes and an id made of
#: letters, digits and hyphens (Chrome: 32 letters a–p; Firefox and Safari: a
#: UUID). Nothing else, and never a wildcard.
EXTENSION_ORIGIN_RE = re.compile(
    r"^(chrome-extension|moz-extension|safari-web-extension)://[A-Za-z0-9-]{8,64}$"
)

#: Records per batch. The extension sends one or a handful; the cap is for a
#: runaway client, not a budget.
MAX_BATCH_RECORDS = 500


class PusherRefused(ValueError):
    """An enrolment the registry will not record: an unknown or non-push source, a bad origin, a revoked app."""


@dataclass(frozen=True)
class PusherGrant:
    """What one pusher may write, and where it calls from. Stored on its enrolment, read on every request."""

    sources: Tuple[str, ...] = ()
    origins: Tuple[str, ...] = ()

    def may_write(self, source_id: str) -> bool:
        return str(source_id or "").strip() in self.sources

    def as_json(self) -> str:
        return json.dumps({"sources": list(self.sources), "origins": list(self.origins)})

    def plain(self) -> str:
        where = f" from {', '.join(self.origins)}" if self.origins else ""
        return f"write {', '.join(self.sources)}{where}"


NO_PUSHER_GRANT = PusherGrant()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pusher_ids(app: str) -> Tuple[str, str]:
    """``(row client_id, app)`` for a pusher, or ValueError on a name that is not a slug."""
    from .mcp_clients import AGENT_CLIENT_ID_PREFIX, normalize_client_id

    short = normalize_client_id(app)
    if short.startswith(PUSHER_CLIENT_ID_PREFIX) or short.startswith(AGENT_CLIENT_ID_PREFIX):
        raise ValueError("name the app itself, not its row id")
    return normalize_client_id(f"{PUSHER_CLIENT_ID_PREFIX}{short}"), short


def is_extension_origin(origin: str) -> bool:
    return bool(EXTENSION_ORIGIN_RE.match(str(origin or "").strip()))


def parse_origins(words: Iterable[str]) -> Tuple[str, ...]:
    """The extension origins an enrolment names, checked one by one. A wildcard, a web origin or a bare id is refused."""
    origins: List[str] = []
    for raw in words:
        origin = str(raw or "").strip().rstrip("/")
        if not origin:
            continue
        if not is_extension_origin(origin):
            raise PusherRefused(
                f"{origin!r} is not an extension origin; expected chrome-extension://<id>, "
                "moz-extension://<uuid> or safari-web-extension://<uuid>, never a wildcard"
            )
        origins.append(origin)
    return tuple(dict.fromkeys(origins))


def parse_sources(words: Iterable[str], *, registry=None) -> Tuple[str, ...]:
    """The push sources an enrolment names: each must be in the registry and take client pushes."""
    from .sources.definitions import accepts_app_ingest
    from .sources.registry import REGISTRY

    registry = REGISTRY if registry is None else registry

    sources: List[str] = []
    for raw in words:
        source_id = str(raw or "").strip()
        if not source_id:
            continue
        definition = registry.get(source_id)
        if definition is None:
            raise PusherRefused(f"this Topos has no source named {source_id!r}")
        if not accepts_app_ingest(definition):
            raise PusherRefused(
                f"{source_id!r} is not a source an app pushes (delivery={definition.delivery!r})"
            )
        sources.append(source_id)
    if not sources:
        raise PusherRefused("name at least one source the app may write")
    return tuple(dict.fromkeys(sources))


def ensure_local_pushers_table(conn: sqlite3.Connection) -> None:
    from .core.state import commit_connection, with_db_write
    from .mcp_clients import ensure_mcp_clients_table

    ensure_mcp_clients_table(conn)
    with with_db_write():
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PUSHER_GRANTS_TABLE} (
                client_id TEXT PRIMARY KEY,
                grant_json TEXT NOT NULL,
                granted_at TEXT NOT NULL
            )
            """
        )
        commit_connection(conn)


def read_pusher_grant(conn: sqlite3.Connection, client_id: str) -> PusherGrant:
    """The stored grant for a ``pusher-<app>`` row. A row with none writes nothing."""
    try:
        row = conn.execute(
            f"SELECT grant_json FROM {PUSHER_GRANTS_TABLE} WHERE client_id = ?", (client_id,)
        ).fetchone()
        data = json.loads(row[0]) if row else {}
        return PusherGrant(
            tuple(str(s) for s in data.get("sources") or ()),
            tuple(str(o) for o in data.get("origins") or ()),
        )
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        logger.warning("pusher grant read failed for %r", client_id, exc_info=True)  # never widen a grant
        return NO_PUSHER_GRANT


def enrol_pusher(
    conn: sqlite3.Connection,
    *,
    app: str,
    grant: PusherGrant,
    approved: bool = False,
    display_name: str = "",
) -> Dict[str, Any]:
    """Enrol (or re-key) a pusher; returns the row plus the ONE-TIME plaintext ``key``.

    ``grant`` replaces what the app may write and where it calls from. Re-running
    rotates the key. A revoked app is re-enrolled only with ``approved=True``,
    which the CLI sets after a fresh yes on a terminal, never silently. The hash is
    not returned: the caller has the key itself, once.
    """
    from .core.state import commit_connection, with_db_write
    from .mcp_clients import MCP_CLIENTS_TABLE, get_client

    cid, short = pusher_ids(app)
    if not grant.sources:
        raise PusherRefused("name at least one source the app may write")
    ensure_local_pushers_table(conn)
    existing = get_client(conn, cid)
    if existing is not None:
        if existing.get("principal_class") != PUSHER_PRINCIPAL_CLASS:
            raise PusherRefused(f"{cid!r} is not a pusher row")
        if existing.get("revoked_at") and not approved:
            raise PusherRefused(f"{short!r} was disconnected; reconnecting it needs a fresh approval")
    name = str(display_name or "").strip() or PUSHER_DISPLAY_NAMES.get(short, short)
    key = f"{PUSHER_KEY_PREFIX}{short}.{secrets.token_hex(20)}"
    with with_db_write():
        conn.execute(
            f"""
            INSERT INTO {MCP_CLIENTS_TABLE}
                (client_id, display_name, principal_class, token_hash, created_at)
            VALUES (?, ?, '{PUSHER_PRINCIPAL_CLASS}', ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                display_name = excluded.display_name,
                token_hash = excluded.token_hash,
                created_at = excluded.created_at,
                revoked_at = NULL
            """,
            (cid, name, _hash(key), _now()),
        )
        conn.execute(
            f"""
            INSERT INTO {PUSHER_GRANTS_TABLE} (client_id, grant_json, granted_at) VALUES (?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET grant_json = excluded.grant_json, granted_at = excluded.granted_at
            """,
            (cid, grant.as_json(), _now()),
        )
        commit_connection(conn)
    row = dict(get_client(conn, cid) or {})
    row.pop("token_hash", None)
    return {**row, "app": short, "grant": grant, "key": key}


def verify_pusher_key(conn: sqlite3.Connection, presented: str) -> Optional[Dict[str, Any]]:
    """Resolve a presented ``tpw_…`` key to its pusher row (with its grant), or None.

    Fail closed on every branch: wrong prefix (a ``tpk_`` token or a ``tpa_`` label
    never matches), wrong shape, unknown app, a row that is not ``local_pusher``, a
    tombstone, a hash mismatch. Read from the table on every call.
    """
    from .mcp_clients import _touch_last_used, get_client

    key = str(presented or "")
    if not key.startswith(PUSHER_KEY_PREFIX) or "." not in key:
        return None
    short = key[len(PUSHER_KEY_PREFIX):].split(".", 1)[0]
    try:
        cid, short = pusher_ids(short)
    except ValueError:
        return None
    try:
        ensure_local_pushers_table(conn)
        row = get_client(conn, cid)
    except Exception:  # noqa: BLE001 — DB trouble must never widen auth
        logger.warning("pusher key verify failed for %r", cid, exc_info=True)
        return None
    if not row or row.get("revoked_at") or row.get("principal_class") != PUSHER_PRINCIPAL_CLASS:
        return None
    if not secrets.compare_digest(str(row.get("token_hash") or ""), _hash(key)):
        return None
    _touch_last_used(conn, cid)
    public = {k: v for k, v in row.items() if k != "token_hash"}
    return {**public, "app": short, "grant": read_pusher_grant(conn, cid)}


def revoke_pusher(conn: sqlite3.Connection, *, app: str) -> bool:
    """Tombstone a pusher's row; the next request with its key is refused."""
    from .core.state import commit_connection, with_db_write
    from .mcp_clients import MCP_CLIENTS_TABLE

    cid, _ = pusher_ids(app)
    ensure_local_pushers_table(conn)
    with with_db_write():
        cur = conn.execute(
            f"UPDATE {MCP_CLIENTS_TABLE} SET revoked_at = ? "
            f"WHERE client_id = ? AND principal_class = '{PUSHER_PRINCIPAL_CLASS}' AND revoked_at IS NULL",
            (_now(), cid),
        )
        commit_connection(conn)
    return cur.rowcount > 0


def list_pushers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The pushers, revoked ones included (tombstones), never a hash."""
    from .mcp_clients import MCP_CLIENTS_TABLE

    ensure_local_pushers_table(conn)
    cur = conn.execute(
        f"SELECT client_id, display_name, created_at, last_used_at, revoked_at"
        f" FROM {MCP_CLIENTS_TABLE} WHERE principal_class = '{PUSHER_PRINCIPAL_CLASS}' ORDER BY created_at"
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for row in rows:
        row["app"] = str(row["client_id"])[len(PUSHER_CLIENT_ID_PREFIX):]
        grant = read_pusher_grant(conn, row["client_id"])
        row["sources"] = list(grant.sources)
        row["origins"] = list(grant.origins)
    return rows


def enrolled_origins(conn: Optional[sqlite3.Connection]) -> FrozenSet[str]:
    """The extension origins of every unrevoked pusher: what the pusher doors answer CORS for.

    A read on every preflight, so revoking an app closes its origin on the next
    request. Any trouble reading answers no origins (fail closed); a database
    without the tables has no pushers.
    """
    if conn is None:
        return frozenset()
    from .mcp_clients import MCP_CLIENTS_TABLE

    try:
        rows = conn.execute(
            f"SELECT g.grant_json FROM {PUSHER_GRANTS_TABLE} g JOIN {MCP_CLIENTS_TABLE} c ON c.client_id = g.client_id"
            f" WHERE c.principal_class = '{PUSHER_PRINCIPAL_CLASS}' AND c.revoked_at IS NULL"
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    origins = set()
    for (grant_json,) in rows:
        try:
            for origin in json.loads(grant_json).get("origins") or ():
                if is_extension_origin(str(origin)):
                    origins.add(str(origin))
        except (ValueError, TypeError, AttributeError):
            continue
    return frozenset(origins)


def source_not_allowed_detail(app: str, source_id: str, grant: PusherGrant) -> str:
    """Mirror of the control plane's ``undeclared_source_detail``: what was refused and how to allow it."""
    allowed = ", ".join(grant.sources) if grant.sources else "nothing"
    return (
        f"Source '{source_id}' is not one '{app}' may write (allowed: {allowed}). "
        f"Re-run `topos-node pushers add {app} --sources {','.join(sorted(set(grant.sources) | {source_id}))}` "
        "on this computer to allow it."
    )
