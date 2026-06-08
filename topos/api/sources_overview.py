from __future__ import annotations

from typing import Any, Dict, Set

from .enrichment import _get_enrichment_status_core, _list_source_enrichments_core
from .source_install import _list_sources_core, _scope_from_payload
from ..core.state import get_db_connection
from ..storage.source_settings import get_source_settings

DEFAULT_INCLUDE = ("enrichments", "enrichment_status", "settings")


def _parse_include(raw: Any) -> Set[str]:
    if isinstance(raw, str):
        return {part.strip() for part in raw.split(",") if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set(DEFAULT_INCLUDE)


async def _get_sources_overview_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = await _list_sources_core(payload)
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    include = _parse_include(payload.get("include"))

    scope = _scope_from_payload(payload)
    dataset_id = str(scope.get("dataset_id") or "").strip()
    metadata_by_source_id: Dict[str, Dict[str, Any]] = {}
    conn = get_db_connection()

    for source_def in sources:
        if not isinstance(source_def, dict):
            continue
        source_id = str(source_def.get("source_id") or "").strip()
        if not source_id:
            continue
        meta: Dict[str, Any] = {}
        if "enrichment_status" in include and source_def.get("enrichment_trigger") == "manual":
            try:
                meta["enrichment_status"] = await _get_enrichment_status_core(source_id, dataset_id)
            except Exception:
                pass
        if "settings" in include and source_def.get("source_type") == "local_sync" and conn and dataset_id:
            settings_row = get_source_settings(conn, dataset_id, source_id)
            if settings_row is not None:
                meta["settings"] = settings_row
        if "enrichments" in include:
            try:
                meta["enrichments"] = _list_source_enrichments_core(source_id)
            except ValueError:
                pass
        if meta:
            metadata_by_source_id[source_id] = meta

    return {
        "status": "ok",
        "sources": sources,
        "metadata_by_source_id": metadata_by_source_id,
    }
