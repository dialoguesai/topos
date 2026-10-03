"""``permissions_v2_bind``: the control plane asks the owner's node to bind itself for sharing (A2A-1 §3.4, §4.2).

Owner-only: the dispatcher answers ``owner_mode_required`` unless the relay stamp verified as the owner's app
with the key this node pinned (§4.2 step 0). Everything after that is the node's half, in
``topos/permissions_v2/self_bind.py``. It runs on a worker thread because a bind backs the database up first,
and that must not hold the event loop.
"""
from __future__ import annotations

import asyncio

from .registry import handles


@handles("permissions_v2_bind", owner_only=True)
async def handle_permissions_v2_bind(message):
    from ...permissions_v2.self_bind import answer
    from ...principal import current_principal

    return await asyncio.to_thread(answer, message, current_principal())
