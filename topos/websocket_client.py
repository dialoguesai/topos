from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("topos.websocket_client")


class CloudWebSocketClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        await asyncio.sleep(0)
