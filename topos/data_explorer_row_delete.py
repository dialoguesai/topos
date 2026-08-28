"""Pipeline-aware row delete for Data Explorer."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .data_explorer_tables import CANONICAL_ROW_ID_COLUMN, is_canonical_schema_table
from .sources.scrub_attribution import (
    TableAction,
    _delete_vec_rows_batched,
    _extract_row_value,
    _list_table_columns,
    _list_user_tables,
    _safe_sql_identifier,
    _select_embedding_ids,
)
from .storage.adapters.sqlite.stores import _NATIVE_ID_COL
from .storage.db.postgres import execute_query, fetch_all, fetch_one
from .storage.db.write_gate import batched_writes

VALID_DELETE_SCOPES = frozenset({"row_only", "with_downstream", "with_upstream", "full_lineage"})


@contextmanager
def _sqlite_delete_gate(conn: Any) -> Iterator[None]:
    """batched_writes (gate + single commit at exit) for SQLite; no-op otherwise."""
    if isinstance(conn, sqlite3.Connection):
        with batched_writes(conn):
            yield
    else:
        yield

_PROTECTED_TABLES = frozenset(
    {
        "engine_config",
        "oplog",
        "projection_meta",
        "schema_meta",
        "sqlite_sequence",
        "ingestion_checkpoints",
        "ingestion_jobs",
        "ingestion_errors",
        "source_runtime_installs",
    }
)

_RECORD_LINK_COLUMNS = ("record_id", "message_id", "entry_id", "event_id", "transaction_id")
_PRIMARY_KEY_FALLBACKS = ("record_id", "id", "entry_id", "message_id", "event_id", "transaction_id")

#: Tables holding DERIVED rows — what "delete everything derived from this" means.
#:
#: Membership decides two things at once, which is why the omissions below were
#: costly. ``_delete_downstream_for_canonical`` sweeps only tables that pass
#: ``_is_enrichment_or_signal_table``, and ``_is_upstream_table`` returns False
#: for them — so a derived table missing from here is not merely unswept by
#: ``with_downstream``, it is actively MISCLASSIFIED as upstream and swept by
#: ``with_upstream`` instead. The owner gets the opposite of what each scope
#: promises.
#:
#: The misclassification is not an oversight in the list so much as a heuristic
#: that cannot tell the two apart: ``_is_upstream_table`` treats
#: ``record_id + source_id`` as an upstream signature, and every derived table
#: carries both. Only an explicit declaration separates them.
#:
#: Measured on the owner's node 2026-08-27, the five added here hold 38,700 rows
#: that "delete this row and everything derived from it" did not reach.
_ENRICHMENT_SIGNAL_TABLES = frozenset(
    {
        "message_emotions",
        "message_embeddings",
        "message_entities",
        "message_topics",
        "message_sentiment",
        "user_goals",
        "signal_tags",
        "signal_scores",
        "signal_embeddings",
        "relationship_edges",
        "graph_nodes",
        "graph_edges",
        "data_health_dimension",
        # --- added 2026-08-27, all record-linked and unambiguously derived ---
        "timeline",             # 14,724 — the record registry, rebuilt by projection
        "topic_cluster_members",#  9,934 — cluster membership, recomputed on demand
        "triage_verdicts",      #  7,957 — per-record triage output
        "entity_mentions",      #  5,830 — extraction output, rebuilt from records
        "cluster_candidates",   #     93 — clustering scratch
        "entity_review",        #    172 — review queue rows raised by extraction
    }
)

#: Record-linked tables deliberately NOT swept as derived, with the reason.
#: Kept as documentation rather than code because the cost of a wrong entry runs
#: both ways: including a source table deletes the owner's data, excluding a
#: derived one leaves it behind after they asked for it to go.
#:
#:   canonical (journal_entries, conversation_messages, activity_events, …)
#:       the record itself or a sibling of it, not something derived from it.
#:   flat source tables (grow_journal_sessions, browser_visits, browser_events,
#:   time_log_sessions, grow_data_sessions, calendar_raw_auto)
#:       the landing shape of the source, upstream of canonical. No `raw_` prefix
#:       and no `source_system` column, so the heuristic misses them — but they
#:       are the owner's ingested data, not a restatement of it.
#:   the full-text and ANN companion stores behind signal_embeddings
#:       maintained by triggers and by the VectorIndex seam. A direct DELETE on
#:       either corrupts the index instead of updating it, which is why this
#:       module must not name their physical tables at all
#:       (tests/storage/test_vector_index_seam.py enforces that).
#:   signal_objects, graph_nodes/graph_edges provenance
#:       BLOCKED, not excluded. signal_objects carries provenance only in
#:       source_refs_json under two incompatible key schemas, and graph edges
#:       carry no record provenance at all (0 of 3,826). Neither can be swept by
#:       record until that is unified — see the C lane.
#:   llm_usage_events, stat_seen, *_lab_run, derivation_training_ledger,
#:   query_audit_events
#:       audit, billing and fold-dedupe bookkeeping. Deleting stat_seen in
#:       particular would let a later refold double-count. Judgment call, left
#:       out pending an explicit decision.


@dataclass(frozen=True)
class LineageAnchor:
    anchor_table: str
    anchor_row_id: str
    canonical_table: Optional[str]
    canonical_id: Optional[str]
    source_id: Optional[str]
    source_record_id: Optional[str]
    #: Set when this canonical row is a FAN-OUT CHILD: the canonical row it was
    #: split out of. Deliberately not folded into ``source_record_id`` — see
    #: ``_parent_canonical_row``. Reported, never deleted on: expanding a delete
    #: across the split is a separate, named scope, not a side effect of
    #: resolving lineage.
    parent_canonical_table: Optional[str] = None
    parent_canonical_id: Optional[str] = None


@dataclass
class RowDeleteResult:
    scope: str
    table_name: str
    row_ids: List[str]
    rows_deleted: int = 0
    table_actions: List[TableAction] = field(default_factory=list)
    #: Canonical rows a deleted row was FANNED OUT OF, kept rather than deleted.
    #: Surfaced so the caller can say "this row was split out of
    #: journal_entries/tl-1, which was left in place" instead of reporting a
    #: partial delete as a complete one.
    parents_retained: List[Dict[str, str]] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "table_name": self.table_name,
            "row_ids": list(self.row_ids),
            "rows_deleted": int(self.rows_deleted),
            "table_actions": [
                {"table": item.table, "action": item.action, "count": int(item.count)}
                for item in self.table_actions
            ],
            "parents_retained": [dict(item) for item in self.parents_retained],
        }


def _is_enrichment_or_signal_table(table_name: str) -> bool:
    if table_name in _ENRICHMENT_SIGNAL_TABLES:
        return True
    if table_name.startswith("signal_") or table_name.startswith("message_"):
        return True
    return False


def _is_upstream_table(table_name: str, columns: Set[str]) -> bool:
    if is_canonical_schema_table(table_name) or _is_enrichment_or_signal_table(table_name):
        return False
    if table_name.startswith("raw_"):
        return True
    if "source_system" in columns:
        return True
    if "record_id" in columns and ("source_id" in columns or "source_system" in columns):
        return True
    return False


def resolve_primary_key_column(conn: Any, table_name: str) -> Optional[str]:
    if not _safe_sql_identifier(table_name):
        return None
    known = _NATIVE_ID_COL.get(table_name)
    if known:
        return known
    columns = set(_list_table_columns(conn, table_name))
    for candidate in _PRIMARY_KEY_FALLBACKS:
        if candidate in columns:
            return candidate
    return None


def _row_to_dict(row: Any, columns: Sequence[str]) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {col: _extract_row_value(row, idx, col) for idx, col in enumerate(columns)}


def _fetch_row(conn: Any, table_name: str, pk_column: str, row_id: str) -> Optional[Dict[str, Any]]:
    columns = _list_table_columns(conn, table_name)
    if pk_column not in columns:
        return None
    row = fetch_one(
        conn,
        f'SELECT * FROM "{table_name}" WHERE "{pk_column}" = %s LIMIT 1',
        (row_id,),
    )
    if row is None:
        return None
    return _row_to_dict(row, columns)


def _lookup_mapping(
    conn: Any,
    *,
    source_id: str,
    source_record_id: str,
) -> Optional[Tuple[str, str]]:
    if not _table_exists(conn, "canonical_source_mappings"):
        return None
    row = fetch_one(
        conn,
        """
        SELECT canonical_table, canonical_id
        FROM canonical_source_mappings
        WHERE source_id = %s AND source_record_id = %s
        LIMIT 1
        """,
        (source_id, source_record_id),
    )
    if row is None:
        return None
    canonical_table = str(_extract_row_value(row, 0, "canonical_table") or "").strip()
    canonical_id = str(_extract_row_value(row, 1, "canonical_id") or "").strip()
    if not canonical_table or not canonical_id:
        return None
    return canonical_table, canonical_id


def _lookup_mapping_by_canonical(
    conn: Any,
    *,
    canonical_table: str,
    canonical_id: str,
) -> Optional[Tuple[str, str]]:
    if not _table_exists(conn, "canonical_source_mappings"):
        return None
    row = fetch_one(
        conn,
        """
        SELECT source_id, source_record_id
        FROM canonical_source_mappings
        WHERE canonical_table = %s AND canonical_id = %s
        LIMIT 1
        """,
        (canonical_table, canonical_id),
    )
    if row is None:
        return None
    source_id = str(_extract_row_value(row, 0, "source_id") or "").strip()
    source_record_id = str(_extract_row_value(row, 1, "source_record_id") or "").strip()
    if not source_id or not source_record_id:
        return None
    return source_id, source_record_id


def _table_exists(conn: Any, table_name: str) -> bool:
    return table_name in set(_list_user_tables(conn))


def _parent_canonical_row(
    conn: Any,
    *,
    table_name: str,
    row_id: str,
    source_record_id: Optional[str],
    source_id: Optional[str],
) -> Optional[tuple]:
    """``(table, id)`` of the canonical row this one was fanned out of, if any.

    ``source_record_id`` carries two incompatible meanings today. On an ordinary
    canonical row it is what the name says — the id of the record in the SOURCE
    system. But a fan-out child writes its PARENT's canonical id there instead:
    every one of the 362 ``location_events`` rows minted by
    ``journal_location_fanout`` points at a real ``journal_entries.entry_id``.

    Measured on the live node 2026-08-27, ``journal_entries`` splits three ways:
    369 rows are self-referential (``source_record_id = entry_id``, the
    grow_data_file and grow_journal lanes) and 121 carry a genuine external
    ``github_activity`` key (``push:{repo}:{sha}:{sha}``) that resolves to no
    canonical row at all. Both fall through this probe, which is what it is for —
    the rule keys on "resolves to another canonical row", not on "differs".

    That difference is load-bearing, because ``_delete_upstream_rows`` treats
    ``source_record_id`` as an upstream key and deletes ``WHERE record_id = ?``
    across every upstream table. For a fan-out child that key belongs to a
    sibling canonical row, so deleting one place event stripped 1,073 rows off
    the journal entry it came from — its flat source row, timeline entries,
    entity mentions, triage verdict and cluster membership — while the journal
    entry itself survived, invisible to retrieval, the graph and the timeline.

    The discrimination is exact rather than heuristic: a value that differs from
    the row's own id AND resolves to a canonical row that is not this one is a
    parent pointer, because a source-system id has no reason to be another
    canonical row's primary key. Self-referential values and values that match
    nothing both fall through.

    Two refinements the first version got wrong:

    * the match is constrained to the SAME ``source_id``. Ids are only unique
      within a source, so without it a collision across two connectors would
      silently re-anchor — and re-anchoring NARROWS a legitimate upstream delete,
      which fails quietly rather than loudly.
    * the candidate set no longer excludes the row's own table. A declared
      ``fan_out`` mints its children into whatever table the declaration names,
      including the parent's own — the ``canonical_field_map`` docstring's GitHub
      example fans commits into ``journal_entries``, the same table the base row
      lands in. The ``source_record_id == row_id`` guard above is what keeps a row
      from being its own parent.
    """
    if not source_record_id or source_record_id == row_id:
        return None
    for candidate_table, id_col in CANONICAL_ROW_ID_COLUMN.items():
        if not _table_exists(conn, candidate_table):
            continue
        columns = set(_list_table_columns(conn, candidate_table))
        if id_col not in columns:
            continue
        sql = f'SELECT 1 FROM "{candidate_table}" WHERE "{id_col}" = %s'
        params: List[Any] = [source_record_id]
        if source_id and "source_id" in columns:
            sql += " AND source_id = %s"
            params.append(source_id)
        if fetch_one(conn, sql + " LIMIT 1", tuple(params)) is not None:
            return candidate_table, source_record_id
    return None


def _resolve_lineage(
    conn: Any,
    *,
    table_name: str,
    pk_column: str,
    row_id: str,
    row: Dict[str, Any],
    probe_parent: bool = True,
) -> LineageAnchor:
    source_id = str(row.get("source_id") or row.get("source_system") or "").strip() or None
    source_record_id = str(row.get("source_record_id") or row.get("record_id") or "").strip() or None
    canonical_table: Optional[str] = None
    canonical_id: Optional[str] = None
    parent_table: Optional[str] = None
    parent_id: Optional[str] = None

    if is_canonical_schema_table(table_name):
        canonical_table = table_name
        canonical_id = row_id
        # The probe only changes the UPSTREAM anchor, so a row_only delete pays
        # nothing for it: it scans every canonical table per row, which on a
        # 50-row selection was measured at hundreds of extra statements for a
        # result that is discarded.
        parent = (
            _parent_canonical_row(
                conn,
                table_name=table_name,
                row_id=row_id,
                source_record_id=source_record_id,
                source_id=source_id,
            )
            if probe_parent
            else None
        )
        if parent:
            # A parent pointer is not an upstream key. Re-anchor the upstream
            # sweep on this row's OWN id so it can only ever reach rows that
            # belong to it, and carry the parent separately for reporting.
            parent_table, parent_id = parent
            source_record_id = row_id
        if not source_record_id:
            source_record_id = row_id
        if not source_id:
            mapped = _lookup_mapping_by_canonical(
                conn,
                canonical_table=table_name,
                canonical_id=row_id,
            )
            if mapped:
                source_id, mapped_source_record_id = mapped
                source_record_id = source_record_id or mapped_source_record_id
    elif source_id and source_record_id:
        mapped = _lookup_mapping(source_id=source_id, source_record_id=source_record_id, conn=conn)
        if mapped:
            canonical_table, canonical_id = mapped
        else:
            for candidate_table, id_col in _NATIVE_ID_COL.items():
                if not _table_exists(conn, candidate_table):
                    continue
                exists = fetch_one(
                    conn,
                    f'SELECT 1 FROM "{candidate_table}" WHERE "{id_col}" = %s AND source_id = %s LIMIT 1',
                    (source_record_id, source_id),
                )
                if exists is not None:
                    canonical_table = candidate_table
                    canonical_id = source_record_id
                    break
    else:
        link_id = str(
            row.get("record_id")
            or row.get("message_id")
            or row.get("entry_id")
            or row.get("event_id")
            or row_id
        ).strip()
        if link_id:
            canonical_id = link_id
            for candidate_table, id_col in _NATIVE_ID_COL.items():
                if not _table_exists(conn, candidate_table):
                    continue
                exists = fetch_one(
                    conn,
                    f'SELECT 1 FROM "{candidate_table}" WHERE "{id_col}" = %s LIMIT 1',
                    (link_id,),
                )
                if exists is not None:
                    canonical_table = candidate_table
                    break

    if canonical_table and canonical_id and (not source_id or not source_record_id):
        mapped = _lookup_mapping_by_canonical(
            conn,
            canonical_table=canonical_table,
            canonical_id=canonical_id,
        )
        if mapped:
            source_id = source_id or mapped[0]
            source_record_id = source_record_id or mapped[1]

    return LineageAnchor(
        anchor_table=table_name,
        anchor_row_id=row_id,
        canonical_table=canonical_table,
        canonical_id=canonical_id,
        source_id=source_id,
        source_record_id=source_record_id,
        parent_canonical_table=parent_table,
        parent_canonical_id=parent_id,
    )


def _count_rows(conn: Any, table_name: str, where_sql: str, params: Sequence[Any]) -> int:
    row = fetch_one(conn, f'SELECT COUNT(*) AS count FROM "{table_name}" WHERE {where_sql}', tuple(params))
    try:
        return int(_extract_row_value(row, 0, "count") or 0)
    except Exception:
        return 0


def _delete_where(
    conn: Any,
    *,
    table_name: str,
    where_sql: str,
    params: Sequence[Any],
    embedding_ids: List[str],
) -> int:
    if table_name == "signal_embeddings":
        columns = set(_list_table_columns(conn, table_name))
        source_column = next((col for col in ("record_id", "message_id", "source_id") if col in columns), None)
        if source_column and len(params) == 1:
            embedding_ids.extend(
                _select_embedding_ids(conn, table_name, source_column, str(params[0]))
            )
        elif "record_id" in columns and len(params) == 1:
            embedding_ids.extend(
                _select_embedding_ids(conn, table_name, "record_id", str(params[0]))
            )
    deleted = _count_rows(conn, table_name, where_sql, params)
    if deleted <= 0:
        return 0
    execute_query(conn, f'DELETE FROM "{table_name}" WHERE {where_sql}', tuple(params))
    return deleted


def _delete_anchor_row(
    conn: Any,
    *,
    table_name: str,
    pk_column: str,
    row_id: str,
    actions: List[TableAction],
    embedding_ids: List[str],
) -> int:
    deleted = _delete_where(
        conn,
        table_name=table_name,
        where_sql=f'"{pk_column}" = %s',
        params=(row_id,),
        embedding_ids=embedding_ids,
    )
    if deleted > 0:
        actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
    return deleted


def _delete_downstream_for_canonical(
    conn: Any,
    *,
    canonical_id: str,
    skip_tables: Set[str],
    actions: List[TableAction],
    embedding_ids: List[str],
) -> int:
    deleted_total = 0
    for table_name in _list_user_tables(conn):
        if (
            not _safe_sql_identifier(table_name)
            or table_name in _PROTECTED_TABLES
            or table_name in skip_tables
            or not _is_enrichment_or_signal_table(table_name)
        ):
            continue
        columns = set(_list_table_columns(conn, table_name))
        for link_col in _RECORD_LINK_COLUMNS:
            if link_col not in columns:
                continue
            deleted = _delete_where(
                conn,
                table_name=table_name,
                where_sql=f'"{link_col}" = %s',
                params=(canonical_id,),
                embedding_ids=embedding_ids,
            )
            if deleted > 0:
                deleted_total += deleted
                actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
            break
    return deleted_total


def _delete_canonical_row(
    conn: Any,
    *,
    canonical_table: str,
    canonical_id: str,
    actions: List[TableAction],
    embedding_ids: List[str],
) -> int:
    if not is_canonical_schema_table(canonical_table):
        return 0
    pk_column = resolve_primary_key_column(conn, canonical_table) or _NATIVE_ID_COL.get(canonical_table)
    if not pk_column:
        return 0
    return _delete_anchor_row(
        conn,
        table_name=canonical_table,
        pk_column=pk_column,
        row_id=canonical_id,
        actions=actions,
        embedding_ids=embedding_ids,
    )


def _delete_upstream_rows(
    conn: Any,
    *,
    source_id: str,
    source_record_id: str,
    skip_tables: Set[str],
    actions: List[TableAction],
) -> int:
    deleted_total = 0
    for table_name in _list_user_tables(conn):
        columns = set(_list_table_columns(conn, table_name))
        if (
            not _safe_sql_identifier(table_name)
            or table_name in _PROTECTED_TABLES
            or table_name in skip_tables
            or not _is_upstream_table(table_name, columns)
        ):
            continue
        predicates: List[str] = []
        params: List[Any] = []
        if "record_id" in columns:
            predicates.append('"record_id" = %s')
            params.append(source_record_id)
        elif "source_record_id" in columns:
            predicates.append('"source_record_id" = %s')
            params.append(source_record_id)
        else:
            continue
        source_predicates: List[str] = []
        if "source_id" in columns:
            source_predicates.append('"source_id" = %s')
            params.append(source_id)
        if "source_system" in columns:
            source_predicates.append('"source_system" = %s')
            params.append(source_id)
        if source_predicates:
            where_sql = f"({' OR '.join(predicates)}) AND ({' OR '.join(source_predicates)})"
        else:
            where_sql = " OR ".join(predicates)
        deleted = _count_rows(conn, table_name, where_sql, params)
        if deleted <= 0:
            continue
        execute_query(conn, f'DELETE FROM "{table_name}" WHERE {where_sql}', tuple(params))
        deleted_total += deleted
        actions.append(TableAction(table=table_name, action="rows_deleted", count=deleted))
    return deleted_total


def _delete_mapping_rows(
    conn: Any,
    *,
    source_id: Optional[str],
    source_record_id: Optional[str],
    canonical_table: Optional[str],
    canonical_id: Optional[str],
    actions: List[TableAction],
) -> int:
    if not _table_exists(conn, "canonical_source_mappings"):
        return 0
    deleted = 0
    if source_id and source_record_id:
        count = _count_rows(
            conn,
            "canonical_source_mappings",
            "source_id = %s AND source_record_id = %s",
            (source_id, source_record_id),
        )
        if count > 0:
            execute_query(
                conn,
                "DELETE FROM canonical_source_mappings WHERE source_id = %s AND source_record_id = %s",
                (source_id, source_record_id),
            )
            deleted += count
    if canonical_table and canonical_id:
        count = _count_rows(
            conn,
            "canonical_source_mappings",
            "canonical_table = %s AND canonical_id = %s",
            (canonical_table, canonical_id),
        )
        if count > 0:
            execute_query(
                conn,
                "DELETE FROM canonical_source_mappings WHERE canonical_table = %s AND canonical_id = %s",
                (canonical_table, canonical_id),
            )
            deleted += count
    if deleted > 0:
        actions.append(
            TableAction(table="canonical_source_mappings", action="rows_deleted", count=deleted)
        )
    return deleted


def delete_database_rows(
    conn: Any,
    *,
    table_name: str,
    row_ids: Sequence[str],
    scope: str,
) -> RowDeleteResult:
    normalized_scope = str(scope or "row_only").strip().lower()
    if normalized_scope not in VALID_DELETE_SCOPES:
        raise ValueError(f"Invalid scope: {scope!r}")

    table = str(table_name or "").strip()
    if not table or not _safe_sql_identifier(table):
        raise ValueError("table_name required")
    if table in _PROTECTED_TABLES:
        raise ValueError(f"Table is protected from deletion: {table}")
    if not _table_exists(conn, table):
        raise ValueError(f"Table or view not found: {table}")

    pk_column = resolve_primary_key_column(conn, table)
    if not pk_column:
        raise ValueError(f"Could not resolve primary key column for table: {table}")

    unique_row_ids = [str(item).strip() for item in row_ids if str(item).strip()]
    if not unique_row_ids:
        raise ValueError("row_ids required")

    result = RowDeleteResult(scope=normalized_scope, table_name=table, row_ids=unique_row_ids)
    actions: List[TableAction] = []
    embedding_ids: List[str] = []
    include_downstream = normalized_scope in {"with_downstream", "full_lineage"}
    include_upstream = normalized_scope in {"with_upstream", "full_lineage"}

    # Lineage deletes take SQLite's write lock at execute time — hold the gate
    # for the batch AND the commit (write_gate lock-order inversion); no-op for
    # a postgres conn (the gate is SQLite-only).
    with _sqlite_delete_gate(conn):
        for row_id in unique_row_ids:
            row = _fetch_row(conn, table, pk_column, row_id)
            if row is None:
                raise ValueError(f"Row not found: {row_id}")
            lineage = _resolve_lineage(
                conn,
                table_name=table,
                pk_column=pk_column,
                row_id=row_id,
                row=row,
                probe_parent=include_upstream,
            )
            if lineage.parent_canonical_table and lineage.parent_canonical_id:
                result.parents_retained.append(
                    {
                        "table": str(lineage.parent_canonical_table),
                        "id": str(lineage.parent_canonical_id),
                        "child_table": table,
                        "child_id": str(row_id),
                    }
                )
                # Reported, never deleted on. Expanding a delete across a split is
                # a separate named scope, not a side effect of resolving lineage —
                # but an under-delete that says nothing reads as a complete one,
                # which is the failure mode this surfaces.
                actions.append(
                    TableAction(
                        table=str(lineage.parent_canonical_table),
                        action="parent_retained",
                        count=1,
                    )
                )
            skip_tables: Set[str] = {table}

            if include_downstream and lineage.canonical_id:
                if lineage.anchor_table != lineage.canonical_table:
                    _delete_canonical_row(
                        conn,
                        canonical_table=str(lineage.canonical_table),
                        canonical_id=str(lineage.canonical_id),
                        actions=actions,
                        embedding_ids=embedding_ids,
                    )
                    if lineage.canonical_table:
                        skip_tables.add(str(lineage.canonical_table))
                _delete_downstream_for_canonical(
                    conn,
                    canonical_id=str(lineage.canonical_id),
                    skip_tables=skip_tables,
                    actions=actions,
                    embedding_ids=embedding_ids,
                )

            if include_upstream:
                if lineage.source_id and lineage.source_record_id:
                    _delete_upstream_rows(
                        conn,
                        source_id=str(lineage.source_id),
                        source_record_id=str(lineage.source_record_id),
                        skip_tables=skip_tables,
                        actions=actions,
                    )
                if (
                    lineage.canonical_table
                    and lineage.canonical_id
                    and lineage.anchor_table != lineage.canonical_table
                ):
                    _delete_canonical_row(
                        conn,
                        canonical_table=str(lineage.canonical_table),
                        canonical_id=str(lineage.canonical_id),
                        actions=actions,
                        embedding_ids=embedding_ids,
                    )
                _delete_mapping_rows(
                    conn,
                    source_id=lineage.source_id,
                    source_record_id=lineage.source_record_id,
                    canonical_table=lineage.canonical_table,
                    canonical_id=lineage.canonical_id,
                    actions=actions,
                )

            result.rows_deleted += _delete_anchor_row(
                conn,
                table_name=table,
                pk_column=pk_column,
                row_id=row_id,
                actions=actions,
                embedding_ids=embedding_ids,
            )

        vec_deleted = _delete_vec_rows_batched(conn, embedding_ids)
        if vec_deleted > 0:
            actions.append(TableAction(table="vector_index", action="vec_rows_deleted", count=vec_deleted))

    result.table_actions = actions
    return result
