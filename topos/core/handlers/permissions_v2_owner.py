"""The owner's sharing screens, over the relay (A2A-3 §7; any-to-any N4 and N5).

Relayed owner commands the any-to-any screens need from the node, answered for the node's owner only:

- ``permissions_v2_share_catalog`` (bound or not): the sources the owner can share from and the kinds this node
  releases (``permissions_v2.share_catalog``);
- ``permissions_v2_share_counts`` (bound): what a compiled policy would cover now, per kind, with the held-back
  reasons, counts only (``permissions_v2.share_counts``);
- ``permissions_v2_share_week`` (bound): what a share's recipients used in a window (``permissions_v2.share_week``);
- ``permissions_v2_ownership`` (bound): what counts as the owner's, and the owner's word on it
  (``permissions_v2.ownership``);
- ``permissions_v2_checking_model`` (bound or not; N5): the checking model's status, and its download after the
  owner's yes (``permissions_v2.checking_model``).

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
COUNTS = "permissions_v2_share_counts"
WEEK = "permissions_v2_share_week"
OWNERSHIP = "permissions_v2_ownership"
CHECKING_MODEL = "permissions_v2_checking_model"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
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


# --- permissions_v2_share_counts (A2A-3 §7.2) ----------------------------------------------------------------

@handles("permissions_v2_share_counts", owner_only=True)
async def handle_permissions_v2_share_counts(message):
    """Owner-only: per kind, what a compiled policy would share now and why the rest is held back. Counts only."""
    def work():
        from ...permissions_v2.canonical import PolicyError
        from ...permissions_v2.registry import parse_policy
        from ...permissions_v2.search_contract import DIRECT_SEARCH_CAPABILITIES
        from ...permissions_v2.share_counts import count
        owner = _owner_gate(message, bound_only=True, unavailable="counts_unavailable")
        payload = _keys(owner.payload, {"binding", "request"}, {"binding", "request"})
        request = _keys(payload["request"], {"policy"}, {"policy"})
        if not isinstance(request["policy"], dict):
            raise _Refused(400, "policy_invalid")
        try:
            policy = parse_policy(request["policy"])
        except PolicyError:
            raise _Refused(400, "policy_invalid") from None
        # Message and knowledge search only: what a share made from a draft is (p2c-v2, p2c-v3).
        if policy.versions.capability not in DIRECT_SEARCH_CAPABILITIES:
            raise _Refused(400, "policy_invalid")
        # The policy must be for this node: a draft compiled for another Topos counts nothing here.
        if any(getattr(policy.binding, field) != value for field, value in owner.identity.model_dump().items()):
            raise _Refused(409, "binding_mismatch")
        service = owner.runtime.evidence_reviews(require_existing=True)
        return count(service.resolver, service.reviews, policy, now=int(time.time()))
    return await _respond(message, COUNTS, work, unavailable="counts_unavailable")


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


# --- permissions_v2_ownership (A2A-3 §7.4) -------------------------------------------------------------------

#: ``ownership.Refused`` codes and their statuses (A2A-3 §7.4).
OWNERSHIP_STATUS = {"preview_stale": 409, "item_unknown": 400, "receipt_unknown": 400, "receipt_revoked": 409}


def _canonical(owner: _Owner, operation: Callable, *, write: bool):
    """``operation(conn)`` on the served canonical database: a write under the node write gate, in one immediate
    transaction (as the owner socket's capture routes write); a read in one read transaction, ungated."""
    from contextlib import nullcontext
    from ...storage.db.write_gate import with_db_write
    path = Path(owner.runtime.protocol.canonical_database)
    with with_db_write() if write else nullcontext():
        conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            result = operation(conn)
            if write:
                conn.commit()
            else:
                conn.rollback()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


@handles("permissions_v2_ownership", owner_only=True)
async def handle_permissions_v2_ownership(message):
    """Owner-only: list what counts as the owner's, confirm "mine" or "not mine" on an entry, withdraw a receipt."""
    def work():
        from ...permissions_v2 import ownership
        owner = _owner_gate(message, bound_only=True, unavailable="ownership_unavailable")
        payload = _keys(owner.payload, {"binding", "operation", "request"}, {"binding", "operation", "request"})
        operation, owner_id = payload["operation"], owner.identity.owner_id
        resource_id = owner.identity.resource_id
        try:
            if operation == "list":
                _keys(payload["request"], set())
                return _canonical(owner, lambda conn: ownership.listing(conn, owner_id=owner_id,
                                                                        resource_id=resource_id), write=False)
            if operation == "confirm":
                fields = {"item_type", "item_id", "decision", "preview_digest"}
                request = _keys(payload["request"], fields, fields)
                digest_value = request["preview_digest"]
                if (request["item_type"] not in ("app", "older") or not _identifier(request["item_id"])
                        or request["decision"] not in ("mine", "not_mine")
                        or not (digest_value is None or (isinstance(digest_value, str) and _HASH.fullmatch(digest_value)))):
                    raise _Refused(400, "payload_invalid")
                return _canonical(owner, lambda conn: ownership.confirm(
                    conn, owner_id=owner_id, resource_id=resource_id, item_type=request["item_type"],
                    item_id=request["item_id"], decision=request["decision"], preview_digest=digest_value),
                    write=True)
            if operation == "withdraw":
                request = _keys(payload["request"], {"receipt_id"}, {"receipt_id"})
                if not _identifier(request["receipt_id"]):
                    raise _Refused(400, "payload_invalid")
                return _canonical(owner, lambda conn: ownership.withdraw(
                    conn, owner_id=owner_id, resource_id=resource_id, receipt_id=request["receipt_id"]), write=True)
        except ownership.Refused as refused:
            raise _Refused(OWNERSHIP_STATUS.get(refused.code, 400), refused.code) from None
        raise _Refused(400, "payload_invalid")
    return await _respond(message, OWNERSHIP, work, unavailable="ownership_unavailable")


# --- permissions_v2_checking_model (A2A-3 §7.5; N5) ------------------------------------------------------------

@handles("permissions_v2_checking_model", owner_only=True)
async def handle_permissions_v2_checking_model(message):
    """Owner-only: the checking model's status; ``download`` with ``confirm: true`` starts it after the owner's yes."""
    def work():
        from ...permissions_v2 import checking_model
        owner = _owner_gate(message, bound_only=False, unavailable="checking_model_unavailable")
        payload = _keys(owner.payload, {"binding", "operation", "request"}, {"operation", "request"})
        operation = payload["operation"]
        if operation == "status":
            _keys(payload["request"], set())
            return checking_model.status()
        if operation == "download":
            request = _keys(payload["request"], {"confirm"}, {"confirm"})
            if request["confirm"] is not True:
                raise _Refused(400, "payload_invalid")
            try:
                return checking_model.download(served=owner.served)
            except checking_model.Refused as refused:
                raise _Refused(409, refused.code) from None
        raise _Refused(400, "payload_invalid")
    return await _respond(message, CHECKING_MODEL, work, unavailable="checking_model_unavailable")
