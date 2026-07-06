"""Orchestration for the Enrichment Lab (create groups, sample node data, apply winner)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set

from ..core.state import get_db_connection
from . import bundles as bundles_mod
from . import store
from . import worker

logger = logging.getLogger("topos.enrichment_lab.service")

_background_tasks: Set[asyncio.Task[Any]] = set()

MAX_MODELS_PER_GROUP = 4
MAX_NODE_SAMPLE = 25
DEFAULT_NODE_SAMPLE = 8

_HF_REPO_RE = re.compile(r"^[\w.\-]+/[\w.\-]+$")


def schedule_process_job_group(group_id: str) -> None:
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


def _validate_models(job_id: str, models: List[str]) -> List[str]:
    from ..enrichment.catalog import get_catalog_entry

    entry = get_catalog_entry(job_id)
    clean = [str(m).strip() for m in models if str(m).strip()]
    if not clean:
        clean = [worker.DEFAULT_MODEL_TAG]
    if len(clean) > MAX_MODELS_PER_GROUP:
        raise ValueError(f"At most {MAX_MODELS_PER_GROUP} models per lab run")
    for tag in clean:
        provider, model = worker.parse_model_tag(
            tag, default_provider=entry.default_provider if entry else None
        )
        if provider == "huggingface" and model and not _HF_REPO_RE.match(model):
            raise ValueError(
                f"Invalid HuggingFace model id {model!r}; expected 'org/model-name'"
            )
    return clean


def sample_node_records(source_id: str, limit: int = DEFAULT_NODE_SAMPLE) -> List[Dict[str, Any]]:
    """Read-only sample of a source's canonical records for lab input preview.

    Content is truncated for display/storage; nothing is written to node data.
    """
    from ..ingestion.canonical_pipeline import load_canonical_records_for_signal
    from ..sources.registry import REGISTRY

    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database not available")
    limit = max(1, min(int(limit or DEFAULT_NODE_SAMPLE), MAX_NODE_SAMPLE))
    records = load_canonical_records_for_signal(conn, source_def, limit=limit)
    out: List[Dict[str, Any]] = []
    for record in records[:limit]:
        rid = (
            record.get("message_id")
            or record.get("record_id")
            or record.get("event_id")
            or record.get("entry_id")
            or record.get("transaction_id")
            or record.get("contact_id")
        )
        if not rid:
            continue
        body = str(
            record.get("content") or record.get("title") or record.get("description") or ""
        )[:4000]
        item: Dict[str, Any] = {"id": str(rid), "body": body, "source_id": source_id}
        if record.get("url"):
            item["url"] = record.get("url")
        if record.get("title"):
            item["title"] = record.get("title")
        out.append(item)
    return out


def create_job_group(
    *,
    job_id: str,
    models: List[str],
    dataset_kind: str = "bundle",
    bundle_id: Optional[str] = None,
    source_id: Optional[str] = None,
    sample_limit: Optional[int] = None,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    from ..enrichment.catalog import get_catalog_entry

    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database not available")

    job_key = str(job_id or "").strip()
    entry = get_catalog_entry(job_key)
    if not entry:
        raise ValueError(f"Unknown enrichment job: {job_key!r}")
    if not entry.supports_lab:
        raise ValueError(f"Enrichment {job_key!r} is not runnable in the Lab")

    clean_models = _validate_models(job_key, models)

    kind = str(dataset_kind or "bundle").strip()
    record_inputs: Dict[str, Dict[str, Any]] = {}
    bundle_version: Optional[str] = None

    if kind == "bundle":
        bundle = bundles_mod.get_bundle(str(bundle_id or "").strip())
        if not bundle:
            raise ValueError(f"Unknown bundle_id: {bundle_id!r}")
        if not bundles_mod.is_bundle_compatible_with_job(bundle, job_key):
            raise ValueError("Bundle is not compatible with this enrichment")
        bundle_version = str(bundle["bundle_version"])
        for record in bundle["records"]:
            record_inputs[str(record["id"])] = {
                k: v for k, v in record.items() if k != "id"
            }
    elif kind == "node":
        src = str(source_id or "").strip()
        if not src:
            raise ValueError("source_id required for node dataset")
        sampled = sample_node_records(src, limit=sample_limit or DEFAULT_NODE_SAMPLE)
        if not sampled:
            raise ValueError(f"No canonical records found for source {src!r}")
        for record in sampled:
            record_inputs[str(record["id"])] = {k: v for k, v in record.items() if k != "id"}
    else:
        raise ValueError("dataset_kind must be 'bundle' or 'node'")

    if not record_inputs:
        raise ValueError("No records to run")

    gid = store.insert_group(
        conn,
        job_id=job_key,
        dataset_kind=kind,
        models=clean_models,
        record_inputs=record_inputs,
        bundle_id=bundle_id if kind == "bundle" else None,
        bundle_version=bundle_version,
        source_id=source_id if kind == "node" else None,
        sample_limit=sample_limit if kind == "node" else None,
        options=options or {},
    )
    schedule_process_job_group(gid)
    return gid


def apply_preferred_model(group_id: str) -> Dict[str, Any]:
    """Persist the group's preferred model as the device override for its job."""
    from ..enrichment.catalog import get_catalog_entry
    from ..enrichment.model_overrides import list_model_overrides, set_model_override

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
    model_tags = {dict(r)["model_tag"] for r in store.list_runs(conn, group_id)}
    if preferred not in model_tags:
        raise ValueError("preferred_model_tag was not part of this job group")

    job_id = str(group["job_id"])
    entry = get_catalog_entry(job_id)
    provider, model = worker.parse_model_tag(
        preferred, default_provider=entry.default_provider if entry else None
    )
    if not model:
        # "default" clears any override, restoring the registry default.
        set_model_override(job_id, "", "", conn=conn)
    else:
        set_model_override(job_id, provider or "huggingface", model, conn=conn)
    return {
        "status": "ok",
        "job_id": job_id,
        "applied_model_tag": preferred,
        "overrides": list_model_overrides(conn=conn),
    }


def serialize_job_group(conn: Any, group_id: str) -> Dict[str, Any]:
    row = store.get_group(conn, group_id)
    if not row:
        raise KeyError(group_id)
    group = dict(row)
    group["models"] = json.loads(group.pop("models_json") or "[]")
    opt_raw = group.pop("options_json", "{}")
    try:
        group["options"] = json.loads(opt_raw) if isinstance(opt_raw, str) else {}
    except json.JSONDecodeError:
        group["options"] = {}
    runs_out: List[Dict[str, Any]] = []
    for r in store.list_runs(conn, group_id):
        rd = dict(r)
        for json_field in ("input_json", "output_json", "metrics_json"):
            raw = rd.pop(json_field, None)
            key = json_field.replace("_json", "")
            try:
                rd[key] = json.loads(raw) if isinstance(raw, str) and raw else None
            except json.JSONDecodeError:
                rd[key] = None
        ul = rd.get("user_liked")
        rd["user_liked"] = True if ul == 1 else False if ul == 0 else None
        runs_out.append(rd)
    return {"group": group, "runs": runs_out}


def enrich_job_groups_list_with_run_summaries(conn: Any, groups: List[Dict[str, Any]]) -> None:
    if not groups:
        return
    gids = [str(g["id"]) for g in groups if g.get("id")]
    summaries = store.history_summaries_for_group_ids(conn, gids)
    for g in groups:
        g["history_summary"] = summaries.get(str(g.get("id") or ""), {})
