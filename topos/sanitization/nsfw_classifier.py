"""Ingest-time NSFW text classification for Platform Privacy Layer (tag, do not sanitize).

**The cutoff applies to the NSFW label.** A result counts as NSFW only when the classifier's NSFW label scores
strictly above ``nsfw_classifier_threshold`` (default 0.91, owner decision 1 Oct 2026). Before, an NSFW top label
counted at any confidence and the setting reached only labels this classifier does not use, so a coin-flip
(0.502) hard-withheld a journal entry from every share. The classifier is trained on Reddit posts and over-fires
on short time-log text: on a copy of one owner's database it had flagged about four in ten journal entries, most
of them below 0.9. ``topos.disclosure.nsfw_recheck`` re-applies the same cutoff to rows tagged under the old rule,
from their stored score.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, Final, List, Optional, Tuple

logger = logging.getLogger("topos.sanitization.nsfw_classifier")

DEFAULT_NSFW_CLASSIFIER_MODEL: Final[str] = "michellejieli/NSFW_text_classifier"
NSFW_CLASSIFY_MAX_BATCH: Final[int] = 32
#: The default for ``nsfw_classifier_threshold``: an NSFW label counts only ABOVE it, never at it.
DEFAULT_NSFW_CLASSIFIER_THRESHOLD: Final[float] = 0.91
#: Labels a classifier uses for "not safe" and for "safe". The shipped classifier has two, NSFW and SFW.
NSFW_LABELS: Final[tuple[str, ...]] = ("nsfw", "label_1", "toxic", "obscene")
SAFE_LABELS: Final[tuple[str, ...]] = ("sfw", "label_0", "neutral", "safe")
#: A label in neither list counts only at or above this floor or the cutoff, whichever is higher (unchanged rule).
UNKNOWN_LABEL_FLOOR: Final[float] = 0.85

# Fast heuristic fallback when ML deps unavailable (tests / lean DB image).
_HEURISTIC_NSFW_TOKENS: Final[tuple[str, ...]] = (
    "nsfw",
    "xxx",
    "porn",
    "explicit",
)
#: The heuristic's fixed scores. Its rows are stored under the classifier's model id, so the stored score is the
#: only mark of them: a float32 classifier output can never equal this double exactly (the re-check relies on it).
HEURISTIC_NSFW_SCORE: Final[float] = 0.95
HEURISTIC_SAFE_SCORE: Final[float] = 0.05


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


def nsfw_threshold(value: Any = None) -> float:
    """The NSFW cutoff: ``value``, else the ``nsfw_classifier_threshold`` setting, else the default.

    A cutoff is a number in [0, 1). Anything else (text, a boolean, NaN, a negative number, 1 or more) is a
    misconfiguration and reads as the default: with a strict comparison a cutoff of 1 would silently stop every
    flag, and the switch for that is ``nsfw_classifier_enabled``.
    """
    if value is None:
        from topos.config.settings import settings

        value = getattr(settings, "nsfw_classifier_threshold", None)
    if value is None or isinstance(value, bool):
        return DEFAULT_NSFW_CLASSIFIER_THRESHOLD
    try:
        cut = float(value)
    except (TypeError, ValueError):
        return DEFAULT_NSFW_CLASSIFIER_THRESHOLD
    if not math.isfinite(cut) or not 0.0 <= cut < 1.0:
        return DEFAULT_NSFW_CLASSIFIER_THRESHOLD
    return cut


def is_nsfw_result(label: str, score: float, threshold: float) -> bool:
    """The gate. An NSFW label counts only when its score is strictly above ``threshold``.

    A safe label never counts. A label in neither list keeps the rule it always had: at or above
    ``max(threshold, UNKNOWN_LABEL_FLOOR)``. An NSFW label with a score that is not a finite number fails
    closed (counts), as every NSFW label did before.
    """
    name = str(label or "").strip().lower()
    if name in SAFE_LABELS:
        return False
    if name in NSFW_LABELS or "nsfw" in name:
        if not math.isfinite(score):
            return True
        return score > threshold
    return math.isfinite(score) and score >= max(threshold, UNKNOWN_LABEL_FLOOR)


def _get_pipeline(model_id: str):
    from topos.engine.model_cache import ModelSlot, get_model_cache

    def _load():
        from topos.sanitization.hub_pipeline import load_pipeline

        logger.info("Loading NSFW classifier model=%r", model_id)
        # Cache first: see hub_pipeline for the boot that hung in a Hub GET.
        return load_pipeline(
            "text-classification",
            model_id,
            model_class="AutoModelForSequenceClassification",
            top_k=None,
        )

    handle, _ = get_model_cache().acquire(ModelSlot.NSFW, model_id, _load)
    return handle


def prewarm_nsfw_classifier() -> None:
    """Load the NSFW classifier pipeline (startup background prewarm)."""
    if not nsfw_classifier_enabled() or not nsfw_classifier_available():
        return
    from topos.config.settings import settings

    model_id = str(
        getattr(settings, "nsfw_classifier_model", None) or DEFAULT_NSFW_CLASSIFIER_MODEL
    )
    _get_pipeline(model_id)


def _heuristic_nsfw(text: str) -> Tuple[bool, float]:
    lower = text.lower()
    for tok in _HEURISTIC_NSFW_TOKENS:
        if tok in lower:
            return True, HEURISTIC_NSFW_SCORE
    return False, HEURISTIC_SAFE_SCORE


def classify_nsfw_text(text: str, *, model_id: Optional[str] = None, threshold: Optional[float] = None) -> Tuple[bool, float, str]:
    """Return (is_nsfw, score, label). Fail-open to safe when classification unavailable.

    ``score`` is the classifier's confidence in its top label, as stored in ``content_nsfw_score``; the cutoff
    decides ``is_nsfw`` (:func:`is_nsfw_result`). The token heuristic is not gated by the cutoff.
    """
    from topos.config.settings import settings

    if not text or not str(text).strip():
        return False, 0.0, "empty"
    if not nsfw_classifier_enabled():
        return False, 0.0, "disabled"

    cut = nsfw_threshold(threshold)

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
        return is_nsfw_result(label, score, cut), score, label or model
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
