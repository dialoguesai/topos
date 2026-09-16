"""Verified owner identity attestation dispatch, shared by HTTP and the CP relay.

A read never enrolls, never installs and never consumes a command. A write
consumes exactly one command, through the ledger's own uniqueness constraint,
and is acknowledged by a node-signed ack so the control plane can prove what the
node did rather than being told.
"""
from __future__ import annotations

import asyncio
import time

from .canonical import PolicyError, digest
from .identity_protocol import (IdentityAckBody, command_digest, sign_identity_ack, verify_identity_command)


async def execute_signed_identity_command(raw, *, principal):
    from .runtime import get_runtime
    from topos.principal import OWNER_APP, Principal, set_principal, reset_principal
    from topos.storage.db.write_gate import with_db_write

    if principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}:
        raise PolicyError("owner_authority_required")
    runtime = get_runtime()
    protocol = runtime.protocol
    identity = protocol.ledger.identity
    if principal.channel == "cp_relay" and (
        principal.acting_user != identity.owner_id or principal.client_id != protocol.frontend_client_id
    ):
        raise PolicyError("owner_authority_required")

    def verify():
        return verify_identity_command(raw, trusted_keys=protocol.trusted_cp_keys,
            issuer_id=protocol.cp_issuer_id, identity=identity,
            frontend_client_id=protocol.frontend_client_id, now=int(time.time()))

    command = verify()
    token = set_principal(Principal(cls=OWNER_APP, channel=principal.channel,
        client_id=command.owner_authorization.client_id, acting_user=identity.owner_id))
    try:
        def apply():
            with with_db_write():
                verify()  # Clock and key validity again, after the writer gate.
                service = runtime.identity_attestations()
                request = command.request
                if request.operation == "describe":
                    return service.describe(request)
                method = service.attest if request.operation == "attest" else service.revoke
                return method(request, command_id=command.command_id, command_hash=command_digest(command))

        error, result = None, None
        try:
            result = await asyncio.to_thread(apply)
        except PolicyError as exc:
            error = exc.code
        except Exception:
            error = "identity_state_unavailable"
        now = int(time.time())
        return sign_identity_ack(IdentityAckBody.parse({
            "version": "topos-owner-identity-ack/v1", "kid": protocol.node_signing_kid,
            "issuer_id": identity.node_id, "audience_id": protocol.cp_issuer_id,
            "command_id": command.command_id, "response_to": digest(command.model_dump()),
            "binding": identity.model_dump(), "operation": command.request.operation,
            "result": None if result is None else result.model_dump(), "error_code": error,
            "issued_at": now, "expires_at": now + 120,
        }), protocol.node_signing_key)
    finally:
        reset_principal(token)
