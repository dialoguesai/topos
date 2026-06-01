"""Orchestration for Filter Lab (create job, apply winner, helpers)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set

from topos.config.sanitization_ollama import (
    ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE,
    SANITIZATION_OLLAMA_TRANSFORM_IDS,
    normalize_put_device_overrides,
    resolve_sanitization_ollama_effective,
)
from topos.config.settings import settings
from topos.core.state import get_db_connection, get_engine_config_value, set_engine_config_value
from topos.engine.backends.ollama import OllamaAdapter

from . import bundles as bundles_mod
from . import store
from . import worker

logger = logging.getLogger("topos.filter_lab.service")

_background_tasks: Set[asyncio.Task[Any]] = set()


def schedule_process_job_group(group_id: str) -> None:
    """Run worker in a background asyncio task."""

    async def _run() -> None:
        await asyncio.to_thread(worker.process_job_group_sync, group_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        worker.process_job_group_sync(group_id)
        return
    task = loop.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def create_job_group(
    *,
    filter_id: str,
    bundle_id: str,
    models: List[str],
    options: Optional[Dict[str, Any]] = None,
) -> str:
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database not available")

    if filter_id not in SANITIZATION_OLLAMA_TRANSFORM_IDS:
        raise ValueError(f"filter_id {filter_id!r} is not runnable in Filter Lab (Ollama sanitization)")

    bundle = bundles_mod.get_bundle(bundle_id)
    if not bundle:
        raise ValueError(f"Unknown bundle_id: {bundle_id!r}")

    if not bundles_mod.is_bundle_compatible_with_filter(bundle, filter_id):
        raise ValueError("Bundle is not compatible with this filter")

    clean_models = [str(m).strip() for m in models if str(m).strip()]
    if not clean_models:
        raise ValueError("models must contain at least one model tag")

    eff = resolve_sanitization_ollama_effective(settings, conn)
    adapter = OllamaAdapter(base_url=eff.host)
    baseline = adapter.list_models()

    record_ids = bundles_mod.bundle_record_ids(bundle)
    if not record_ids:
        raise ValueError("Bundle has no records")

    gid = store.insert_group(
        conn,
        filter_id=filter_id,
        bundle_id=bundle_id,
        bundle_version=str(bundle["bundle_version"]),
        baseline_models=baseline,
        models=clean_models,
        record_ids=record_ids,
        options=options or {},
    )
    schedule_process_job_group(gid)
    return gid


def apply_preferred_model(group_id: str) -> Dict[str, Any]:
    """Merge group's preferred_model_tag into device sanitization models for filter_id."""
    from topos.config.sanitization_ollama import effective_config_for_api

    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database not available")

    row = store.get_group(conn, group_id)
    if not row:
        raise ValueError("Job group not found")
    group = dict(row)
    preferred = (group.get("preferred_model_tag") or "").strip()
    if not preferred:
        raise ValueError("preferred_model_tag is not set on this job group")

    filter_id = group["filter_id"]
    model_tags_in_group = {dict(r)["model_tag"] for r in store.list_runs(conn, group_id)}
    if preferred not in model_tags_in_group:
        raise ValueError("preferred_model_tag was not part of this job group")

    raw = get_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE) or "{}"
    try:
        existing = json.loads(raw)
    except json.JSONDecodeError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    merged: Dict[str, Any] = {"version": int(existing.get("version") or 1)}
    for k in ("enabled", "host", "default_model", "timeout_sec", "max_input_chars"):
        if k in existing and existing[k] is not None:
            merged[k] = existing[k]
    models = dict(existing.get("models") or {}) if isinstance(existing.get("models"), dict) else {}
    models[filter_id] = preferred
    merged["models"] = models

    json_str = normalize_put_device_overrides({"device_overrides": merged})
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, json_str)
    return {"status": "ok", **effective_config_for_api(settings, conn)}


def enrich_job_groups_list_with_run_summaries(conn: Any, groups: List[Dict[str, Any]]) -> None:
    """Attach per-group run aggregates for GET /job-groups list (models, latency, liked, rating)."""
    if not groups:
        return
    gids = [str(g["id"]) for g in groups if g.get("id")]
    summaries = store.history_summaries_for_group_ids(conn, gids)
    for g in groups:
        gid = str(g.get("id") or "")
        g["history_summary"] = summaries.get(gid, store.empty_history_summary())


def serialize_job_group(conn: Any, group_id: str) -> Dict[str, Any]:
    row = store.get_group(conn, group_id)
    if not row:
        raise KeyError(group_id)
    g = dict(row)
    g["baseline_models"] = json.loads(g.pop("baseline_models_json") or "[]")
    g["pulled_models"] = json.loads(g.pop("pulled_models_json") or "[]")
    opt_raw = g.pop("options_json", "{}")
    try:
        g["options"] = json.loads(opt_raw) if isinstance(opt_raw, str) else {}
    except json.JSONDecodeError:
        g["options"] = {}
    runs_out = []
    for r in store.list_runs(conn, group_id):
        rd = dict(r)
        ul = rd.get("user_liked")
        if ul == 1:
            rd["user_liked"] = True
        elif ul == 0:
            rd["user_liked"] = False
        else:
            rd["user_liked"] = None
        runs_out.append(rd)
    return {"group": g, "runs": runs_out}
