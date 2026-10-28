"""Shared Engine invocation helpers for canonical/signal jobs."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Protocol

from ....engine.tasks import ModelRequest, ProcessingTask, RequestedBy

logger = logging.getLogger("topos.enrichment.engine_runner")


class _EngineRunner(Protocol):
    def run(self, task: ProcessingTask) -> Any: ...


#: Canonical jobs whose model this module brings up ahead of their first batch, by the
#: subtype the job asks the engine for.
WARMED_JOB_SUBTYPES: Dict[str, str] = {
    "entities": "entity_extraction_batch",
    "embeddings": "embedding",
}


async def warm_model_for(engine: Any, task: ProcessingTask) -> None:
    """Bring the model ``task`` needs up on the loader thread, and wait for it there.

    The first batch of a job awaits the load this way: the load runs on the engine's own
    loader thread (`engine.backends.huggingface.warm_async`), from the local cache when it
    holds the model, while the loop keeps running; every later batch finds the slot
    resident. Measured on the owner's node, 28 Oct 2026: the NER load inside a batch's
    pool worker went to the Hub with the files on disk and held the slot for 20 s.

    An engine that cannot say which model it would use (a test double) is left alone, and
    a load that fails is left to the run, which reports it as the task's own failure.
    """
    config = getattr(engine, "inference_config", None)
    provider = str(task.model_request.provider or "").strip().lower()
    if config is None or provider != "huggingface":
        return
    from ....engine.backends.huggingface import WARMABLE_SUBTYPES, warm_async

    resolved = config(task)
    subtype = str(resolved.get("subtype") or "")
    if subtype not in WARMABLE_SUBTYPES:
        return
    try:
        await warm_async(subtype, resolved.get("model"))
    except Exception as exc:  # noqa: BLE001 — the run reports its own failure
        logger.debug("model warm for %s did not finish: %s", subtype, exc)


def warm_models_for_jobs(job_names: List[str], *, engine: Any = None) -> List[str]:
    """Start the loads the named canonical jobs will need, on the loader thread, now.

    Returns at once with the subtypes it started. The first batch of each job awaits the
    same load through `warm_model_for`, so starting it here only moves it earlier: the
    load overlaps the stage's own work instead of following it.
    """
    started: List[str] = []
    for job in job_names:
        subtype = WARMED_JOB_SUBTYPES.get(str(job or ""))
        if subtype is None:
            continue
        try:
            if engine is None:
                from ....engine.engine import Engine

                engine = Engine()
            task = ProcessingTask(
                id=f"warm_{subtype}", type="enrichment", subtype=subtype, source_id=None,
                record_ids=[], input={},
                model_request=ModelRequest(provider="huggingface", model=None),
                requested_by=RequestedBy(origin="ingestion_pipeline"))
            resolved = engine.inference_config(task)
            from ....engine.backends.huggingface import warm

            warm(subtype, resolved.get("model"))
            started.append(subtype)
        except Exception as exc:  # noqa: BLE001 — a warm that cannot start costs the batch nothing
            logger.debug("model warm for %s not started: %s", job, exc)
    return started


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
    await warm_model_for(engine, task)
    return await asyncio.to_thread(engine.run, task)
