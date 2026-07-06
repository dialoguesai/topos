"""Derived-layer garbage collection.

The 2026-07-06 storage audit found unbounded accumulation in every derived
layer that has a writer but no reaper:

  * signal_objects held 11,240 stale ``top_topics`` rows (82% of the table)
    for 6 live clusters — pre-stable-ID recomputes minted fresh cluster ids
    and the materializer upserted each as a brand-new object, forever;
  * signal_dimension_brief_revisions grew a full markdown+JSON snapshot per
    ingest batch (~128/day per active brief) with no compaction;
  * uma_access_requests / mcp_request_log grew ~100 rows/day with no
    retention;
  * junk embeddings (undecoded NSKeyedArchiver blobs) survived in
    signal_embeddings and formed a 2k-member junk cluster.

Everything removed here is derived and rebuildable (or pure audit log);
canonical data is never touched. Run via the nightly runner's ``gc`` step.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger("topos.features.lifecycle.gc")

BRIEF_KEEP_ALL_DAYS = 7      # every revision this recent survives
BRIEF_DAILY_DAYS = 90        # then: latest revision per brief per day
AUDIT_RETENTION_DAYS = 90

# Superseded tables kept only because live code still references them.
# GC marks them in wiki_table_catalog instead of dropping; removal needs the
# referencing code retired first.
DEPRECATED_TABLES = {
    "persons": "superseded by entities (entity_type='person')",
    "person_aliases": "superseded by entities.aliases_json",
    "relationship_edges": "superseded by entity_edges",
    "signal_summaries": "never populated; summaries live on briefs/facts",
    "signal_tags": "never populated; tags live in signal_objects payloads",
    "message_embeddings": "superseded by signal_embeddings",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def audit_retention_days() -> int:
    raw = os.environ.get("TOPOS_AUDIT_RETENTION_DAYS", str(AUDIT_RETENTION_DAYS)).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return AUDIT_RETENTION_DAYS


def reconcile_top_topics_objects(conn: sqlite3.Connection) -> int:
    """Delete signal_objects top_topics rows whose cluster no longer exists.

    These are per-recompute snapshots of topic_clusters — derived from
    derived data — so deletion (not tombstoning) is correct: the live set is
    rewritten by the materializer after every recompute.
    """
    if not _table_exists(conn, "signal_objects") or not _table_exists(conn, "topic_clusters"):
        return 0
    cursor = conn.execute(
        """
        DELETE FROM signal_objects
        WHERE object_type = 'top_topics'
          AND object_key NOT IN (SELECT cluster_id FROM topic_clusters)
        """
    )
    removed = int(cursor.rowcount or 0)
    if removed:
        logger.info("GC: removed %d stale top_topics signal objects", removed)
    return removed


def compact_brief_revisions(
    conn: sqlite3.Connection,
    *,
    keep_all_days: int = BRIEF_KEEP_ALL_DAYS,
    daily_days: int = BRIEF_DAILY_DAYS,
) -> int:
    """Thin the brief revision history without losing its shape.

    Policy: keep every revision from the last ``keep_all_days``; keep the
    last revision per brief per *day* up to ``daily_days``; keep the last
    revision per brief per ISO *week* beyond that. Head revisions are always
    protected (signal_dimension_briefs.head_revision_id), as is revision 1
    (the brief's origin).
    """
    if not _table_exists(conn, "signal_dimension_brief_revisions"):
        return 0
    protected: set = set()
    if _table_exists(conn, "signal_dimension_briefs"):
        protected = {
            str(r[0])
            for r in conn.execute(
                "SELECT head_revision_id FROM signal_dimension_briefs"
                " WHERE head_revision_id IS NOT NULL"
            ).fetchall()
        }
    cursor = conn.execute(
        """
        DELETE FROM signal_dimension_brief_revisions
        WHERE revision_id NOT IN (
            -- newest revision per brief per day (recent window)
            SELECT revision_id FROM (
                SELECT revision_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY brief_id, DATE(created_at)
                           ORDER BY revision_number DESC
                       ) AS rn
                FROM signal_dimension_brief_revisions
                WHERE created_at >= datetime('now', ?)
            ) WHERE rn = 1
        )
        AND revision_id NOT IN (
            -- newest revision per brief per ISO week (long tail)
            SELECT revision_id FROM (
                SELECT revision_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY brief_id, STRFTIME('%Y-%W', created_at)
                           ORDER BY revision_number DESC
                       ) AS rn
                FROM signal_dimension_brief_revisions
            ) WHERE rn = 1
        )
        AND created_at < datetime('now', ?)
        AND revision_number > 1
        """
        + (
            f" AND revision_id NOT IN ({','.join('?' for _ in protected)})"
            if protected
            else ""
        ),
        (f"-{int(daily_days)} days", f"-{int(keep_all_days)} days", *protected),
    )
    removed = int(cursor.rowcount or 0)
    if removed:
        logger.info("GC: compacted %d brief revisions", removed)
    return removed


def apply_audit_retention(conn: sqlite3.Connection, *, days: int | None = None) -> Dict[str, int]:
    """Trim pure request-audit ledgers. 0 days disables trimming."""
    days = audit_retention_days() if days is None else days
    removed = {"uma_access_requests": 0, "mcp_request_log": 0}
    if days <= 0:
        return removed
    for table, ts_column in (
        ("uma_access_requests", "created_at"),
        ("mcp_request_log", "requested_at"),
    ):
        if not _table_exists(conn, table):
            continue
        try:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {ts_column} < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            removed[table] = int(cursor.rowcount or 0)
        except sqlite3.OperationalError as exc:
            logger.debug("GC: retention skipped for %s: %s", table, exc)
    total = sum(removed.values())
    if total:
        logger.info("GC: trimmed %d audit rows (%d-day retention)", total, days)
    return removed


def purge_junk_embeddings(conn: sqlite3.Connection) -> int:
    """Remove embeddings whose preview is serialization garbage.

    Complements the nightly ``cleanjunk`` step (which walks canonical
    content): this catches junk judged from the stored preview itself, e.g.
    rows whose canonical parent was already fixed or removed. FTS shadow rows
    go via the existing AFTER DELETE trigger; ANN rows are removed explicitly.
    """
    from ...features.signal.embed_context import is_derivable_content
    from ...storage.adapters.sqlite.vector_search import delete_vec_rows

    if not _table_exists(conn, "signal_embeddings"):
        return 0
    rows = conn.execute(
        "SELECT embedding_id, record_id, text_preview FROM signal_embeddings"
        " WHERE text_preview IS NOT NULL AND text_preview != ''"
    ).fetchall()
    junk_embedding_ids: List[str] = []
    junk_record_ids: set = set()
    for embedding_id, record_id, preview in rows:
        if not is_derivable_content(str(preview)):
            junk_embedding_ids.append(str(embedding_id))
            if record_id:
                junk_record_ids.add(str(record_id))
    for start in range(0, len(junk_embedding_ids), 200):
        chunk = junk_embedding_ids[start : start + 200]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM signal_embeddings WHERE embedding_id IN ({placeholders})", chunk
        )
        delete_vec_rows(conn, chunk)
    if junk_record_ids and _table_exists(conn, "topic_cluster_members"):
        ids = list(junk_record_ids)
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM topic_cluster_members WHERE record_id IN ({placeholders})",
                chunk,
            )
    if junk_embedding_ids:
        logger.info("GC: purged %d junk embeddings", len(junk_embedding_ids))
    return len(junk_embedding_ids)


def mark_deprecated_tables(conn: sqlite3.Connection) -> int:
    """Record superseded tables in wiki_table_catalog (no drops — code refs remain)."""
    if not _table_exists(conn, "wiki_table_catalog"):
        return 0
    marked = 0
    for table, note in DEPRECATED_TABLES.items():
        if not _table_exists(conn, table):
            continue
        conn.execute(
            """
            INSERT INTO wiki_table_catalog (table_name, authoritative_table, status, deprecation_note, updated_at)
            VALUES (?, NULL, 'deprecated', ?, datetime('now'))
            ON CONFLICT(table_name) DO UPDATE SET
                status='deprecated', deprecation_note=excluded.deprecation_note,
                updated_at=datetime('now')
            """,
            (table, note),
        )
        marked += 1
    return marked


def run_gc(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Full GC pass. Derived/audit data only; canonical rows are never touched."""
    report: Dict[str, Any] = {
        "top_topics_objects_removed": reconcile_top_topics_objects(conn),
        "brief_revisions_compacted": compact_brief_revisions(conn),
        "junk_embeddings_purged": purge_junk_embeddings(conn),
        "deprecated_tables_marked": mark_deprecated_tables(conn),
    }
    report["audit_rows_trimmed"] = apply_audit_retention(conn)
    conn.commit()
    return report
