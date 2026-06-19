"""Shared Engine invocation helpers for canonical/signal jobs."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ....engine import Engine
from ....engine.tasks import ModelRequest, ProcessingTask


async def run_engine_task(
    engine: Engine,
    *,
    task_id: str,
    subtype: str,
    source_id: Optional[str],
    record_ids: list[str],
    input_payload: Dict[str, Any],
    provider: str = "huggingface",
    model: Optional[str] = None,
) -> Any:
    task = ProcessingTask(
        id=task_id,
        type="enrichment",
        subtype=subtype,
        source_id=source_id,
        record_ids=record_ids,
        input=input_payload,
        model_request=ModelRequest(provider=provider, model=model),
    )
    return await asyncio.to_thread(engine.run, task)
