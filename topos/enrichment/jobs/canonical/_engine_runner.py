"""Shared Engine invocation helpers for canonical/signal jobs."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict, Optional, Protocol

from ....engine.tasks import ModelRequest, ProcessingTask, RequestedBy


class _EngineRunner(Protocol):
    def run(self, task: ProcessingTask) -> Any: ...


async def run_engine_task(
    engine: _EngineRunner,
    *,
    task_id: str,
    subtype: str,
    source_id: Optional[str],
    record_ids: list[str],
    input_payload: Dict[str, Any],
    provider: str = "huggingface",
    model: Optional[str] = None,
    #: The model-pack role `model` was resolved from, or None for a job that is
    #: not on a pack. Named by the caller because only it knows which role it
    #: asked for — this helper is shared by jobs that sit on different roles.
    pack_role: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Any:
    if model is None:
        # Device-level per-job override (set from the Enrichment Lab's
        # "apply preferred"). Explicitly requested models always win.
        try:
            from ...model_overrides import get_override_for_subtype

            override = get_override_for_subtype(subtype)
            if override:
                provider = override.get("provider") or provider
                model = override.get("model")
        except Exception:  # noqa: BLE001 — never block enrichment on overrides
            pass
    # Resolved after the device override, against whichever model actually runs:
    # the pack's knobs were set on ITS model, and carrying them onto an
    # overridden one would run that model at settings its owner never chose.
    binding = None
    if pack_role:
        from ....config.signal_extraction import resolve_signal_extraction_pack_params

        binding = resolve_signal_extraction_pack_params(conn, model, role=pack_role)
    task = ProcessingTask(
        id=task_id,
        type="enrichment",
        subtype=subtype,
        source_id=source_id,
        record_ids=record_ids,
        input=input_payload,
        model_request=ModelRequest(
            provider=provider,
            model=model,
            thinking=binding.thinking if binding else None,
            context=binding.context if binding else None,
            max_tokens=binding.max_tokens if binding else None,
        ),
        requested_by=RequestedBy(origin="ingestion_pipeline"),
    )
    return await asyncio.to_thread(engine.run, task)
