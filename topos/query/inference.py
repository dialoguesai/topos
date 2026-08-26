"""Bounded query inference via Engine (Appendix B)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, Optional

from ..config.settings import settings
from ..engine.client import EngineClient, get_engine_client_or_local
from ..engine.tasks import ModelRequest, ProcessingTask

DEFAULT_MAX_CONTEXT_CHARS = 4000
DEFAULT_INFERENCE_TIMEOUT_SEC = 45.0
_INFERENCE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="query_inference")


def build_inference_context_packet(filtered_context: Dict[str, Any], *, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS, packet_resolution: str = "scores_only") -> Dict[str, Any]:
    """Bound the context for the inference model, strongest evidence first.

    The retrieval packet lists `scores` LAST (after clusters/hits/graph), so a
    naive prefix truncation amputated exactly the evidence the model needed —
    it then honestly answered "unknown" to well-supported queries. Reorder to
    evidence-first and trim the low-signal furniture before cutting."""
    ctx = dict(filtered_context or {})
    compact: Dict[str, Any] = {}
    for key in ("scope_id", "access_mode"):
        if key in ctx:
            compact[key] = ctx[key]
    # Packet resolution (F2.6 / owner decision 2026-08-25): at 'facts'/'facts_all' the
    # fact items carry content, and it is emitted as a STRUCTURED block with its own
    # budget instead of competing as flattened prose inside `scores`. The env flag is a
    # harness override only. See PLAN_DERIVATION_LAYER.md §2.6 BP2.
    if packet_resolution in ("facts", "facts_all") or os.environ.get(
        "TOPOS_FACTS_BLOCK", ""
    ).strip() in ("1", "true", "on"):
        fact_items = [s_ for s_ in (ctx.get("scores") or [])
                      if isinstance(s_, dict) and "fact" in str(s_.get("retrieval_source") or "")]
        if fact_items:
            block = []
            for it in fact_items[:12]:
                entry = {"fact": str(it.get("summary_text") or it.get("content") or "")[:160]}
                if it.get("predicate"):
                    entry["predicate"] = it.get("predicate")
                if it.get("value") is not None:
                    entry["value"] = str(it.get("value"))[:120]
                for k in ("valid_from", "valid_to", "altitude", "pack", "confidence"):
                    if it.get(k) is not None:
                        entry[k] = it.get(k)
                block.append(entry)
            compact["facts"] = block   # placed before scores: truncation hits it LAST
    scores = ctx.get("scores")
    if isinstance(scores, list) and scores:
        ranked = sorted(
            (s for s in scores if isinstance(s, dict)),
            key=lambda s: float(s.get("relevance_score") or 0.0),
            reverse=True,
        )
        compact["scores"] = ranked[:15]
    hits = ctx.get("semantic_hits")
    if isinstance(hits, list) and hits:
        strong = [h for h in hits if isinstance(h, dict) and h.get("similarity") is not None]
        strong.sort(key=lambda h: float(h.get("similarity") or 0.0), reverse=True)
        if strong:
            compact["semantic_hits"] = strong[:10]
    clusters = ctx.get("topic_clusters")
    if isinstance(clusters, list) and clusters:
        compact["topic_clusters"] = [
            {k: c.get(k) for k in ("label", "relevance_score") if isinstance(c, dict)}
            for c in clusters[:3]
        ]
    for key, value in ctx.items():
        if key not in compact and key not in ("semantic_hits", "topic_clusters", "graph", "scores"):
            compact[key] = value
    raw = json.dumps(compact, default=str, separators=(",", ":"))
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
    packet_resolution: str = "scores_only",
) -> Dict[str, Any]:
    bounded = build_inference_context_packet(context_packet, max_chars=max_chars, packet_resolution=packet_resolution)
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
