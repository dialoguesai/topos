"""Dedicated, bounded WebSocket dispatch for signed answer submit and fetch."""
from __future__ import annotations

import asyncio
import threading
import logging

from topos.principal import THIRD_PARTY, reset_principal, set_principal
from topos.relay_stamp import verify_relay_stamp

from . import kept_stamp, owner_account_words
from .answer_protocol import ACCEPTS_FIELD, LEGACY_ACCEPTED_VERSION, parse_accepted_version
from .answer_release import AnswerBusy, AnswerDeferred
from .canonical import PolicyError, canonical_bytes
from .runtime import get_runtime

SUBMIT_TYPE = "permissions_v2_answer_submit"
FETCH_TYPE = "permissions_v2_answer_fetch"
#: How long this dispatch waits for the service before answering the uniform refusal. The work itself is not
#: cancelled (a thread cannot be): a fetch still running then hands its body to a reply nobody reads.
WAIT_SECONDS = 15

#: Lane 190: the fetches whose store read is still running on a thread, keyed by the asker and the answer id. A second
#: fetch for the same answer while the first still waits behind the node's write gate is answered busy at once, with no
#: second thread: the app polls every few seconds for up to twelve minutes, and each wait-out would otherwise leave
#: one more thread blocked on the gate (the node's shared thread pool is small). The key is dropped when the thread ends.
_FETCHES_WAITING: set = set()
_FETCHES_LOCK = threading.Lock()


def _fetch_waiting_key(principal, payload):
    intent = payload.get("intent")
    answer_id = intent.get("answer_id") if isinstance(intent, dict) else None
    return (principal.acting_user, principal.client_id, str(answer_id or ""))
_log = logging.getLogger(__name__)


def _owner_account_words(message, kind, request_id) -> frozenset:
    """The share owner's account name words the control plane attested for this ask (A2A-4 amendment 10), or none.

    The field rides beside the principal stamp, outside the closed payload: the asker's intent has no room for it.
    Absent (a control plane from before the field): no words, the rule of 1.5.6. Present on a fetch, not exactly the
    grammar, or not verified under the pinned relay stamp key for this very message: the request is refused."""
    if owner_account_words.FIELD not in message:
        return frozenset()
    if kind != SUBMIT_TYPE:
        raise PolicyError("permission_denied")
    from topos import relay_stamp
    return owner_account_words.verify_stamp(message[owner_account_words.FIELD], msg_id=request_id, msg_type=kind,
                                            key=relay_stamp._load_public_key_bytes())


def _kept(message, kind, request_id):
    """`(keep_until, kq)` the control plane signed for a kept question (lane OFF1N, `kept_stamp`): the end of its keep
    window and its opaque id. None for a live ask.

    The field rides beside the principal stamp, outside the closed payload, as the owner's account words do. Absent (a
    live ask, or a control plane from before the field): None, and the ask is served exactly as before. Present on a
    fetch, not exactly the grammar, or not verified under the pinned relay stamp key for this very message: the
    request is refused."""
    if kept_stamp.FIELD not in message:
        return None
    if kind != SUBMIT_TYPE:
        raise PolicyError("permission_denied")
    from topos import relay_stamp
    return kept_stamp.verify_stamp(message[kept_stamp.FIELD], msg_id=request_id, msg_type=kind,
                                   key=relay_stamp._load_public_key_bytes())


def _accepted_version(message, kind) -> int:
    """The answer protocol version the asker's control plane says it accepts (A2A-4 amendment 13), from the ask frame.

    The field rides beside the principal stamp, outside the closed payload, and carries no authority: it can only
    narrow what the share's own search decision already released (the cap of records a body carries and, from answer
    protocol 4, the length of its answer: 30 and 32,000 at 4, 16 and 8,000 at 3), never widen it past the grant's
    `max_k`. Absent (a control plane from before the field): `LEGACY_ACCEPTED_VERSION`, and the ask is served exactly
    as 1.5.9 served it. Present on a fetch, or not exactly the grammar: the request is refused."""
    if ACCEPTS_FIELD not in message:
        return LEGACY_ACCEPTED_VERSION
    if kind != SUBMIT_TYPE:
        raise PolicyError("permission_denied")
    return parse_accepted_version(message[ACCEPTS_FIELD])


async def dispatch_answer(ws, message):
    request_id = message.get("id")
    try:
        kind = message.get("type")
        if kind not in (SUBMIT_TYPE, FETCH_TYPE) or not isinstance(request_id, str):
            raise PolicyError("permission_denied")
        principal = verify_relay_stamp(message)
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
                or not principal.acting_user or not principal.client_id):
            raise PolicyError("permission_denied")
        payload = message.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"envelope", "intent"}:
            raise PolicyError("permission_denied")
        account_words = _owner_account_words(message, kind, request_id)
        accepted_version = _accepted_version(message, kind)
        kept_ask = _kept(message, kind, request_id)
        waiting_key = None
        if kind == FETCH_TYPE:
            waiting_key = _fetch_waiting_key(principal, payload)
            with _FETCHES_LOCK:
                if waiting_key in _FETCHES_WAITING:
                    raise AnswerBusy()
                _FETCHES_WAITING.add(waiting_key)
        token = set_principal(principal)
        try:
            def work():
                try:
                    return _work()
                finally:
                    if waiting_key is not None:
                        with _FETCHES_LOCK:
                            _FETCHES_WAITING.discard(waiting_key)

            def _work():
                service = get_runtime().answers()
                if kind == SUBMIT_TYPE:
                    # A live ask reaches the service exactly as before; only a kept one carries its window and id.
                    kept = {} if kept_ask is None else {"keep_until": kept_ask[0], "kept_question": kept_ask[1]}
                    result, output = service.submit(envelope=payload["envelope"], payload=payload["intent"],
                                                    request_id=request_id, owner_account_words=account_words,
                                                    accepted_version=accepted_version, **kept)
                else:
                    result, output = service.fetch(envelope=payload["envelope"], payload=payload["intent"],
                                                   request_id=request_id)
                selective_answer = False
                if kind == FETCH_TYPE:
                    from .signing import parse_envelope
                    _authority, policy = service._current(parse_envelope(payload["envelope"]))
                    selective_answer = getattr(policy.search, "selective_facts", None) is not None
                return result, output, selective_answer

            try:
                result, output, selective_answer = await asyncio.wait_for(asyncio.to_thread(work), timeout=WAIT_SECONDS)
            except asyncio.TimeoutError:
                # BL-159, counts-only: which door, never an id. The service's own line follows with its seconds.
                _log.warning("permissions answer relay: no result within %d s (%s)", WAIT_SECONDS,
                             "fetch" if kind == FETCH_TYPE else "ask")
                # Lane 190 (the 21 Oct gate, Q7): a FETCH that ran out of this wait is not a refusal. The answer is
                # finished or still being written, and the fetch's own store read was waiting behind the node's
                # write gate. Answered as a refusal, the control plane forgot the held answer and the app ended the
                # turn with "didn't answer this one" while the node had answered. So a fetch's wait running out is
                # `answer_busy` (429): the control plane keeps the id and the app keeps polling. An ASK's wait
                # running out stays a refusal: a retried ask would be a second job.
                if kind == FETCH_TYPE:
                    raise AnswerBusy() from None
                raise
        finally:
            reset_principal(token)
        from .selective_facts import require_active_output
        from . import switches
        require_active_output(output, enabled=switches.on(switches.SELECTIVE_FACTS), selective_answer=selective_answer)
        await ws.send(canonical_bytes({"id": request_id, "type": kind, "status": "ok",
                                       "payload": {"result": result, "output": output}}).decode("ascii"))
    except AnswerDeferred:
        # Lane OFF1N (WS-OFF, 23 Oct): a kept ask that wrote nothing, its share's index not served. The frame of a
        # busy fetch with its own word: `answer_busy` means still being written (keep polling), `answer_deferred`
        # means this delivery is over (the control plane asks the kept question again later).
        from . import search_timing
        search_timing.refusal("answers", PolicyError("answer_deferred"))
        await ws.send(canonical_bytes({"id": request_id, "type": message.get("type"), "status": "error",
                                       "code": 429, "error": "answer_deferred"}).decode("ascii"))
    except AnswerBusy:
        from . import search_timing
        search_timing.refusal("answers", PolicyError("answer_busy"))   # the node's own log, with attribution on
        await ws.send(canonical_bytes({"id": request_id, "type": message.get("type"), "status": "error",
                                       "code": 429, "error": "answer_busy"}).decode("ascii"))
    except Exception as refused:
        from . import search_timing
        search_timing.refusal("answers", refused)
        await ws.send(canonical_bytes({"id": request_id, "type": message.get("type"), "status": "error",
                                       "code": 403, "error": "permission_denied"}).decode("ascii"))
