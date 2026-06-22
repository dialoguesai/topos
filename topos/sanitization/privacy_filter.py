"""
Local PII redaction via Hugging Face ``openai/privacy-filter``.

Uses the transformers token-classification pipeline recommended on the model card:
https://huggingface.co/openai/privacy-filter

Fail-open: callers should catch exceptions and keep the original text.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Final, List, Optional

logger = logging.getLogger("topos.sanitization.privacy_filter")

PRIVACY_FILTER_MODEL_ID: Final[str] = "openai/privacy-filter"

PRIVACY_FILTER_TRANSFORM_IDS: Final[tuple[str, ...]] = (
    "pii_redaction",
    "name_removal",
    "contact_removal",
)

ENTITY_PLACEHOLDERS: Final[Dict[str, str]] = {
    "private_person": "[NAME]",
    "private_email": "[EMAIL]",
    "private_phone": "[PHONE]",
    "private_address": "[ADDRESS]",
    "account_number": "[ACCOUNT]",
    "private_url": "[URL]",
    "private_date": "[DATE]",
    "secret": "[SECRET]",
}

TRANSFORM_ENTITY_GROUPS: Final[Dict[str, frozenset[str]]] = {
    "pii_redaction": frozenset(ENTITY_PLACEHOLDERS.keys()),
    "name_removal": frozenset({"private_person"}),
    "contact_removal": frozenset({"private_email", "private_phone", "private_address", "private_url"}),
}

_pipeline: Any = None
_pipeline_model: Optional[str] = None
_pipeline_lock = threading.Lock()


def privacy_filter_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def privacy_filter_enabled() -> bool:
    from topos.config.settings import settings

    return bool(getattr(settings, "privacy_filter_enabled", True)) and privacy_filter_available()


def _resolve_device() -> int | str:
    from topos.config.settings import settings

    configured = (getattr(settings, "privacy_filter_device", None) or "").strip().lower()
    if configured in ("cpu", "cuda", "mps"):
        import torch

        if configured == "cuda":
            return 0 if torch.cuda.is_available() else "cpu"
        if configured == "mps":
            return "mps" if torch.backends.mps.is_available() else "cpu"
        return configured

    import torch

    if torch.cuda.is_available():
        return 0
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _get_pipeline(model_id: str):
    global _pipeline, _pipeline_model
    with _pipeline_lock:
        if _pipeline is not None and _pipeline_model == model_id:
            return _pipeline
        from transformers import pipeline

        device = _resolve_device()
        logger.info("Loading privacy-filter pipeline model=%r device=%s", model_id, device)
        _pipeline = pipeline(
            "token-classification",
            model=model_id,
            device=device,
        )
        _pipeline_model = model_id
        return _pipeline


def _detect_entities(text: str, *, model_id: str) -> List[Dict[str, Any]]:
    pipe = _get_pipeline(model_id)
    result = pipe(text, aggregation_strategy="simple")
    return list(result) if isinstance(result, list) else []


def _redact_spans(
    text: str,
    entities: List[Dict[str, Any]],
    *,
    allowed_groups: frozenset[str],
) -> str:
    spans: List[tuple[int, int, str]] = []
    for ent in entities:
        group = str(ent.get("entity_group") or ent.get("entity") or "").strip()
        if group not in allowed_groups:
            continue
        start = ent.get("start")
        end = ent.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        placeholder = ENTITY_PLACEHOLDERS.get(group, "[PII]")
        spans.append((start, end, placeholder))
    if not spans:
        return text

    spans.sort(key=lambda s: s[0], reverse=True)
    out = text
    for start, end, placeholder in spans:
        if start < 0 or end > len(out) or start >= end:
            continue
        out = out[:start] + placeholder + out[end:]
    return out


def apply_text_transform_with_privacy_filter(
    text: str,
    transform_id: str,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """Run a PII transform using the local openai/privacy-filter model."""
    _ = params
    if transform_id not in PRIVACY_FILTER_TRANSFORM_IDS:
        raise ValueError(f"transform_id {transform_id!r} is not handled by privacy-filter")

    from topos.config.settings import settings

    model_id = (getattr(settings, "privacy_filter_model", None) or PRIVACY_FILTER_MODEL_ID).strip()
    max_in = getattr(settings, "privacy_filter_max_input_chars", None)
    if max_in is None:
        max_in = settings.sanitization_ollama_max_input_chars
    max_in = int(max_in)
    if max_in > 0 and len(text) > max_in:
        text = text[:max_in] + "\n[TRUNCATED_FOR_PRIVACY_FILTER]"

    allowed = TRANSFORM_ENTITY_GROUPS[transform_id]
    entities = _detect_entities(text, model_id=model_id)
    return _redact_spans(text, entities, allowed_groups=allowed)


PRIVACY_LAYER_VERSION: Final[str] = "1"
PRIVACY_DISCLOSE_MAX_BATCH: Final[int] = 32


def redact_privacy_batch(
    items: List[Dict[str, Any]],
    *,
    transform_id: str = "pii_redaction",
) -> Dict[str, Any]:
    """Batch PII redaction for Engine privacy_disclosure tasks and HTTP disclose API."""
    from topos.config.settings import settings

    if not privacy_filter_available():
        return {
            "error": "privacy filter unavailable",
            "status": "unavailable",
            "items": [],
            "model": getattr(settings, "privacy_filter_model", PRIVACY_FILTER_MODEL_ID),
            "privacy_layer_version": PRIVACY_LAYER_VERSION,
        }
    if len(items) > PRIVACY_DISCLOSE_MAX_BATCH:
        return {
            "error": f"batch exceeds limit of {PRIVACY_DISCLOSE_MAX_BATCH}",
            "status": "too_large",
            "items": [],
            "model": getattr(settings, "privacy_filter_model", PRIVACY_FILTER_MODEL_ID),
            "privacy_layer_version": PRIVACY_LAYER_VERSION,
        }
    model_id = (getattr(settings, "privacy_filter_model", None) or PRIVACY_FILTER_MODEL_ID).strip()
    out_items: List[Dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "")
        tid = str(item.get("transform_id") or transform_id)
        try:
            redacted = apply_text_transform_with_privacy_filter(text, tid, {})
            out_items.append({"id": item_id, "text": redacted})
        except Exception as exc:  # noqa: BLE001
            out_items.append({"id": item_id, "text": text, "error": str(exc)})
    return {
        "items": out_items,
        "model": model_id,
        "privacy_layer_version": PRIVACY_LAYER_VERSION,
        "provider": "huggingface",
        "status": "ok",
    }
