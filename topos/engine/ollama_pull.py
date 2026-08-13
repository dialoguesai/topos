"""Start and observe Ollama model downloads (PLAN_LOCAL_MODEL_QUICKSTART Q1).

The websocket is request/response, but a pull takes minutes. So a pull is a
start message plus a poll message over an in-memory record keyed by tag: the
browser asks once to begin and then polls for progress, exactly as it already
does for the sanitization prewarm.

Progress lives in this process only. A node restart mid-pull loses the record,
not the download — Ollama finishes it server-side, and the next poll answers
"idle" while ``list_models`` shows the tag arrived. That is the honest answer
for a fact we genuinely no longer know.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from .backends.ollama import OllamaAdapter

logger = logging.getLogger("topos.engine.ollama_pull")

STATE_IDLE = "idle"
STATE_PULLING = "pulling"
STATE_DONE = "done"
STATE_ERROR = "error"

_LOCK = threading.Lock()
_PROGRESS: Dict[str, Dict[str, Any]] = {}

#: A node runs for weeks and the browser may name any tag, typos included. More
#: than a handful of finished records means this store is being kept as a log,
#: which is not what it is for.
_MAX_TERMINAL_RECORDS = 64


def _evict_locked(keep: str = "") -> None:
    """Drop the oldest FINISHED records once there are too many.

    Caller holds ``_LOCK``. Only terminal records are eligible: forgetting a
    download that is still running would let the CTA offer to start it again,
    costing the owner the transfer twice (spec D4). `keep` spares the record the
    caller has just written, which is the one about to be polled. Dicts preserve
    insertion order, so the oldest terminal record is the first one found.
    """
    terminal = [
        tag
        for tag, record in _PROGRESS.items()
        if record.get("state") in (STATE_DONE, STATE_ERROR) and tag != keep
    ]
    for tag in terminal[: max(0, len(terminal) - _MAX_TERMINAL_RECORDS)]:
        _PROGRESS.pop(tag, None)


def _record(model: str) -> Dict[str, Any]:
    return {
        "model": model,
        "state": STATE_IDLE,
        "status": "",
        "completed": 0,
        "total": 0,
        "error": "",
    }


def reset_progress() -> None:
    """Drop every record. Test seam — nothing in the product calls this."""
    with _LOCK:
        _PROGRESS.clear()


def pull_status(model: str) -> Dict[str, Any]:
    """The current record for ``model``; an untouched tag reads back idle."""
    tag = str(model or "").strip()
    with _LOCK:
        existing = _PROGRESS.get(tag)
        return dict(existing) if existing is not None else _record(tag)


def _apply_frame(tag: str, frame: Dict[str, Any]) -> None:
    completed = int(frame.get("completed") or 0)
    total = int(frame.get("total") or 0)
    with _LOCK:
        record = _PROGRESS.setdefault(tag, _record(tag))
        _evict_locked(keep=tag)
        record["status"] = str(frame.get("status") or "")
        record["total"] = max(total, int(record.get("total") or 0))
        # /api/pull reports per-layer byte counts, so a fresh layer restarts at
        # zero. The UI reads this as a progress bar, and a bar that jumps
        # backwards reads as a bug even when the download is fine.
        record["completed"] = max(completed, int(record.get("completed") or 0))


def _run_pull(tag: str, adapter: Any) -> None:
    try:
        adapter.pull_model(tag, stream=True, on_progress=lambda frame: _apply_frame(tag, frame))
    except Exception as exc:  # noqa: BLE001 — the tag may simply not exist
        logger.warning("ollama pull failed for %s: %s", tag, exc)
        with _LOCK:
            record = _PROGRESS.setdefault(tag, _record(tag))
            record["state"] = STATE_ERROR
            record["error"] = str(exc)
            _evict_locked(keep=tag)
        return
    with _LOCK:
        record = _PROGRESS.setdefault(tag, _record(tag))
        record["state"] = STATE_DONE
        record["error"] = ""
        # A completed pull with no byte frames (already-present model) would
        # otherwise report 0/0 and render as a stalled bar.
        if not record["total"]:
            record["total"] = record["completed"] = 1
        _evict_locked(keep=tag)


def start_pull(model: str, *, adapter: Optional[Any] = None) -> Dict[str, Any]:
    """Begin downloading ``model`` in the background; returns the first record.

    Starting a tag that is already downloading is a no-op: a double-clicked CTA
    must not cost the owner a second multi-gigabyte transfer (spec D4).
    """
    tag = str(model or "").strip()
    if not tag:
        raise ValueError("model is required")

    with _LOCK:
        existing = _PROGRESS.get(tag)
        if existing is not None and existing.get("state") == STATE_PULLING:
            return dict(existing)
        record = _record(tag)
        record["state"] = STATE_PULLING
        _PROGRESS[tag] = record
        snapshot = dict(record)

    worker = threading.Thread(
        target=_run_pull,
        args=(tag, adapter if adapter is not None else OllamaAdapter()),
        name=f"ollama-pull-{tag}",
        daemon=True,
    )
    worker.start()
    return snapshot
