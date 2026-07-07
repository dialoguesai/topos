"""Per-source enrichment enable/disable overrides (device-level engine config).

Source definitions declare which enrichment jobs run per lane. This module
layers runtime, user-controlled overrides on top so an enrichment can be
attached to (or detached from) a specific installed source without editing
the definition. Stored as engine config JSON:

    {"chatgpt_file_ingestion": {"goal_extraction": {"enabled": true, "lanes": ["canonical", "signal"]},
                                "dimension_summary": {"enabled": false, "lanes": ["signal"]}}}

Overrides are consulted by the central lane resolvers
(``jobs_configured_for_source``, ``resolved_signal_derivation_jobs``, the
canonical ingest pipeline, and the manual-process paths) so catalog display,
backfill gating, coverage, ingestion, and signal derivation all agree.
Lookups fail open: any storage error yields the definition defaults.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.enrichment.source_overrides")

ENGINE_CONFIG_KEY_SOURCE_ENRICHMENTS = "source_enrichment_overrides"

# Lanes a user can toggle at runtime. Raw-lane jobs are ingest-format stubs
# and stay definition-driven.
TOGGLABLE_LANES = ("canonical", "signal")


def _load_all(conn) -> Dict[str, Dict[str, Any]]:
    from ..core.state import get_engine_config_value

    raw = get_engine_config_value(conn, ENGINE_CONFIG_KEY_SOURCE_ENRICHMENTS) or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def get_source_overrides(source_id: str, conn=None) -> Dict[str, Dict[str, Any]]:
    """Override map {job_id: {enabled, lanes}} for one source (fail-open {})."""
    try:
        if conn is None:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        if conn is None:
            return {}
        entry = _load_all(conn).get(str(source_id or "").strip())
        return entry if isinstance(entry, dict) else {}
    except Exception as exc:  # noqa: BLE001 — overrides must never break resolution
        logger.debug("source enrichment override lookup failed for %s: %s", source_id, exc)
        return {}


def apply_overrides_to_jobs(
    source_id: str, lane: str, jobs: List[str], conn=None
) -> List[str]:
    """Apply per-source overrides to a lane's statically-resolved job list."""
    overrides = get_source_overrides(source_id, conn=conn)
    if not overrides:
        return jobs
    out = list(jobs)
    for job_id, override in overrides.items():
        if not isinstance(override, dict):
            continue
        lanes = override.get("lanes") or []
        if lane not in lanes:
            continue
        enabled = override.get("enabled")
        if enabled is True and job_id not in out:
            out.append(job_id)
        elif enabled is False and job_id in out:
            out.remove(job_id)
    return out


def effective_canonical_enrichment_jobs(source_def: Any, conn=None) -> List[str]:
    """Canonical-lane jobs for a source with runtime overrides applied."""
    static_jobs = list(getattr(source_def, "canonical_enrichment_jobs", []) or [])
    source_id = str(getattr(source_def, "source_id", "") or "")
    if not source_id:
        return static_jobs
    return apply_overrides_to_jobs(source_id, "canonical", static_jobs, conn=conn)


def togglable_lanes_for_job(job_id: str) -> List[str]:
    """Lanes where this job can be toggled per source (from the catalog)."""
    from .catalog import get_catalog_entry

    entry = get_catalog_entry(job_id)
    if not entry or entry.stub:
        return []
    return [lane for lane in entry.lanes if lane in TOGGLABLE_LANES]


def set_source_enrichment_override(
    source_id: str,
    job_id: str,
    enabled: Optional[bool],
    conn=None,
) -> Dict[str, Dict[str, Any]]:
    """Persist an override; ``enabled=None`` clears it (definition default).

    Returns the source's full override map after the write.
    """
    from ..core.state import get_db_connection, set_engine_config_value

    if conn is None:
        conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database connection not available")

    source_key = str(source_id or "").strip()
    job_key = str(job_id or "").strip()
    if not source_key:
        raise ValueError("source_id required")
    if not job_key:
        raise ValueError("job_id required")

    lanes = togglable_lanes_for_job(job_key)
    if enabled is not None and not lanes:
        raise ValueError(
            f"Enrichment {job_key!r} cannot be toggled per source "
            "(unknown, stub, or raw-lane only)"
        )

    all_overrides = _load_all(conn)
    source_overrides = all_overrides.get(source_key)
    if not isinstance(source_overrides, dict):
        source_overrides = {}

    if enabled is None:
        source_overrides.pop(job_key, None)
    else:
        source_overrides[job_key] = {"enabled": bool(enabled), "lanes": lanes}

    if source_overrides:
        all_overrides[source_key] = source_overrides
    else:
        all_overrides.pop(source_key, None)
    set_engine_config_value(
        conn, ENGINE_CONFIG_KEY_SOURCE_ENRICHMENTS, json.dumps(all_overrides)
    )
    return source_overrides
