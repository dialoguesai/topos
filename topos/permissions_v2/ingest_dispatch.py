"""Verified owner snapshot dispatch shared by HTTP and the CP relay."""
from __future__ import annotations

import asyncio
import time

from .canonical import PolicyError, digest
from .ingest_protocol import CHATGPT_SOURCE_ID, IngestAckBody, command_digest, sign_ingest_ack, verify_ingest_command


async def execute_signed_ingest(raw, *, principal):
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
        return verify_ingest_command(raw, trusted_keys=protocol.trusted_cp_keys,
            issuer_id=protocol.cp_issuer_id, identity=identity,
            frontend_client_id=protocol.frontend_client_id, now=int(time.time()))

    # UDS supplies local owner mode; the signed command and persisted pairing
    # supply the exact account. Never derive it from the dataset or headers.
    command = verify()
    token = set_principal(Principal(cls=OWNER_APP, channel=principal.channel,
        client_id=command.owner_authorization.client_id, acting_user=identity.owner_id))
    try:
        def apply():
            with with_db_write():
                verify()  # Clock/key validity after waiting for the writer gate.
                service = runtime.ingestion()
                conn = runtime.ingestion_connection()
                try:
                    request = command.request
                    if request.operation not in {"describe", "status"}:
                        service.consume_command(conn, command_id=command.command_id,
                            command_hash=command_digest(command), allow_install=request.operation == "enroll")
                    if request.operation == "run":
                        return service, None
                    # Describe/enroll carry a non-default reader contract in the
                    # dump; the other operations name their lane by source id.
                    arguments = request.model_dump(exclude={"operation", "source_id"})
                    if request.operation not in {"describe", "enroll"} and request.source_id != "imessage":
                        arguments["source_id"] = request.source_id
                    method = "describe_snapshot" if request.operation == "describe" else request.operation
                    return service, getattr(service, method)(conn, **arguments)
                finally:
                    conn.close()

        error = None
        result = None
        try:
            service, result = await asyncio.to_thread(apply)
            if command.request.operation == "run":
                # The runner claims only its own lane's job; a mismatch fails the claim.
                if command.request.source_id == CHATGPT_SOURCE_ID:
                    from topos.ingestion.chatgpt_owner_snapshot import run_chatgpt_snapshot_job as runner
                else:
                    from topos.ingestion.owner_snapshot import run_snapshot_job as runner
                result = await runner(service, runtime.ingestion_connection, command.request.job_id)
        except PolicyError as exc:
            error = exc.code
        except Exception:
            error = "ingest_unavailable"
        now = int(time.time())
        return sign_ingest_ack(IngestAckBody.parse({
            "version": "topos-owner-ingest-ack/v2", "kid": protocol.node_signing_kid,
            "issuer_id": identity.node_id, "audience_id": protocol.cp_issuer_id,
            "command_id": command.command_id, "response_to": digest(command.model_dump()),
            "binding": identity.model_dump(), "operation": command.request.operation,
            "result": result, "error_code": error, "issued_at": now, "expires_at": now + 120,
        }), protocol.node_signing_key)
    finally:
        reset_principal(token)
