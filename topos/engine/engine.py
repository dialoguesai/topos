"""Topos Engine: single entry point for processing tasks (PRD §5)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from .intake import normalize_task

logger = logging.getLogger("topos.engine")
from .queue_manager import QueueManager, TaskHandle
from .result_formatter import format_result
from .router import get_adapter_for_task
from .tasks import ExecutionMeta, ProcessingResult, ProcessingTask
from .validator import validate_task


class Engine:
    """Core processing engine. run(task) sync; submit(task) async via queue."""

    def __init__(
        self,
        registry: Optional[Any] = None,
        queue_max_size: int = 0,
        use_queue_analyzer: bool = False,
    ) -> None:
        """Optional registry for model resolution; queue_max_size 0 = unbounded.
        use_queue_analyzer: when True, worker dequeues in model-batched order (same model together)."""
        self._registry = registry
        self._queue = QueueManager(max_size=queue_max_size)
        self._use_queue_analyzer = use_queue_analyzer

    def submit(self, task: ProcessingTask) -> Optional[TaskHandle]:
        """Enqueue task; return TaskHandle or None if queue full. Worker must run to process."""
        valid, err_msg = validate_task(task)
        if not valid:
            handle = TaskHandle(task.id)
            handle._set_failed(format_result(task_id=task.id, status="failed", raw_output={}, error=err_msg))
            return handle
        task_id = self._queue.enqueue(task)
        if task_id is None:
            return None
        return self._queue.get_handle(task_id)

    def run_worker_once(self) -> bool:
        """Dequeue one task (FIFO or model-sorted when use_queue_analyzer), run it, store result in handle. Returns True if a task was run."""
        task = self._queue.get_next_for_worker(self._use_queue_analyzer, block=False)
        if task is None:
            return False
        handle = self._queue.get_handle(task.id)
        if handle:
            handle._set_running()
        result = self.run(task)
        if handle:
            if result.status == "completed":
                handle._set_completed(result)
            else:
                handle._set_failed(result)
        return True

    def run(self, task: ProcessingTask) -> ProcessingResult:
        """
        Execute a task synchronously: intake → validate → route → inference → format.
        Returns a ProcessingResult (never raises; errors are in result.status and result.error).
        """
        # Validate first (before intake fills defaults, so empty provider is rejected)
        valid, err_msg = validate_task(task)
        if not valid:
            return format_result(
                task_id=task.id,
                status="failed",
                raw_output={},
                error=err_msg,
            )
        # Intake: normalize defaults
        normalized = normalize_task(task)
        # Build adapter config: subtype and model from task or registry
        config = self._build_inference_config(normalized)
        # Route to backend
        adapter = get_adapter_for_task(normalized)
        # Run inference
        start = time.perf_counter()
        raw_output = adapter.run_inference(normalized.input, config=config)
        duration_ms = int((time.perf_counter() - start) * 1000)
        # Adapter may return error in output
        if raw_output.get("error"):
            try:
                from ..observability.metrics import record_metric
                record_metric("engine.task_failed", 1.0)
            except Exception:
                pass
            return format_result(
                task_id=normalized.id,
                status="failed",
                raw_output=raw_output,
                provenance_source_id=normalized.source_id,
                provenance_record_ids=normalized.record_ids if normalized.record_ids else None,
                execution_meta=ExecutionMeta(
                    provider=normalized.model_request.provider,
                    model=config.get("model"),
                    duration_ms=duration_ms,
                    cache_hit=False,
                ),
                error=str(raw_output.get("error")),
            )
        execution_meta = ExecutionMeta(
            provider=normalized.model_request.provider,
            model=raw_output.get("model") or normalized.model_request.model or config.get("model"),
            duration_ms=duration_ms,
            cache_hit=False,
        )
        try:
            from ..observability.metrics import record_metric
            record_metric("engine.task_completed", 1.0)
            record_metric("engine.inference_duration_ms", float(duration_ms))
        except Exception:
            pass
        return format_result(
            task_id=normalized.id,
            status="completed",
            raw_output=raw_output,
            provenance_source_id=normalized.source_id,
            provenance_record_ids=normalized.record_ids if normalized.record_ids else None,
            execution_meta=execution_meta,
        )

    def ensure_model(self, provider: str, model_name: str, **kwargs: Any) -> bool:
        """
        Ensure the model is available: download/pull if not present.
        Returns True if we downloaded/pulled (caller may remove it later for cleanup), False if already present.
        Logs when a download is started and when complete; adapters may log progress.
        """
        provider = (provider or "").strip().lower()
        if provider == "ollama":
            from .backends import get_ollama_adapter
            adapter = get_ollama_adapter()
            if adapter.ensure_model(model_name):
                logger.info("Model %s (provider=ollama) download complete.", model_name)
                return True
            return False
        if provider == "huggingface":
            from .backends import get_huggingface_adapter
            adapter = get_huggingface_adapter()
            if adapter.ensure_model(model_name, subtype=kwargs.get("subtype")):
                logger.info("Model %s (provider=huggingface) download complete.", model_name)
                return True
            return False
        return False

    def _build_inference_config(self, task: ProcessingTask) -> Dict[str, Any]:
        """Build config dict for adapter.run_inference: subtype and model."""
        model = task.model_request.model
        if not model and self._registry is not None:
            spec = self._registry.get_model_for_task(task.type, task.subtype)
            if spec:
                provider = (task.model_request.provider or "").strip().lower()
                if provider == "ollama":
                    model = spec.get("ollama_model") or spec.get("model")
                else:
                    model = spec.get("huggingface_path") or spec.get("model")
        return {
            "subtype": task.subtype or "",
            "model": model,
        }
