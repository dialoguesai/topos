"""Bounded query inference via Engine (Appendix B)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, Optional

from ..config.settings import settings
from ..engine.client import EngineClient, get_engine_client_or_local
from ..engine.tasks import ModelRequest, ProcessingTask

DEFAULT_MAX_CONTEXT_CHARS = 4000
DEFAULT_INFERENCE_TIMEOUT_SEC = 45.0
_INFERENCE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="query_inference")


def build_inference_context_packet(filtered_context: Dict[str, Any], *, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS) -> Dict[str, Any]:
    raw = json.dumps(filtered_context, default=str, separators=(",", ":"))
    truncated = len(raw) > max_chars
    if truncated:
        raw = raw[:max_chars]
    return {"context": raw, "truncated": truncated}


def run_query_inference(
    *,
    query_text: str,
    context_packet: Dict[str, Any],
    scope_id: str,
    engine: Optional[EngineClient] = None,
    max_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    timeout_sec: float = DEFAULT_INFERENCE_TIMEOUT_SEC,
) -> Dict[str, Any]:
    bounded = build_inference_context_packet(context_packet, max_chars=max_chars)
    client = get_engine_client_or_local(engine)
    task = ProcessingTask(
        id=f"query_inf_{scope_id}",
        type="query_inference",
        subtype="query_inference",
        source_id=scope_id,
        record_ids=[],
        input={"query": query_text, "context": bounded["context"]},
        model_request=ModelRequest(provider="ollama", model=settings.ollama_query_model),
    )

    def _run() -> Any:
        return client.run(task)

    try:
        future = _INFERENCE_POOL.submit(_run)
        result = future.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        return {"answer": "unknown", "confidence": 0.0, "deferred": True, "error": "inference_timeout"}
    except Exception as exc:
        return {"answer": "unknown", "confidence": 0.0, "error": str(exc)}

    if result.status == "deferred":
        err = getattr(result, "error", None) or (result.output or {}).get("error")
        out = {"answer": "unknown", "confidence": 0.0, "deferred": True}
        if err:
            out["error"] = err
        return out
    if result.status != "completed":
        return {"answer": "unknown", "confidence": 0.0, "error": result.error}
    out = result.output or {}
    return {
        "answer": out.get("answer") or out.get("output") or "unknown",
        "confidence": float(out.get("confidence") or 0.0),
    }
