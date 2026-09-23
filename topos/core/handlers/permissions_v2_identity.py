"""Default-disabled owner identity attestation relay with payload-bound proof."""
from .registry import handles


@handles("permissions_v2_identity_command", owner_only=True)
async def handle_permissions_v2_identity_command(message):
    from topos.principal import current_principal
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.identity_dispatch import execute_signed_identity_command

    request_id = message.get("id")
    payload = message.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"envelope"}:
        return {"id": request_id, "status": "error", "code": 400, "error": "identity_payload_invalid"}
    try:
        ack = await execute_signed_identity_command(payload["envelope"], principal=current_principal())
        return {"id": request_id, "status": "ok", "payload": {"ack": ack.model_dump()}}
    except PolicyError as exc:
        disabled = exc.code in {"permissions_v2_disabled", "identity_attestations_disabled"}
        return {"id": request_id, "status": "error", "code": 404 if disabled else 403,
                "error": "identity_disabled" if disabled else "identity_authority_invalid"}
    except Exception:
        return {"id": request_id, "status": "error", "code": 503, "error": "identity_unavailable"}
