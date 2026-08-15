"""Enrichment Lab worker: executes dry-runs of enrichment jobs.

Runs the REAL enrichment job code (``job.enrich()``) against materialized lab
inputs with an optional per-group model override, and stores structured
outputs in lab tables only — node data is never written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from . import store

logger = logging.getLogger("topos.enrichment_lab.worker")

DEFAULT_MODEL_TAG = "default"


class _ModelOverrideEngine:
    """Engine wrapper that forces provider/model on every task it runs."""

    def __init__(self, inner: Any, provider: Optional[str], model: Optional[str]) -> None:
        self._inner = inner
        self._provider = provider
        self._model = model

    def run(self, task: Any) -> Any:
        if self._provider:
            task.model_request.provider = self._provider
        if self._model:
            task.model_request.model = self._model
        return self._inner.run(task)


def parse_model_tag(tag: str, *, default_provider: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """'default' -> no override; 'hf:org/repo' / 'ollama:tag' -> explicit;
    bare tag -> job's default provider (HF ids with a slash imply huggingface)."""
    clean = str(tag or "").strip()
    if not clean or clean == DEFAULT_MODEL_TAG:
        return None, None
    if clean.startswith("hf:"):
        return "huggingface", clean[3:].strip()
    if clean.startswith("ollama:"):
        return "ollama", clean[7:].strip()
    if "/" in clean:
        return "huggingface", clean
    return default_provider or "huggingface", clean


def _hf_model_cached(model: str) -> bool:
    """True when the model snapshot is already in the local HF cache."""
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=model,
            local_files_only=True,
            ignore_patterns=list(_SNAPSHOT_IGNORE_PATTERNS),
        )
        return True
    except Exception:  # noqa: BLE001 — any miss means "not cached"
        return False


_DISK_HEADROOM = 1.2


def _disk_space_error(size_bytes: Optional[int]) -> Optional[str]:
    """Refuse a download that clearly cannot fit on disk (when size is known)."""
    if not size_bytes or size_bytes <= 0:
        return None
    try:
        import os
        import shutil

        from huggingface_hub.constants import HF_HUB_CACHE

        from . import model_resolve

        path = str(HF_HUB_CACHE)
        while path and not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        free = shutil.disk_usage(path or "/").free
    except Exception:  # noqa: BLE001 — never block on the pre-check itself
        return None
    needed = int(size_bytes * _DISK_HEADROOM)
    if free < needed:
        return (
            "insufficient_disk_space: model weights need ~"
            f"{model_resolve.format_size(needed)} but only "
            f"{model_resolve.format_size(free)} is free"
        )
    return None


# Weight formats the torch runtime never loads; skipping them avoids
# downloading the same model two or three times over.
_SNAPSHOT_IGNORE_PATTERNS = (
    "*.h5",
    "*.msgpack",
    "*.tflite",
    "*.ot",
    "*.onnx",
    "onnx/**",
    "openvino/**",
    "*.gguf",
)


def _ensure_hf_model(model: str) -> Optional[str]:
    """Download the model snapshot; returns a user-facing error or None."""
    try:
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
    except ImportError:
        return None  # let the lazy from_pretrained path handle it
    try:
        snapshot_download(repo_id=model, ignore_patterns=list(_SNAPSHOT_IGNORE_PATTERNS))
        return None
    except GatedRepoError:
        return (
            f"Model '{model}' is gated on HuggingFace; accept its license on "
            "huggingface.co and configure an HF token, then retry"
        )
    except RepositoryNotFoundError:
        return f"Model '{model}' was not found on the HuggingFace Hub"
    except Exception as exc:  # noqa: BLE001 — network, disk, corrupt snapshot
        return f"download_failed: {str(exc)[:300]}"


def _prepare_hf_model(
    conn: Any, job_id: str, model: str, model_runs: List[Dict[str, Any]]
) -> Optional[str]:
    """Pre-flight an explicit HF model: task fit, disk space, download.

    Returns an error string (fails the model's runs) or None to proceed.
    Hub-unreachable resolution never blocks: offline nodes can still run
    already-downloaded models.
    """
    from . import model_resolve

    resolved = model_resolve.resolve_model(model)
    status = resolved.get("status")
    if status == "not_found":
        return f"Model '{model}' was not found on the HuggingFace Hub"
    if status == "unauthorized":
        return (
            f"Model '{model}' is gated or private; accept its license on "
            "huggingface.co and configure an HF token"
        )
    if status == "ok":
        fit = model_resolve.task_compatibility(job_id, resolved.get("pipeline_tag"))
        if fit is False:
            from ..enrichment.catalog import get_catalog_entry

            entry = get_catalog_entry(job_id)
            expected = (entry.hf_task if entry else None) or "unknown"
            return (
                f"task_mismatch: model '{model}' is tagged "
                f"'{resolved.get('pipeline_tag')}' but this enrichment expects "
                f"'{expected}'"
            )

    if _hf_model_cached(model):
        return None

    if status == "ok":
        disk_error = _disk_space_error(resolved.get("size_bytes"))
        if disk_error:
            return disk_error

    for run in model_runs:
        store.update_run(conn, run["id"], status="downloading_model")
    return _ensure_hf_model(model)


def _build_job(job_id: str, engine: Any) -> Any:
    from ..enrichment.jobs import (
        EmbeddingsJob,
        Emo27Job,
        EntitiesJob,
        GoalExtractionJob,
        SentimentJob,
        TopicsJob,
    )

    factories = {
        "emo_27": Emo27Job,
        "entities": EntitiesJob,
        "sentiment": SentimentJob,
        "embeddings": EmbeddingsJob,
        "topics": TopicsJob,
        "goal_extraction": GoalExtractionJob,
    }
    factory = factories.get(job_id)
    if not factory:
        raise ValueError(f"Enrichment '{job_id}' is not runnable in the Lab")
    return factory(engine=engine)


def _record_for_job(job_id: str, record_id: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a lab input into the canonical-record dict the job expects."""
    record = dict(input_data)
    record.setdefault("message_id", record_id)
    record.setdefault("content", input_data.get("body") or input_data.get("content") or "")
    record.setdefault("source_id", input_data.get("source_id") or "enrichment_lab")
    return record


def _summarize_output(job_id: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trim heavy payloads (vectors) for storage/display."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        slim = dict(row)
        vector = slim.pop("vector", None)
        if job_id == "embeddings" and isinstance(vector, list):
            slim["vector_preview"] = [round(float(v), 5) for v in vector[:8]]
            slim.setdefault("dims", len(vector))
        out.append(slim)
    return out


async def _process_group(group_id: str) -> None:
    from ..core.state import get_db_connection
    from ..engine import Engine
    from ..enrichment.catalog import get_catalog_entry

    conn = get_db_connection()
    if not conn:
        logger.error("Enrichment Lab worker: no database connection")
        return
    group_row = store.get_group(conn, group_id)
    if not group_row:
        logger.error("Enrichment Lab worker: group %s not found", group_id)
        return
    group = dict(group_row)
    job_id = str(group["job_id"])
    entry = get_catalog_entry(job_id)
    default_provider = entry.default_provider if entry else "huggingface"

    store.update_group_status(conn, group_id, "running")
    runs = [dict(r) for r in store.list_runs(conn, group_id)]
    runs_by_model: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs:
        runs_by_model.setdefault(str(run["model_tag"]), []).append(run)

    any_failed = False
    for model_tag, model_runs in runs_by_model.items():
        provider, model = parse_model_tag(model_tag, default_provider=default_provider)

        if provider == "huggingface" and model:
            prep_error = _prepare_hf_model(conn, job_id, model, model_runs)
            if prep_error:
                finished = store.utc_now_iso()
                for run in model_runs:
                    store.update_run(
                        conn,
                        run["id"],
                        status="failed",
                        finished_at=finished,
                        error_code=prep_error,
                    )
                any_failed = True
                continue

        engine: Any = Engine()
        if provider or model:
            engine = _ModelOverrideEngine(engine, provider, model)
        try:
            job = _build_job(job_id, engine)
        except ValueError as exc:
            for run in model_runs:
                store.update_run(conn, run["id"], status="failed", error_code=str(exc))
            any_failed = True
            continue

        for run in model_runs:
            try:
                input_data = json.loads(run.get("input_json") or "{}")
            except json.JSONDecodeError:
                input_data = {}
            record = _record_for_job(job_id, str(run["record_id"]), input_data)
            started = store.utc_now_iso()
            t0 = time.monotonic()
            try:
                rows = await job.enrich([record])
                latency_ms = int((time.monotonic() - t0) * 1000)
                if rows and isinstance(rows[0], dict) and rows[0].get("_deferred"):
                    store.update_run(
                        conn,
                        run["id"],
                        status="failed",
                        started_at=started,
                        finished_at=store.utc_now_iso(),
                        latency_ms=latency_ms,
                        error_code=str(rows[0].get("error") or "deferred"),
                    )
                    any_failed = True
                    continue
                store.update_run(
                    conn,
                    run["id"],
                    status="succeeded",
                    started_at=started,
                    finished_at=store.utc_now_iso(),
                    latency_ms=latency_ms,
                    output_json=json.dumps(_summarize_output(job_id, rows)),
                    metrics_json=json.dumps({"rows": len(rows)}),
                )
            except Exception as exc:  # noqa: BLE001 — per-run isolation
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.error(
                    "Enrichment Lab run failed: group=%s run=%s model=%s: %s",
                    group_id,
                    run["id"],
                    model_tag,
                    exc,
                )
                store.update_run(
                    conn,
                    run["id"],
                    status="failed",
                    started_at=started,
                    finished_at=store.utc_now_iso(),
                    latency_ms=latency_ms,
                    error_code=str(exc)[:500],
                )
                any_failed = True

    final = "completed_with_errors" if any_failed else "completed"
    store.update_group_status(conn, group_id, final)

    try:
        from ..engine.pipeline_memory import flush_engine_model_cache_after_pipeline

        flush_engine_model_cache_after_pipeline()
    except Exception:  # noqa: BLE001
        pass


def process_job_group_sync(group_id: str) -> None:
    """Synchronous entrypoint (run via asyncio.to_thread from the service)."""
    asyncio.run(_process_group(group_id))
