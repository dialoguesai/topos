"""Ingest-time NSFW text classification for Platform Privacy Layer (tag, do not sanitize)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Final, List, Optional, Tuple

logger = logging.getLogger("topos.sanitization.nsfw_classifier")

DEFAULT_NSFW_CLASSIFIER_MODEL: Final[str] = "michellejieli/NSFW_text_classifier"
NSFW_CLASSIFY_MAX_BATCH: Final[int] = 32

# Fast heuristic fallback when ML deps unavailable (tests / lean DB image).
_HEURISTIC_NSFW_TOKENS: Final[tuple[str, ...]] = (
    "nsfw",
    "xxx",
    "porn",
    "explicit",
)


def nsfw_classifier_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def nsfw_classifier_enabled() -> bool:
    from topos.config.settings import settings

    return bool(getattr(settings, "nsfw_classifier_enabled", True))


def _get_pipeline(model_id: str):
    from topos.engine.model_cache import ModelSlot, get_model_cache

    def _load():
        from transformers import pipeline

        logger.info("Loading NSFW classifier model=%r", model_id)
        return pipeline("text-classification", model=model_id, top_k=None)

    handle, _ = get_model_cache().acquire(ModelSlot.NSFW, model_id, _load)
    return handle


def _heuristic_nsfw(text: str) -> Tuple[bool, float]:
    lower = text.lower()
    for tok in _HEURISTIC_NSFW_TOKENS:
        if tok in lower:
            return True, 0.95
    return False, 0.05


def classify_nsfw_text(text: str, *, model_id: Optional[str] = None, threshold: Optional[float] = None) -> Tuple[bool, float, str]:
    """Return (is_nsfw, score, label). Fail-open to safe when classification unavailable."""
    from topos.config.settings import settings

    if not text or not str(text).strip():
        return False, 0.0, "empty"
    if not nsfw_classifier_enabled():
        return False, 0.0, "disabled"

    cut = threshold
    if cut is None:
        cut = float(getattr(settings, "nsfw_classifier_threshold", 0.5) or 0.5)

    model = (model_id or getattr(settings, "nsfw_classifier_model", None) or DEFAULT_NSFW_CLASSIFIER_MODEL).strip()
    max_in = int(getattr(settings, "nsfw_classifier_max_input_chars", 512) or 512)
    snippet = str(text)
    if max_in > 0 and len(snippet) > max_in:
        snippet = snippet[:max_in]

    if not nsfw_classifier_available():
        is_nsfw, score = _heuristic_nsfw(snippet)
        return is_nsfw, score, "heuristic"

    try:
        pipe = _get_pipeline(model)
        raw = pipe(snippet)
        labels: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            if raw and isinstance(raw[0], list):
                labels = list(raw[0])
            elif raw and isinstance(raw[0], dict):
                labels = list(raw)
        best = max(labels, key=lambda x: float(x.get("score") or 0.0), default={})
        label = str(best.get("label") or "").strip().lower()
        score = float(best.get("score") or 0.0)
        is_nsfw = label in ("nsfw", "label_1", "toxic", "obscene") or "nsfw" in label
        if not is_nsfw and score >= cut and label not in ("sfw", "label_0", "neutral", "safe"):
            is_nsfw = score >= max(cut, 0.85)
        if label in ("sfw", "label_0", "neutral", "safe"):
            is_nsfw = False
        return is_nsfw, score, label or model
    except Exception as exc:  # noqa: BLE001
        logger.warning("NSFW classifier failed, using heuristic: %s", exc)
        is_nsfw, score = _heuristic_nsfw(snippet)
        return is_nsfw, score, "heuristic_fallback"


def classify_nsfw_batch(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Batch NSFW classification for Engine tasks and HTTP API."""
    from topos.config.settings import settings

    if not nsfw_classifier_enabled():
        return {
            "status": "disabled",
            "items": [{"id": str(i.get("id") or ""), "nsfw": False, "score": 0.0, "label": "disabled"} for i in items],
            "model": getattr(settings, "nsfw_classifier_model", DEFAULT_NSFW_CLASSIFIER_MODEL),
        }
    if len(items) > NSFW_CLASSIFY_MAX_BATCH:
        return {
            "status": "too_large",
            "error": f"batch exceeds limit of {NSFW_CLASSIFY_MAX_BATCH}",
            "items": [],
            "model": getattr(settings, "nsfw_classifier_model", DEFAULT_NSFW_CLASSIFIER_MODEL),
        }
    model_id = (getattr(settings, "nsfw_classifier_model", None) or DEFAULT_NSFW_CLASSIFIER_MODEL).strip()
    out_items: List[Dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "")
        is_nsfw, score, label = classify_nsfw_text(text, model_id=model_id)
        out_items.append({"id": item_id, "nsfw": bool(is_nsfw), "score": score, "label": label})
    return {
        "status": "ok",
        "items": out_items,
        "model": model_id,
        "provider": "huggingface" if nsfw_classifier_available() else "heuristic",
    }
