from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ._batch_limits import (
    DIMENSION_SUMMARY_MAX_RECORDS,
    DIMENSION_SUMMARY_MAX_TOTAL_CHARS,
    DIMENSION_SUMMARY_RECORD_CHARS,
    MAX_JOB_MESSAGES,
)
from ._engine_runner import run_engine_task
from .brief_fallback import prepare_brief_record, rules_brief_sections, _structured_journal_place_band
from ....engine import Engine
from ....config.signal_extraction import get_signal_extraction_model_request, get_signal_extraction_provider
from ....core.state import get_db_connection
from ....features.signal.dimension_briefs import DimensionBriefStore
from ....features.signal.brief_ontology import llm_merge_section_ids
from ....features.signal.brief_schemas import llm_prompt_for_dimension
from ....features.signal.dimension_registry import dimensions_for_brief_update
from ....features.signal.brief_canonical_loader import load_canonical_messages_for_dimension
from ....features.signal.structured_signal import (
    enrich_structured_batch,
    should_update_narrative_brief,
    structured_signal_enabled,
)

logger = logging.getLogger("topos.enrichment.jobs.dimension_summary")


def _truncate_records_for_summary(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    total_chars = 0
    for record in records[:DIMENSION_SUMMARY_MAX_RECORDS]:
        content = str(record.get("brief_input") or record.get("content") or record)[:DIMENSION_SUMMARY_RECORD_CHARS]
        if total_chars + len(content) > DIMENSION_SUMMARY_MAX_TOTAL_CHARS:
            remaining = DIMENSION_SUMMARY_MAX_TOTAL_CHARS - total_chars
            if remaining <= 0:
                break
            content = content[:remaining]
        total_chars += len(content)
        row = dict(record) if isinstance(record, dict) else {}
        row["content"] = content
        out.append(row)
    return out


def _records_for_brief_dimension(
    dimension: str,
    batch: List[Dict[str, Any]],
    *,
    fallback_source_id: Optional[str],
    conn,
) -> List[Dict[str, Any]]:
    """Select ingest-batch rows relevant to one dimension; fall back to DB loader."""
    dim = str(dimension or "").strip().lower()
    matched: List[Dict[str, Any]] = []
    for rec in batch:
        sid = str(rec.get("source_id") or fallback_source_id or "")
        brief_dims = dimensions_for_brief_update(source_id=sid)
        if dim not in brief_dims:
            continue
        if dim == "places" and not _structured_journal_place_band(rec):
            continue
        matched.append(rec)
    if matched:
        return matched
    if conn is not None:
        loaded = load_canonical_messages_for_dimension(conn, dim, source_id=fallback_source_id)
        if loaded:
            return [prepare_brief_record(r) for r in loaded]
    return []


class DimensionSummaryJob(BaseEnrichmentJob):
    """LLM-backed dimension brief updater (living document per signal dimension)."""

    def __init__(self, *, name: Optional[str] = None, engine: Optional[Engine] = None):
        super().__init__(name=name)
        self._engine = engine or Engine()

    def get_derived_table(self) -> str:
        return "signal_dimension_briefs"

    def get_job_name(self) -> str:
        return "dimension_summary"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        only_dimension: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not canonical_messages:
            if progress_callback:
                progress_callback(0, 0)
            return []

        capped = canonical_messages[:MAX_JOB_MESSAGES]
        if len(canonical_messages) > MAX_JOB_MESSAGES:
            logger.warning(
                "DimensionSummaryJob capped input from %d to %d messages",
                len(canonical_messages),
                MAX_JOB_MESSAGES,
            )

        prepared = [prepare_brief_record(r) for r in capped]
        source_id = prepared[0].get("source_id") if prepared else capped[0].get("source_id")
        dimensions = dimensions_for_brief_update(
            source_id=str(source_id or ""),
            only_dimension=only_dimension,
        )
        if only_dimension and not dimensions:
            dim_key = str(only_dimension).strip().lower()
            return [{"_error": f"unknown_dimension:{dim_key}"}]
        conn = get_db_connection()
        if conn is None:
            return [{"_deferred": True, "error": "database_unavailable"}]

        structured_result: Dict[str, Any] = {}
        if structured_signal_enabled():
            structured_result = enrich_structured_batch(
                prepared,
                source_id=str(source_id or "") or None,
                only_dimension=only_dimension,
            )

        store = DimensionBriefStore(conn)
        updated_dims: List[str] = []

        for idx, dimension in enumerate(dimensions):
            if structured_signal_enabled() and not should_update_narrative_brief(dimension):
                if progress_callback:
                    progress_callback(idx + 1, len(dimensions))
                continue
            dim_records = _truncate_records_for_summary(
                _records_for_brief_dimension(
                    dimension,
                    prepared,
                    fallback_source_id=str(source_id or "") or None,
                    conn=conn,
                )
            )
            if not dim_records:
                continue
            section_updates: Dict[str, Optional[str]] = {}
            engine_provider, extraction_model = get_signal_extraction_model_request()
            configured_provider = get_signal_extraction_provider()
            result = await run_engine_task(
                self._engine,
                task_id=f"dim_brief_{dimension}",
                subtype="brief_update",
                source_id=source_id,
                record_ids=[],
                input_payload={
                    "dimension": dimension,
                    "records": dim_records,
                    "prompt": llm_prompt_for_dimension(dimension),
                },
                provider=engine_provider,
                model=extraction_model,
            )
            if result.status == "completed":
                output = result.output or {}
                merge_keys = llm_merge_section_ids(dimension)
                section_updates = {key: output.get(key) for key in merge_keys}
                if not any(str(v or "").strip() for v in section_updates.values()):
                    legacy = output.get("at_a_glance") or output.get("summary_text")
                    if legacy and merge_keys:
                        section_updates = {merge_keys[0]: legacy}
                provider = configured_provider
            elif result.status == "deferred":
                logger.info(
                    "DimensionSummaryJob %s deferred for %s; using rules fallback",
                    configured_provider,
                    dimension,
                )
                section_updates = rules_brief_sections(dimension, dim_records)
                provider = "rules_fallback"
            else:
                continue
            if not any(str(v or "").strip() for v in section_updates.values()):
                continue
            store.merge_system_update(
                dimension,
                section_updates,
                source_id=source_id,
                model=result.output.get("model") if result.status == "completed" and result.output else None,
                provider=provider,
            )
            updated_dims.append(dimension)
            if progress_callback:
                progress_callback(idx + 1, len(dimensions))

        return [
            {
                "_brief_updated": True,
                "dimensions": updated_dims,
                "source_id": source_id,
                "structured": structured_result,
            }
        ]
