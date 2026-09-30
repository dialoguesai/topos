"""Dedicated bounded WebSocket dispatch for p2c-v1 search.

Same doors as the locator transport (release_transport.py): a feature flag, the
CP relay stamp of a THIRD_PARTY recipient, a payload of exactly
{envelope, intent}, request-id binding, one uniform error frame. One deliberate
difference (design §7 R12): the adapter checkpoints and signs, returns, and only
then is the frame sent, so no node gate is held through the network write.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

from topos.principal import THIRD_PARTY, reset_principal, set_principal
from topos.relay_stamp import verify_relay_stamp

from . import search_timing
from .canonical import PolicyError, canonical_bytes
from .runtime import get_runtime
from .search_contract import CAPABILITY_SEARCH, DIRECT_SEARCH_CAPABILITIES
from .search_release import MAX_BATCH_ITEMS, parse_search_envelope
from .signing import AuthorityBinding, parse_authority, verify_current_signature

MESSAGE_TYPE = "permissions_v2_message_search"
SEND_TIMEOUT_SECONDS = 5
FLAG = "TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED"
# Batched search (OD-36, design §3.2-3.4). Subordinate to FLAG: both must be on.
BATCH_MESSAGE_TYPE = "permissions_v2_message_search_batch"
BATCH_FLAG = "TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_BATCH_ENABLED"
#: What the node advertises as `permissions_v2_search_batch_version` in its heartbeat capabilities.
BATCH_VERSION = 1
BATCH_CAPABILITIES = frozenset({CAPABILITY_SEARCH, *DIRECT_SEARCH_CAPABILITIES})
#: The longest a batch waits for its grant's lock when its `respond_by` would allow longer.
BATCH_LOCK_MAX_WAIT_SECONDS = 60


def _enabled() -> bool:
    return os.environ.get(FLAG, "").lower() == "true"


def _batch_enabled() -> bool:
    return _enabled() and os.environ.get(BATCH_FLAG, "").lower() == "true"


def batch_capability_version() -> int:
    """The heartbeat's `permissions_v2_search_batch_version`: 1 when this node answers batch frames, else 0.

    The CP relays a batch natively only to a node advertising >= 1; otherwise it sends the batch's
    envelopes as ordinary single frames (compat mode), which every node already answers.
    """
    return BATCH_VERSION if _batch_enabled() else 0


async def dispatch_message_search(ws, message) -> None:
    request_id = message.get("id")
    cancelled = threading.Event()
    timing = search_timing.transport()  # does nothing unless TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS=true
    # This search's verified boundary and review digest, shared by its three stages (N3a); closed below.
    verification = []
    try:
        if not _enabled() or message.get("type") != MESSAGE_TYPE:
            raise PolicyError("message_search_disabled")
        principal = verify_relay_stamp(message)
        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":
            raise PolicyError("recipient_relay_required")
        body = message.get("payload")
        if not isinstance(body, dict) or set(body) != {"envelope", "intent"}:
            raise PolicyError("release_payload_invalid")
        signed = parse_search_envelope(body["envelope"])
        if request_id != signed.request_id:
            raise PolicyError("request_binding")
        timing.bound(signed.request_id)

        def work():
            timing.started("adapter")
            token = set_principal(principal)
            try:
                with timing.active():
                    runtime = get_runtime()
                    adapter = runtime.message_search()
                    if cancelled.is_set():
                        raise PolicyError("release_cancelled_or_expired")
                    make = getattr(adapter, "verification", None)
                    verified = make() if make is not None else None
                    if verified is not None:
                        verification.append(verified)
                    return runtime, adapter, adapter.dispatch(envelope=body["envelope"], payload=body["intent"],
                                                              request_id=request_id, verified=verified)
            finally:
                reset_principal(token)
                timing.ended("adapter")

        timing.submitted("adapter")
        worker = asyncio.create_task(asyncio.to_thread(work))
        try:
            runtime, adapter, (result, output) = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise
        timing.resumed("adapter")
        # Every node gate is released here. The checkpoint already decided this send.
        def still_current():
            now = int(time.time())
            if (cancelled.is_set() or result["expires_at"] <= now or not _enabled() or get_runtime() is not runtime):
                raise PolicyError("release_cancelled_or_expired")
            verify_current_signature(signed, trusted_keys=runtime.protocol.ledger.trusted_keys, now=now)
            return now
        now = still_current()
        # Narrow the window the gate release opens (design §7 R12): a grant revoked, expired or
        # re-policied since the checkpoint no longer sends. A brief ledger read, then no gate.
        ledger = runtime.protocol.ledger

        def current_authority():
            timing.started("send_check")
            try:
                timing.asking()
                with ledger._transaction() as db:
                    timing.acquired("send_check")
                    # Protection first: the node's revision moves only on sync, so a black hole or
                    # tombstone committed after the checkpoint would otherwise be invisible here.
                    runtime.protocol._sync_protection(db)
                    timing.lap("protection")
                    authority = ledger._authority(db, signed.grant_id, now)[0]
                    timing.lap("authority")
                timing.lap("commit")
                # Observed aliases/contact/context edits need not advance the signed
                # protection clock. Check the private ranking basis again after the
                # checkpoint. Release the ledger before this check can take a node
                # gate to remove a stale index; preserve the established lock order.
                with timing.active():  # so the digest's own gate wait reports to this search (send_check_digest)
                    adapter.index.check_own(signed.grant_id, authority, now=now, digest_point="send_check_digest",
                                            verified=verification[0] if verification else None,
                                            laps=timing.check_own_laps(), provenance_point="send_check_provenance")
                timing.lap("check_own")
                return authority
            finally:
                timing.ended("send_check")
        timing.submitted("send_check")
        authority = await asyncio.to_thread(current_authority)
        timing.resumed("send_check")
        if authority.model_dump() != result["authority"]:
            raise PolicyError("authority_stale")
        frame = {"id": request_id, "type": MESSAGE_TYPE, "status": "ok", "payload": {"result": result, "output": output}}

        async def actual_send():
            # The read above is an await, so what only this transport can re-check (flag, key, clock,
            # runtime) is checked again here: in the task that invokes ws.send, with no await before the
            # write, as the fact and source doors do. A change made while the read was in flight refuses.
            still_current()
            await ws.send(canonical_bytes(frame).decode("ascii"))
        with timing.span("send"):
            await asyncio.wait_for(actual_send(), SEND_TIMEOUT_SECONDS)
        timing.finish("ok")
    except asyncio.CancelledError:
        raise
    except Exception:
        # Recipient errors reveal no fact existence, index state, review/protection
        # state, credential/config paths, query text or exception diagnostics.
        error = {"id": request_id, "type": MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}
        try:
            await asyncio.wait_for(ws.send(canonical_bytes(error).decode("ascii")), SEND_TIMEOUT_SECONDS)
        except Exception:
            pass
        timing.finish("error")
    finally:
        for verified in verification:
            verified.close()


# -- batched search (OD-36) -------------------------------------------------------------------------

_grant_locks: dict[str, threading.Lock] = {}
_grant_locks_guard = threading.Lock()


def _grant_lock(grant_id: str) -> threading.Lock:
    """Defence in depth (design §3.4): one batch in flight per grant on this node. The CP already sends at most one."""
    with _grant_locks_guard:
        lock = _grant_locks.get(grant_id)
        if lock is None:
            lock = _grant_locks[grant_id] = threading.Lock()
        return lock


def _batch_binding(batch_id, payload) -> tuple[list, list, int]:
    """Design §3.2's checks, before any work: shape, position binding, one shared authority, no duplicate.

    Returns (items for the adapter, the parsed envelopes, respond_by in epoch ms). Anything else raises.
    """
    if not isinstance(batch_id, str) or not isinstance(payload, dict) or set(payload) != {"items", "respond_by"}:
        raise PolicyError("release_payload_invalid")
    items, respond_by = payload["items"], payload["respond_by"]
    if (not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_ITEMS
            or not isinstance(respond_by, int) or isinstance(respond_by, bool) or respond_by <= 0):
        raise PolicyError("release_payload_invalid")
    adapter_items, envelopes = [], []
    for position, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"envelope", "intent"}:
            raise PolicyError("release_payload_invalid")
        signed = parse_search_envelope(item["envelope"])
        # Binds each signed envelope to this frame and to its place in it.
        if signed.request_id != f"{batch_id}:{position}":
            raise PolicyError("request_binding")
        if signed.capability_version not in BATCH_CAPABILITIES:
            raise PolicyError("unsupported_capability")
        envelopes.append(signed)
        adapter_items.append({"envelope": item["envelope"], "payload": item["intent"], "request_id": signed.request_id})
    first = envelopes[0]

    def shared(signed):
        return (parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields}).model_dump(),
                signed.kid, signed.issued_at, signed.expires_at)
    if any(shared(signed) != shared(first) for signed in envelopes[1:]):
        raise PolicyError("batch_binding")
    if len({signed.request_hash for signed in envelopes}) != len(envelopes):
        raise PolicyError("batch_binding")
    return adapter_items, envelopes, respond_by


async def dispatch_message_search_batch(ws, message) -> None:
    """One recipient batch (design §3.4): one verification pass, N ranked, walked, receipted and signed queries.

    The same doors as a single search -- flag, THIRD_PARTY relay stamp, closed payload, request binding
    (here, positional), one uniform error frame -- then one adapter call under one SearchVerification,
    one send-time check and one frame. A batch is answered whole or refused whole: there is no
    per-item refusal, so nothing in a refused frame says which query, or how many, would have failed.
    """
    batch_id = message.get("id")
    cancelled = threading.Event()
    timing = search_timing.transport()
    verification = []
    held = []
    try:
        if not _batch_enabled() or message.get("type") != BATCH_MESSAGE_TYPE:
            raise PolicyError("message_search_disabled")
        principal = verify_relay_stamp(message)
        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":
            raise PolicyError("recipient_relay_required")
        items, envelopes, respond_by = _batch_binding(batch_id, message.get("payload"))
        timing.bound(batch_id, n=len(items))
        grant_lock = _grant_lock(envelopes[0].grant_id)

        def past_deadline() -> bool:
            # Advisory: the CP stops waiting at respond_by. Never an authority bound (expires_at is).
            return time.time() * 1000 >= respond_by

        def work():
            timing.started("adapter")
            token = set_principal(principal)
            try:
                wait = min(BATCH_LOCK_MAX_WAIT_SECONDS, respond_by / 1000 - time.time())
                if wait <= 0 or not grant_lock.acquire(timeout=wait):
                    raise PolicyError("batch_in_flight")
                held.append(grant_lock)
                with timing.active():
                    runtime = get_runtime()
                    adapter = runtime.message_search()
                    if cancelled.is_set() or past_deadline():
                        raise PolicyError("release_cancelled_or_expired")
                    verified = adapter.verification()
                    verification.append(verified)
                    return runtime, adapter, adapter.dispatch_batch(items=items, verified=verified,
                                                                    past_deadline=past_deadline)
            finally:
                reset_principal(token)
                timing.ended("adapter")

        timing.submitted("adapter")
        worker = asyncio.create_task(asyncio.to_thread(work))
        try:
            runtime, adapter, answered = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise
        timing.resumed("adapter")
        if len(answered) != len(envelopes):
            raise PolicyError("batch_binding")

        def still_current():
            now = int(time.time())
            if cancelled.is_set() or not _batch_enabled() or get_runtime() is not runtime or past_deadline():
                raise PolicyError("release_cancelled_or_expired")
            for signed, (result, _output) in zip(envelopes, answered):
                if result["expires_at"] <= now or result["request_id"] != signed.request_id:
                    raise PolicyError("release_cancelled_or_expired")
                verify_current_signature(signed, trusted_keys=runtime.protocol.ledger.trusted_keys, now=now)
            return now
        now = still_current()
        ledger = runtime.protocol.ledger
        grant_id = envelopes[0].grant_id

        def current_authority():
            # Once per batch, exactly the single search's send check: protection, authority, check_own.
            timing.started("send_check")
            try:
                timing.asking()
                with ledger._transaction() as db:
                    timing.acquired("send_check")
                    runtime.protocol._sync_protection(db)
                    timing.lap("protection")
                    authority = ledger._authority(db, grant_id, now)[0]
                    timing.lap("authority")
                timing.lap("commit")
                with timing.active():
                    adapter.index.check_own(grant_id, authority, now=now, digest_point="send_check_digest",
                                            verified=verification[0], laps=timing.check_own_laps(),
                                            provenance_point="send_check_provenance")
                timing.lap("check_own")
                return authority
            finally:
                timing.ended("send_check")
        timing.submitted("send_check")
        authority = await asyncio.to_thread(current_authority)
        timing.resumed("send_check")
        expected = authority.model_dump()
        if any(result["authority"] != expected for result, _output in answered):
            raise PolicyError("authority_stale")
        frame = {"id": batch_id, "type": BATCH_MESSAGE_TYPE, "status": "ok",
                 "payload": {"items": [{"result": result, "output": output} for result, output in answered]}}

        async def actual_send():
            still_current()
            await ws.send(canonical_bytes(frame).decode("ascii"))
        with timing.span("send"):
            await asyncio.wait_for(actual_send(), SEND_TIMEOUT_SECONDS)
        timing.finish("ok")
    except asyncio.CancelledError:
        raise
    except Exception:
        # One frame for every refusal: no item, count, class, index or review state is named.
        error = {"id": batch_id, "type": BATCH_MESSAGE_TYPE, "status": "error", "code": 403,
                 "error": "permission_denied"}
        try:
            await asyncio.wait_for(ws.send(canonical_bytes(error).decode("ascii")), SEND_TIMEOUT_SECONDS)
        except Exception:
            pass
        timing.finish("error")
    finally:
        for verified in verification:
            verified.close()
        for lock in held:
            lock.release()
