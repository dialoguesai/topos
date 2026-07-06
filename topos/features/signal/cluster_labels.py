"""Bounded local-LLM topic-cluster labels with deterministic fallback.

TF-IDF term labels degrade with corpus noise ("https / good / here" on the
live node). A single short local-LLM call per cluster per full recompute is
cheap (recomputes are debounced to ~daily) and makes every surface that shows
cluster labels legible.

Contract (same shape as the disclosure minimizer's EngineSelector):
  * ``complete`` is injectable for tests;
  * any timeout / error / unparseable output keeps the deterministic label;
  * the first hard failure aborts remaining calls (a down model must cost one
    timeout, not k of them).
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("topos.features.signal.cluster_labels")

_LABEL_POOL = ThreadPoolExecutor(max_workers=2)
_MAX_LABEL_CHARS = 60
_SAMPLE_PREVIEWS = 12
_PREVIEW_CHARS = 140

_REJECT_MARKERS = ("label", "topic cluster", "i cannot", "i can't", "sorry", "as an ai")


def cluster_label_model() -> str:
    explicit = os.environ.get("TOPOS_CLUSTER_LABEL_MODEL", "").strip()
    if explicit:
        return explicit
    try:
        from ...config.settings import settings

        return str(settings.ollama_query_model)
    except Exception:
        return "llama3.2:latest"


def build_label_prompt(cluster: Dict[str, Any]) -> str:
    terms = [str(t) for t in (cluster.get("label_terms") or []) if t]
    label = str(cluster.get("label") or "")
    if label:
        terms = [t for t in label.split(" / ") if t] + terms
    previews: List[str] = []
    for member in (cluster.get("members") or [])[: _SAMPLE_PREVIEWS * 3]:
        text = str(member.get("text_preview") or "").strip()
        if text:
            previews.append(text[:_PREVIEW_CHARS])
        if len(previews) >= _SAMPLE_PREVIEWS:
            break
    lines = [
        "You name topic clusters built from one person's private notes,",
        "messages, and web activity. Reply with ONLY a short descriptive",
        "label of 2-5 words. No quotes, no trailing punctuation, no",
        "explanation.",
        "",
        f"Frequent terms: {', '.join(terms[:8]) or '(none)'}",
        "Sample items:",
    ]
    lines.extend(f"- {p}" for p in previews)
    lines.append("Label:")
    return "\n".join(lines)


def parse_label(text: str) -> Optional[str]:
    """First line of model output, sanitized; None keeps the fallback label."""
    raw = str(text or "").strip()
    if not raw:
        return None
    line = raw.splitlines()[0].strip()
    line = line.strip("\"'`“”‘’ .:;")
    line = re.sub(r"\s+", " ", line)
    if not line or len(line) > _MAX_LABEL_CHARS:
        return None
    lowered = line.lower()
    if any(marker in lowered for marker in _REJECT_MARKERS):
        return None
    if len(line.split()) > 7:
        return None
    return line


class LabelerUnavailable(Exception):
    pass


def _complete_via_engine(prompt: str) -> str:
    from ...engine.client import get_engine_client_or_local
    from ...engine.tasks import ModelRequest, ProcessingTask

    client = get_engine_client_or_local(None)
    task = ProcessingTask(
        id="cluster_label",
        type="query_inference",
        subtype="query_inference",
        source_id="cluster_labeler",
        record_ids=[],
        input={"query": prompt, "context": ""},
        model_request=ModelRequest(provider="ollama", model=cluster_label_model()),
    )
    result = client.run(task)
    if getattr(result, "status", None) != "completed":
        raise LabelerUnavailable(f"label model status={getattr(result, 'status', None)}")
    out = result.output or {}
    return str(out.get("answer") or out.get("output") or "")


def apply_llm_cluster_labels(
    clusters: List[Dict[str, Any]],
    *,
    complete: Optional[Callable[[str], str]] = None,
    timeout_sec: float = 10.0,
    mode: Optional[str] = None,
) -> int:
    """Replace deterministic labels with LLM labels where possible.

    The term label is preserved in metadata["term_label"] so nothing is lost
    and drift between labelers stays inspectable. Returns count relabeled.
    """
    from .vector_settings import cluster_llm_labels_mode

    resolved_mode = mode or cluster_llm_labels_mode()
    if resolved_mode == "off" or not clusters:
        return 0
    runner = complete or _complete_via_engine
    relabeled = 0
    for cluster in clusters:
        prompt = build_label_prompt(cluster)
        try:
            future = _LABEL_POOL.submit(runner, prompt)
            text = future.result(timeout=timeout_sec)
        except (FuturesTimeoutError, LabelerUnavailable, Exception) as exc:  # noqa: BLE001
            logger.info("cluster LLM labeling stopped (%s); term labels kept", exc)
            break  # one failure means the model is down — don't pay k timeouts
        label = parse_label(text)
        if not label:
            continue
        metadata = dict(cluster.get("metadata") or {})
        metadata["term_label"] = cluster.get("label")
        metadata["label_model"] = cluster_label_model()
        cluster["metadata"] = metadata
        cluster["label"] = label
        relabeled += 1
    return relabeled
