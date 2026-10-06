"""The data relay requires the dedicated verified WebSocket dispatch gate."""
from .registry import handles


@handles("permissions_v2_message_search")
async def handle_permissions_v2_message_search(message):
    # p2c-v3 search is answered only by its bounded relay dispatch; the generic
    # handler path can never return a payload for deferred forwarding.
    return {"id": message.get("id"), "status": "error", "code": 403, "error": "permission_denied"}


@handles("permissions_v2_message_search_batch")
async def handle_permissions_v2_message_search_batch(message):
    # Batched search, likewise: only its bounded relay dispatch answers it.
    return {"id": message.get("id"), "status": "error", "code": 403, "error": "permission_denied"}


@handles("permissions_v2_answer_submit")
async def handle_permissions_v2_answer_submit(message):
    return {"id": message.get("id"), "status": "error", "code": 403, "error": "permission_denied"}


@handles("permissions_v2_answer_fetch")
async def handle_permissions_v2_answer_fetch(message):
    return {"id": message.get("id"), "status": "error", "code": 403, "error": "permission_denied"}
