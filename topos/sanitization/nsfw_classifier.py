"""Ingest-time NSFW tagging for the Platform Privacy Layer (tag, do not sanitize): the explicit-wording rule.

The text classifier this module was named for (``michellejieli/NSFW_text_classifier``) is retired. Measured on a
copy of one owner's database it could not separate explicit from ordinary text: invented explicit sentences and an
invented sentence about dinner scored alike near its 0.97 ceiling, it flagged 41% of journal entries, 37% of
messages and 62% of AI-chat rows where it ran, it read only the first 512 characters, and it never ran at all on
most iMessage rows. The decision is now :mod:`topos.sanitization.explicit_wording`, a deterministic rule over the
whole text, and no model is loaded for it: not at startup, not at ingest, not on the engine.

This module keeps the names the pipeline, the engine task ``content_nsfw_classification`` and the HTTP route
``/v1/privacy/nsfw-classify`` call, so a node of either age talking to an engine of the other gets the rule's
answer. The one setting that still means something is ``nsfw_classifier_enabled`` (off: nothing is tagged); the
retired classifier's model, cutoff and input-cap settings are gone, and an environment that still sets them is
ignored (``Settings`` ignores unknown names).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Final, List, Tuple

from .explicit_wording import RULE_ID, evaluate

logger = logging.getLogger("topos.sanitization.nsfw_classifier")

#: The id written to ``content_nsfw_model`` for every tag the rule decides.
NSFW_TAGGER_ID: Final[str] = RULE_ID
NSFW_CLASSIFY_MAX_BATCH: Final[int] = 32
#: ``content_nsfw_score`` by tier: unambiguous vocabulary, a phrase around an ambiguous word, no hit.
TIER_SCORES: Final[Dict[Any, float]] = {"unambiguous": 1.0, "phrase": 0.5, None: 0.0}


def nsfw_classifier_enabled() -> bool:
    from topos.config.settings import settings

    return bool(getattr(settings, "nsfw_classifier_enabled", True))


def classify_nsfw_text(text: str) -> Tuple[bool, float, str]:
    """Return (is_nsfw, score, label): the rule's verdict, its tier as the score, the tier (or "none") as the
    label. Empty text and a disabled tagger answer (False, 0.0, "empty" | "disabled")."""
    if not text or not str(text).strip():
        return False, 0.0, "empty"
    if not nsfw_classifier_enabled():
        return False, 0.0, "disabled"
    verdict = evaluate(str(text))
    return verdict.flagged, TIER_SCORES[verdict.tier], verdict.tier or "none"


def classify_nsfw_batch(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Batch NSFW tagging for Engine tasks and the HTTP API. ``model`` names the rule, never a model."""
    if not nsfw_classifier_enabled():
        return {
            "status": "disabled",
            "items": [{"id": str(i.get("id") or ""), "nsfw": False, "score": 0.0, "label": "disabled"} for i in items],
            "model": NSFW_TAGGER_ID,
        }
    if len(items) > NSFW_CLASSIFY_MAX_BATCH:
        return {
            "status": "too_large",
            "error": f"batch exceeds limit of {NSFW_CLASSIFY_MAX_BATCH}",
            "items": [],
            "model": NSFW_TAGGER_ID,
        }
    out_items: List[Dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "")
        is_nsfw, score, label = classify_nsfw_text(text)
        out_items.append({"id": item_id, "nsfw": bool(is_nsfw), "score": score, "label": label})
    return {"status": "ok", "items": out_items, "model": NSFW_TAGGER_ID, "provider": "rule"}
