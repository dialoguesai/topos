"""The owner's sharing screens, over the relay (A2A-3 §7; any-to-any N4 and N5).

Relayed owner commands the any-to-any screens need from the node, answered for the node's owner only:

- ``permissions_v2_share_catalog`` (bound or not): the sources the owner can share from and the kinds this node
  releases (``permissions_v2.share_catalog``);
- ``permissions_v2_share_week`` (bound): what a share's recipients used in a window (``permissions_v2.share_week``).

Every one is the same kind of door (A2A-3 §7):

- registered ``owner_only``, so the dispatcher refuses any principal but ``owner_app``;
- here, the channel must be the control plane's relay and the stamp's ``acting_user`` the node's owner: the ledger
  identity's owner when the node is bound, ``engine_config.user_id`` when it is not. Anything else, and the control
  plane's automatic re-sync client (status only, owner decision 2), is 403 ``owner_authority_required``;
- a message for a bound node carries ``binding``, the node identity, which must equal the ledger's: else 409
  ``binding_mismatch``. A message that may reach an unbound node may still carry one, and then it is checked too;
- the reply is ``{"id", "type", "status": "ok", "payload"}`` or ``{"id", "type", "status": "error", "code",
  "error"}`` with a closed code; a frame that does not have the documented shape is 400 ``payload_invalid``;
- counts, ids and fixed sentences only.

The work runs off the event loop (``asyncio.to_thread``), which carries the principal with it.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .registry import handles

CATALOG = "permissions_v2_share_catalog"
WEEK = "permissions_v2_share_week"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_MISSING = object()


class _Refused(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass
class _Owner:
    """What the gate established: the payload, and the runtime and identity when the node is bound and loads."""
    payload: dict
    runtime: Any
    identity: Any
    served: Optional[Path]


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 200 and _IDENTIFIER.fullmatch(value) is not None


def _integer(value: Any) -> bool:
    from ...permissions_v2.canonical import MAX_INTEGER
    return type(value) is int and 0 <= value <= MAX_INTEGER


def _served_database() -> Optional[Path]:
    from ...permissions_v2.runtime import _served_database as served
    try:
        return served().resolve(strict=True)
    except Exception:  # noqa: BLE001 -- no database to serve: nothing to answer from
        return None


def _engine_owner(served: Optional[Path]) -> Optional[str]:
    """``engine_config.user_id`` of the served database, read-only; None when it cannot be read."""
    if served is None:
        return None
    try:
        conn = sqlite3.connect(served.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    value = row[0] if row else None
    return value if isinstance(value, str) and value.strip() else None


def _owner_gate(message: dict, *, bound_only: bool, unavailable: str) -> _Owner:
    """The owner check and the binding check every one of these messages makes, in that order."""
    from ...permissions_v2 import switches
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.ledger import NodeIdentity
    from ...permissions_v2.protection_doorbell import AUTO_RESYNC_CLIENT
    from ...principal import OWNER_APP, current_principal

    principal = current_principal()
    if (principal is None or principal.cls != OWNER_APP or principal.channel != "cp_relay"
            or not principal.acting_user or principal.client_id == AUTO_RESYNC_CLIENT):
        raise _Refused(403, "owner_authority_required")
    bound = switches.is_bound()
    runtime = identity = None
    if bound:
        try:
            from ...permissions_v2.runtime import get_runtime
            runtime = get_runtime()
            identity = runtime.protocol.ledger.identity
        except Exception:  # noqa: BLE001 -- a bound node whose sharing will not load; decided below
            runtime = identity = None
    served = Path(runtime.protocol.canonical_database) if runtime is not None else _served_database()
    owner = identity.owner_id if identity is not None else _engine_owner(served)
    if owner is None or principal.acting_user != owner:
        raise _Refused(403, "owner_authority_required")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise _Refused(400, "payload_invalid")
    binding = payload.get("binding", _MISSING)
    if bound_only or binding is not _MISSING:
        if identity is None:
            # Bound but not loadable: the identity cannot be read now. Not bound: no binding can match.
            raise _Refused(503, unavailable) if bound else _Refused(409, "binding_mismatch")
        try:
            named = NodeIdentity.parse(binding) if isinstance(binding, dict) else None
        except PolicyError:
            named = None
        if named is None or named != identity:
            raise _Refused(409, "binding_mismatch")
    return _Owner(payload=payload, runtime=runtime, identity=identity, served=served)


async def _respond(message: dict, kind: str, work: Callable[[], dict], *, unavailable: str) -> dict:
    """Run ``work`` off the loop; its answer, a closed refusal, or ``unavailable`` for anything else."""
    request_id = message.get("id")
    try:
        payload = await asyncio.to_thread(work)
    except _Refused as refused:
        return {"id": request_id, "type": kind, "status": "error", "code": refused.status, "error": refused.code}
    except Exception:  # noqa: BLE001 -- never a path, a row or an exception chain on the wire
        return {"id": request_id, "type": kind, "status": "error", "code": 503, "error": unavailable}
    return {"id": request_id, "type": kind, "status": "ok", "payload": payload}


def _keys(value: Any, allowed: set, required: set = frozenset()) -> dict:
    if not isinstance(value, dict) or not set(value) <= allowed or not required <= set(value):
        raise _Refused(400, "payload_invalid")
    return value


# --- permissions_v2_share_catalog (A2A-3 §7.1) ---------------------------------------------------------------

@handles("permissions_v2_share_catalog", owner_only=True)
async def handle_permissions_v2_share_catalog(message):
    """Owner-only: every source with rows in a shareable table, every install under the named scopes, the kinds."""
    def work():
        from ...permissions_v2.share_catalog import MAX_SCOPES, SCOPE_FIELDS, catalog
        owner = _owner_gate(message, bound_only=False, unavailable="catalog_unavailable")
        payload = _keys(owner.payload, {"binding", "request"}, {"request"})
        request = _keys(payload["request"], {"scopes"}, {"scopes"})
        scopes = request["scopes"]
        if not isinstance(scopes, list) or len(scopes) > MAX_SCOPES:
            raise _Refused(400, "payload_invalid")
        owner_id = owner.identity.owner_id if owner.identity is not None else _engine_owner(owner.served)
        checked = []
        for scope in scopes:
            scope = _keys(scope, set(SCOPE_FIELDS), set(SCOPE_FIELDS))
            # Strings with no padding, never a wildcard, and this node's owner's: a scope never lists another's.
            if any(not _identifier(scope[field]) for field in SCOPE_FIELDS) or scope["user_id"] != owner_id:
                raise _Refused(400, "payload_invalid")
            checked.append({field: scope[field] for field in SCOPE_FIELDS})
        if owner.served is None:
            raise _Refused(503, "catalog_unavailable")
        return catalog(owner.served, checked, now=int(time.time()))
    return await _respond(message, CATALOG, work, unavailable="catalog_unavailable")


# --- permissions_v2_share_week (A2A-3 §7.3) ------------------------------------------------------------------

@handles("permissions_v2_share_week", owner_only=True)
async def handle_permissions_v2_share_week(message):
    """Owner-only: items used, answered and not answered for 1 to 20 of this node's shares in [since, until)."""
    def work():
        from ...permissions_v2.share_week import MAX_GRANTS, week
        owner = _owner_gate(message, bound_only=True, unavailable="week_unavailable")
        payload = _keys(owner.payload, {"binding", "request"}, {"binding", "request"})
        request = _keys(payload["request"], {"grant_ids", "since", "until"}, {"grant_ids", "since", "until"})
        grant_ids, since, until = request["grant_ids"], request["since"], request["until"]
        if (not isinstance(grant_ids, list) or not 1 <= len(grant_ids) <= MAX_GRANTS
                or len(set(grant_ids)) != len(grant_ids) or not all(_identifier(grant) for grant in grant_ids)
                or not _integer(since) or not _integer(until) or since > until):
            raise _Refused(400, "payload_invalid")
        return week(owner.runtime.protocol.ledger.path, grant_ids, since=since, until=until)
    return await _respond(message, WEEK, work, unavailable="week_unavailable")
