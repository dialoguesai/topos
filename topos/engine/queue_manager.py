"""In-memory task queue for async execution (Sprint 05)."""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, Optional

from .tasks import ProcessingTask, ProcessingResult


class TaskHandle:
    """Handle for a submitted task: poll status and get result."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._status = "pending"  # pending | running | completed | failed
        self._result: Optional[ProcessingResult] = None
        self._lock = threading.Lock()

    def get_status(self) -> str:
        with self._lock:
            return self._status

    def get_result(self, timeout: Optional[float] = None) -> Optional[ProcessingResult]:
        with self._lock:
            result = self._result
            if result is not None and self._status in ("completed", "failed"):
                return result
            return self._result

    def consume_result(self) -> Optional[ProcessingResult]:
        """Return result and drop handle storage to avoid unbounded growth."""
        with self._lock:
            result = self._result
            return result

    def _set_running(self) -> None:
        with self._lock:
            self._status = "running"

    def _set_completed(self, result: ProcessingResult) -> None:
        with self._lock:
            self._status = "completed"
            self._result = result

    def _set_failed(self, result: Optional[ProcessingResult] = None) -> None:
        with self._lock:
            self._status = "failed"
            self._result = result


class QueueManager:
    """In-memory queue with optional max size."""

    def __init__(self, max_size: int = 0, *, max_handles: int = 256) -> None:
        self._max_size = max_size  # 0 = unbounded
        self._max_handles = max(1, int(max_handles))
        self._queue: queue.Queue = queue.Queue(maxsize=max_size if max_size > 0 else 0)
        self._handles: Dict[str, TaskHandle] = {}
        self._handles_lock = threading.Lock()

    def enqueue(self, task: ProcessingTask) -> Optional[str]:
        """Enqueue task; return task_id or None if queue full."""
        task_id = task.id
        if self._max_size > 0 and self._queue.qsize() >= self._max_size:
            return None
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            return None
        with self._handles_lock:
            self._handles[task_id] = TaskHandle(task_id)
        return task_id

    def dequeue(self, block: bool = True, timeout: Optional[float] = None) -> Optional[ProcessingTask]:
        try:
            return self._queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

    def get_next_for_worker(self, sort_by_model: bool, block: bool = False, timeout: Optional[float] = None) -> Optional[ProcessingTask]:
        """
        Get the next task for the worker. If sort_by_model is True, drain the queue,
        sort tasks by (provider, model) to batch same-model runs, then return the first
        and put the rest back in order.
        """
        if not sort_by_model:
            return self.dequeue(block=block, timeout=timeout)
        # Drain into list
        tasks: list = []
        while True:
            t = self.dequeue(block=False)
            if t is None:
                break
            tasks.append(t)
        if not tasks:
            return None
        if len(tasks) == 1:
            return tasks[0]
        # Sort by (provider, model) so same model is processed together
        def model_key(task: ProcessingTask) -> str:
            p = (task.model_request.provider or "").strip().lower()
            m = (task.model_request.model or "").strip()
            return f"{p}|{m}"

        tasks.sort(key=model_key)
        # Put back all but the first
        for t in tasks[1:]:
            try:
                self._queue.put_nowait(t)
            except queue.Full:
                # Should not happen with unbounded queue; put the rest back and return first
                break
        return tasks[0]

    def get_handle(self, task_id: str) -> Optional[TaskHandle]:
        with self._handles_lock:
            return self._handles.get(task_id)

    def prune_handle(self, completed_task_id: str) -> None:
        with self._handles_lock:
            self._handles.pop(completed_task_id, None)
            if len(self._handles) <= self._max_handles:
                return
            for task_id, handle in list(self._handles.items()):
                if handle.get_status() in ("completed", "failed"):
                    self._handles.pop(task_id, None)
                if len(self._handles) <= self._max_handles:
                    break

    def qsize(self) -> int:
        return self._queue.qsize()
