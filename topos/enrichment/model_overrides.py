"""Per-job enrichment model overrides (device-level engine config).

The Enrichment Lab's "apply preferred" writes a per-job override here; the
production jobs pick it up through ``run_engine_task`` whenever a task does
not explicitly request a model. Stored as engine config JSON:

    {"emo_27": {"provider": "huggingface", "model": "SamLowe/roberta-base-go_emotions"}}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("topos.enrichment.model_overrides")

ENGINE_CONFIG_KEY_ENRICHMENT_MODELS = "enrichment_models_device"

# Engine task subtype -> catalog job id, so the single run_engine_task choke
# point can resolve overrides without changing every job call site.
SUBTYPE_TO_JOB: Dict[str, str] = {
    "emotion_classification_batch": "emo_27",
    "emotion_classification": "emo_27",
    "embedding": "embeddings",
    "sentiment_classification": "sentiment",
    # Both forms, deliberately. entities_job emits the _batch subtype, and for
    # months only the bare form was mapped — so a Lab "apply preferred" for
    # entities was stored, displayed, and silently never applied. emo_27 had
    # already hit the same hole and was patched to carry both; entities was
    # missed. tests/enrichment/test_model_override_subtypes.py now derives the
    # required entries from the jobs' own run_engine_task calls, so a job
    # renaming its subtype fails a test instead of orphaning its override.
    "entity_extraction_batch": "entities",
    "entity_extraction": "entities",
    "topic_extraction": "topics",
    "goal_extraction": "goal_extraction",
}


def _load_overrides(conn) -> Dict[str, Dict[str, Any]]:
    from ..core.state import get_engine_config_value

    raw = get_engine_config_value(conn, ENGINE_CONFIG_KEY_ENRICHMENT_MODELS) or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def get_model_override(job_id: str, conn=None) -> Optional[Dict[str, str]]:
    """Override {provider, model} for a job, or None."""
    try:
        if conn is None:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        if conn is None:
            return None
        entry = _load_overrides(conn).get(str(job_id or "").strip())
        if isinstance(entry, dict) and str(entry.get("model") or "").strip():
            return {
                "provider": str(entry.get("provider") or "").strip() or "huggingface",
                "model": str(entry["model"]).strip(),
            }
    except Exception as exc:  # noqa: BLE001 — overrides must never break enrichment
        logger.debug("model override lookup failed for %s: %s", job_id, exc)
    return None


def get_override_for_subtype(subtype: Optional[str], conn=None) -> Optional[Dict[str, str]]:
    job_id = SUBTYPE_TO_JOB.get(str(subtype or "").strip())
    if not job_id:
        return None
    return get_model_override(job_id, conn=conn)


def set_model_override(job_id: str, provider: str, model: str, conn=None) -> Dict[str, Any]:
    """Persist an override for a job; returns the full override map."""
    from ..core.state import get_db_connection, set_engine_config_value

    if conn is None:
        conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database connection not available")
    overrides = _load_overrides(conn)
    job_key = str(job_id or "").strip()
    if not job_key:
        raise ValueError("job_id required")
    model_clean = str(model or "").strip()
    if model_clean:
        overrides[job_key] = {
            "provider": str(provider or "").strip() or "huggingface",
            "model": model_clean,
        }
    else:
        overrides.pop(job_key, None)
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_ENRICHMENT_MODELS, json.dumps(overrides))
    return overrides


def list_model_overrides(conn=None) -> Dict[str, Dict[str, Any]]:
    from ..core.state import get_db_connection

    if conn is None:
        conn = get_db_connection()
    if conn is None:
        return {}
    return _load_overrides(conn)
