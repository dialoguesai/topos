"""Signed truth doors for a free statement (``effort: high``): read by the node's interpreter, then checked or shown.

The control plane forwards a statement here only after its own grammar refused it, and only to a node that
advertises ``truth_interpret.CAPABILITY_FIELD``. Two request types (``truth_contract``), payload
``{"pack_id", "statement"}``:

- ``VERIFY_STATEMENT``: read the statement, then answer exactly as ``permissions.v2.verify_claim`` answers the claim
  it was read as, and sign the claim with the verdict (``TruthStatementResult.interpreted``), so what was checked is
  part of the node's signed bytes rather than something the control plane adds.
- ``INTERPRET`` (the confirm step): read the statement and sign only what it was read as, the question's other
  answers and an ``interpretation_id``. No owner answer is read. The game shows the reading; the player confirms or
  corrects it with the structured ``permissions.v2.verify_claim``, which may carry the id
  (``truth_contract.ConfirmedTruthIntent``) so the owner's receipt says confirmed, corrected or unlinked. The id is
  provenance, never authority: the structured check is one every consented grant can already make.

Order, for both: the envelope and its authority (no write); the grant's explicit Icebreakers consent; then, for a
statement the guards pass, the grant's hourly allowance and a model slot; the reading; and only then the label. An
unconsented grant never spends the owner's model time, and the reading never depends on the owner's answers.

Every exit spends the request id. A statement the interpreter did not read as one answer is ``NotUnderstood`` (a 400
``statement_not_understood``: nothing was checked, never an ``unknown`` verdict) and counts no daily question. The
model's state is never answered that way (DR-04, no new way for a recipient to tell the node is busy): a timeout or
an unavailable model is ``Unavailable`` (503 ``interpret_unavailable``) and a full queue ``truth_interpret.Busy``
(429 ``interpret_busy``), both of which the control plane answers as it answers a busy node; only the grant's own
spent allowance (429 ``interpret_allowance``) is about the recipient alone. Anything else is the uniform refusal. The
statement's text is never stored: the receipt binds the signed request hash and the codes it was read as.
"""
from __future__ import annotations

import asyncio
import collections
import secrets
import threading
import time

from topos.principal import THIRD_PARTY
from topos.relay_stamp import verify_relay_stamp
from topos.storage.db.write_gate import with_db_write
from . import switches, truth_interpret
from .canonical import PolicyError, canonical_bytes, digest
from .forwarding import ReleaseBody, sign_node_result
from .runtime import get_runtime
from .signing import TruthRequestContext, parse_envelope, verify_current_signature
from .truth_contract import (INTERPRET, INTERPRETATION_TTL_SECONDS, PACK, VERIFY_STATEMENT, Alternative,
                             InterpretationResult, Interpreted, Proposition, TruthIntent, TruthStatement,
                             TruthStatementResult, authorize_truth)

STATEMENT_MESSAGE_TYPE = "permissions_v2_verify_claim_statement"
INTERPRET_MESSAGE_TYPE = "permissions_v2_interpret_claim"
REQUEST_TYPES = {STATEMENT_MESSAGE_TYPE: VERIFY_STATEMENT, INTERPRET_MESSAGE_TYPE: INTERPRET}
#: How long the dispatch waits for its thread: the queue wait, both model calls and the label read.
WAIT_SECONDS = truth_interpret.QUEUE_WAIT_SECONDS + truth_interpret.DEADLINE_SECONDS + 5
SEND_TIMEOUT_SECONDS = 5
#: Interpretations remembered for their confirm link, newest last; the oldest are forgotten past this.
SHOWN_MAX = 4096


class NotUnderstood(Exception):
    """The statement was not read as one answer. `reason` is the operator's and the owner's, never the recipient's."""

    def __init__(self, reading):
        super().__init__(reading.reason)
        self.reading = reading
        self.reason = reading.reason


class Unavailable(NotUnderstood):
    """The model could not be asked or did not finish (`Reading.operational`): an operational refusal that says
    nothing about the statement, answered like a busy node, never as "not understood" (DR-04)."""


def refusal(reading) -> NotUnderstood:
    return Unavailable(reading) if reading.operational else NotUnderstood(reading)


class _Shown:
    """Interpretations this process showed, by id, for `provenance`. Lost on restart: the link is then "unlinked"."""

    def __init__(self):
        self._rows = collections.OrderedDict()
        self._lock = threading.Lock()

    def add(self, interpretation_id, *, grant_id, question_id, value, expires_at):
        with self._lock:
            self._rows[interpretation_id] = (grant_id, question_id, value, expires_at)
            while len(self._rows) > SHOWN_MAX:
                self._rows.popitem(last=False)

    def take(self, interpretation_id, *, grant_id, intent, now) -> str:
        """confirmed, corrected or unlinked. One shown reading links at most one check."""
        with self._lock:
            row = self._rows.pop(interpretation_id, None)
        if row is None or row[0] != grant_id or row[3] <= now or row[1] != intent.question_id:
            return "unlinked"
        return "confirmed" if row[2] == intent.value else "corrected"


SHOWN = _Shown()


def provenance(intent, *, grant_id, now) -> str | None:
    """For `truth_release.dispatch`: None for a three-key claim, otherwise how it relates to a reading shown here."""
    interpretation_id = getattr(intent, "interpretation_id", None)
    if interpretation_id is None:
        return None
    return SHOWN.take(interpretation_id, grant_id=grant_id, intent=intent, now=now)


def _read(statement: str, grant_id: str | None, *, read=None):
    """The reading, through the grant's allowance and the node's model slot (`truth_interpret.read`)."""
    read = read or truth_interpret.read
    if grant_id is not None and truth_interpret.guard(statement) is None:
        truth_interpret.ALLOWANCE.take(grant_id)  # a statement the guards refuse costs no model time and no allowance
    return read(statement)


def _sign(runtime, signed, current, output, checked_at):
    return sign_node_result(ReleaseBody(version="topos-node-disclosure/v1", kid=runtime.protocol.node_signing_kid,
        envelope_hash=digest(signed.model_dump()), request_id=signed.request_id, request_hash=signed.request_hash,
        authority=current, output_hash=digest(output.model_dump()), checked_at=checked_at,
        expires_at=signed.expires_at), runtime.protocol.node_signing_key).model_dump()


def dispatch(runtime, principal, envelope, payload, request_id, *, request_type, read=None):
    """(result, output, revision) for one signed statement request. Raises NotUnderstood, Busy or PolicyError."""
    signed = parse_envelope(envelope)
    if request_type not in (VERIFY_STATEMENT, INTERPRET) or signed.request_type != request_type or signed.request_id != request_id:
        raise PolicyError("request_binding")
    if principal.cls != THIRD_PARTY or principal.channel != "cp_relay" or not principal.acting_user or not principal.client_id:
        raise PolicyError("recipient_relay_required")
    statement = TruthStatement.parse(payload)
    ledger = runtime.protocol.ledger
    request = TruthRequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
        "client_id": principal.client_id, "grant_id": signed.grant_id, "assignment_id": signed.assignment_id,
        "request_id": request_id, "request_type": request_type})
    with with_db_write():
        with ledger._transaction() as db:
            runtime.protocol._sync_protection(db)
        admission = ledger.verify(signed.model_dump(), request=request, payload=statement.model_dump(), now=int(time.time()))
    try:
        with with_db_write():
            with ledger._transaction() as db:
                _current, policy = ledger._authority(db, signed.grant_id, int(time.time()))
                authorize_truth(policy, statement)
        # Outside the write gate: the model takes seconds and writes nothing.
        reading = _read(statement.statement, signed.grant_id, read=read)
        if not reading.accepted:
            raise refusal(reading)
        if request_type == INTERPRET:
            return _interpret(runtime, signed, admission, reading)
        intent = TruthIntent.parse({"pack_id": statement.pack_id, "question_id": reading.question_id,
                                    "value": reading.value})
        from .truth_release import read as read_label
        with with_db_write():
            lease = ledger.admit_verified(admission, now=int(time.time()))
            verdict, revision = read_label(runtime, intent)
            output = TruthStatementResult.parse({**verdict.model_dump(), "interpreted": Interpreted.parse(
                {"question_id": intent.question_id, "value": intent.value, "path": "high",
                 "interpreter": truth_interpret.VERSION}).model_dump()})
            current = ledger.checkpoint_truth_statement(lease, statement=statement, intent=intent, output=output,
                                                        revision=revision, now=int(time.time()))
        checked_at = int(time.time())
        verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
        return _sign(runtime, signed, current, output, checked_at), output.model_dump(), revision
    except BaseException:
        with with_db_write():
            ledger.refuse(admission, now=int(time.time()))  # a no-op once the id is claimed
        raise


def _interpret(runtime, signed, admission, reading):
    ledger = runtime.protocol.ledger
    now = int(time.time())
    output = InterpretationResult.parse({
        "interpretation": Proposition.parse(reading.interpretation()).model_dump(),
        "alternatives": [Alternative.parse(a).model_dump() for a in reading.alternatives()],
        "interpretation_id": secrets.token_urlsafe(16), "expires_at": now + INTERPRETATION_TTL_SECONDS,
        "interpreter": truth_interpret.VERSION})
    with with_db_write():
        current = ledger.record_interpretation(admission, output=output, now=now)
    SHOWN.add(output.interpretation_id, grant_id=signed.grant_id, question_id=reading.question_id,
              value=reading.value, expires_at=output.expires_at)
    checked_at = int(time.time())
    verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
    return _sign(runtime, signed, current, output, checked_at), output.model_dump(), None


def enabled() -> bool:
    return switches.on(switches.MESSAGE_SEARCH) and switches.on(switches.TRUTH_INTERPRET)


def capability_version() -> int:
    """`truth_interpret.CAPABILITY_FIELD` in the heartbeat: 1 while the doors are on and the interpreter can run."""
    return truth_interpret.CAPABILITY_VERSION if enabled() and truth_interpret.available() else 0


async def dispatch_statement(ws, message):
    request_id, kind = message.get("id"), message.get("type")
    try:
        if kind not in REQUEST_TYPES or not enabled():
            raise PolicyError("truth_disabled")
        principal = verify_relay_stamp(message)
        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":
            raise PolicyError("recipient_relay_required")
        body = message.get("payload")
        if not isinstance(body, dict) or set(body) != {"envelope", "intent"}:
            raise PolicyError("release_payload_invalid")
        runtime = get_runtime()
        signed = parse_envelope(body["envelope"])
        request_type = REQUEST_TYPES[kind]
        result, output, revision = await asyncio.wait_for(asyncio.to_thread(
            dispatch, runtime, principal, body["envelope"], body["intent"], request_id, request_type=request_type),
            WAIT_SECONDS)

        def current():
            # As truth_release: the authority, consent and (for a verdict) the label are re-read before sending.
            from .truth_release import read as read_label
            now = int(time.time())
            with with_db_write():
                ledger = runtime.protocol.ledger
                with ledger._transaction() as db:
                    runtime.protocol._sync_protection(db)
                    authority, policy = ledger._authority(db, signed.grant_id, now)
                    authorize_truth(policy, TruthStatement.parse(body["intent"]))
                if request_type == VERIFY_STATEMENT:
                    read = output["interpreted"]
                    value, latest = read_label(runtime, TruthIntent.parse(
                        {"pack_id": PACK, "question_id": read["question_id"], "value": read["value"]}))
                    if latest != revision or {**value.model_dump(), "interpreted": read} != output:
                        raise PolicyError("authority_stale")
                if authority.model_dump() != result["authority"]:
                    raise PolicyError("authority_stale")
                verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=now)
        await asyncio.to_thread(current)

        async def send():
            if get_runtime() is not runtime or not enabled() or result["expires_at"] <= int(time.time()):
                raise PolicyError("authority_stale")
            verify_current_signature(signed, trusted_keys=runtime.protocol.ledger.trusted_keys, now=int(time.time()))
            await ws.send(canonical_bytes({"id": request_id, "type": kind, "status": "ok",
                                           "payload": {"result": result, "output": output}}).decode("ascii"))
        await asyncio.wait_for(send(), SEND_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Unavailable as refused:
        # The model's state, not the statement: the control plane answers it as it answers a busy node (DR-02, DR-04).
        await asyncio.wait_for(ws.send(canonical_bytes({"id": request_id, "type": kind, "status": "error",
            "code": 503, "error": "interpret_unavailable",
            "refusal_class": "interpret_" + str(refused.reason)}).decode("ascii")), SEND_TIMEOUT_SECONDS)
    except NotUnderstood as refused:
        # Decided by the statement alone, after consent: the recipient's control plane answers it as its grammar's
        # own "not understood", and keeps the class for its operator.
        await asyncio.wait_for(ws.send(canonical_bytes({"id": request_id, "type": kind, "status": "error",
            "code": 400, "error": "statement_not_understood",
            "refusal_class": "statement_high_" + str(refused.reason)}).decode("ascii")), SEND_TIMEOUT_SECONDS)
    except truth_interpret.Busy as busy:
        await asyncio.wait_for(ws.send(canonical_bytes({"id": request_id, "type": kind, "status": "error",
            "code": 429, "error": busy.code}).decode("ascii")), SEND_TIMEOUT_SECONDS)
    except Exception:
        await asyncio.wait_for(ws.send(canonical_bytes({"id": request_id, "type": kind, "status": "error",
            "code": 403, "error": "permission_denied"}).decode("ascii")), SEND_TIMEOUT_SECONDS)


def preview(conn, payload: dict) -> dict:
    """The owner's preview of a statement (`calibration_preview` with `statement`, `effort: "high"`): what it was
    read as, or which stage refused it and why, and with `confirm: "required"` no verdict. Owner only (the handler
    checks), the node's model slot (Busy when full), no grant allowance."""
    from ..features.calibration.store import verify
    if not enabled():
        raise PolicyError("truth_interpret_disabled")
    if (not set(payload) <= {"statement", "effort", "confirm", "pack_id"} or payload.get("effort") != "high"
            or payload.get("confirm", "none") not in ("none", "required")):
        raise PolicyError("malformed_request")
    statement = TruthStatement.parse({"pack_id": payload.get("pack_id", PACK), "statement": payload.get("statement")})
    reading = _read(statement.statement, None)
    diagnostics = reading.diagnostics()
    if not reading.accepted:
        return {"operation": "interpret_claim", "status": "unavailable" if reading.operational else "not_understood",
                "diagnostics": diagnostics}
    if payload.get("confirm") == "required":
        return {"operation": "interpret_claim", "status": "needs_confirmation",
                "interpretation": reading.interpretation(), "alternatives": reading.alternatives(),
                "diagnostics": diagnostics}
    intent = TruthIntent.parse({"pack_id": statement.pack_id, "question_id": reading.question_id, "value": reading.value})
    verdict, _revision = verify(conn, intent)
    return {**verdict.model_dump(), "interpreted": {"question_id": reading.question_id, "value": reading.value,
            "path": "high", "interpreter": truth_interpret.VERSION}, "interpretation": reading.interpretation(),
            "diagnostics": diagnostics}
