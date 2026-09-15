"""Default-disabled owner snapshot relay with mandatory payload-bound proof."""
from .registry import handles


@handles("permissions_v2_ingest_snapshot", owner_only=True)
async def handle_permissions_v2_ingest_snapshot(message):
    from topos.principal import current_principal
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.ingest_dispatch import execute_signed_ingest

    request_id = message.get("id")
    payload = message.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"envelope"}:
        return {"id": request_id, "status": "error", "code": 400, "error": "ingest_payload_invalid"}
    try:
        ack = await execute_signed_ingest(payload["envelope"], principal=current_principal())
        return {"id": request_id, "status": "ok", "payload": {"ack": ack.model_dump()}}
    except PolicyError as exc:
        disabled = exc.code in {"permissions_v2_disabled", "ingest_snapshots_disabled"}
        return {"id": request_id, "status": "error", "code": 404 if disabled else 403, "error": "ingest_disabled" if disabled else "ingest_authority_invalid"}
    except Exception:
        return {"id": request_id, "status": "error", "code": 503, "error": "ingest_unavailable"}
