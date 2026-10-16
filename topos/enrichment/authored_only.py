"""Which rows an enrichment job reads: a source's ``authored_only_jobs`` (owner ruling, 16 Oct 2026).

A source definition may name jobs that read only what the owner typed. Such a job, in either lane, is handed the
rows whose record role is ``authored`` (``features/provenance/roles.py``, the one answer to "whose words does this
row hold") and never sees the rest. Every other job, and every row of a source that names none, is unchanged. The
coding-session sources name entities, topics and goal_extraction: the AI's replies land, get embeddings and stay
searchable, but mint no entity, topic or goal.

Applied where a job is handed its rows (``EnrichmentOrchestrator.run_canonical``,
``SignalDerivationOrchestrator.run_signal_derivation``), which every path reaches: the sync lanes' post-canonical
pipeline, the manual-process and backfill doors, reprocess, the derivation-debt retry. The manual-process door's
catch-up read uses it too, so a row a job may not read is never counted as still owed to that job.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def _source_of(record: Any, default: Optional[str]) -> str:
    own = record.get("source_id") if isinstance(record, dict) else None
    return str(own or default or "").strip()


def rows_for_job(job_name: str, records: Sequence[Dict[str, Any]], *,
                 source_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The records ``job_name`` may read, in order. ``source_id`` stands for a record that carries none.

    Typed means ``record_role == 'authored'`` with the row's table (its ``_table`` stamp, else its source's
    canonical table: a reloaded row carries no stamp, and without one an owner row reads as someone else's) and
    no posture: the question is who typed the row, and goal_extraction applies the owner's posture itself.
    """
    rows = list(records or [])
    from ..sources.registry import REGISTRY

    gates: Dict[str, Any] = {}
    for record in rows:
        sid = _source_of(record, source_id)
        if sid not in gates:
            definition = REGISTRY.get(sid) if sid else None
            listed = getattr(definition, "authored_only_jobs", None) or ()
            gates[sid] = definition if job_name in listed else None
    if not any(gates.values()):
        return rows

    from ..disclosure.field_registry import canonical_table_for_group
    from ..features.provenance.roles import ROLE_AUTHORED, record_role

    kept: List[Dict[str, Any]] = []
    for record in rows:
        definition = gates.get(_source_of(record, source_id))
        if definition is None:
            kept.append(record)
            continue
        if not isinstance(record, dict):
            continue  # no row to read a role from: not provably typed
        table =str(record.get("_table") or canonical_table_for_group(definition.canonical_group_id) or "")
        if record_role(record, table=table) == ROLE_AUTHORED:
            kept.append(record)
    return kept


__all__ = ["rows_for_job"]
