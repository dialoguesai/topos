"""The data relay requires the dedicated verified WebSocket dispatch gate."""
from .registry import handles


@handles("permissions_v2_source_read")
async def handle_permissions_v2_source_read(message):
    # Normal handler responses leave their authorization scope before the
    # generic transport sends them. Data may use only the bounded relay path.
    return {"id": message.get("id"), "status": "error", "code": 403, "error": "permission_denied"}


@handles("permissions_v2_message_search")
async def handle_permissions_v2_message_search(message):
    # p2c-v1 search is answered only by its bounded relay dispatch; the generic
    # handler path can never return a payload for deferred forwarding.
    return {"id": message.get("id"), "status": "error", "code": 403, "error": "permission_denied"}
