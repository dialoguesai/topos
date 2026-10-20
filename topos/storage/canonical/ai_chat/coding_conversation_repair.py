"""One-time repair for coding-session conversations filed under the wrong prefix.

A runtime install of a coding source put a shim bound to that source's id over the ``ai_coding_tool`` mapper all three
coding sources share (``sources/runtime_install.BUNDLED_STORAGE_MAPPER_IDS``). The first coding source a process
installed then named every coding conversation: on the first live node checked (20 Oct 2026) 48,634 Claude Code
messages written by 1.5.13 sat under ``codex_sessions:<session>`` and the next build's under
``claude_code_sessions:<session>``, and a session that spanned the restart was two ``ai_chat_conversations`` rows.
The install is fixed for new writes; this sweep puts the rows on disk right.

Every coding-source message and conversation is renamed to ``<tool>:<session>``
(``coding_tool_mapper.canonical_coding_conversation_id``), the tool taken from the row's own ``source_id``. Rows that
then share an id merge into one: earliest ``created_at``, latest ``updated_at``; ``title`` is the earliest row's (the
first record's title never moves), ``latest_title`` the newest row's that has one, and the bookkeeping the newest
row's, as the upsert would have left a single row. A merged chat's messages are renumbered by event time. The derived
rows that copied the old id follow it: embedding and cluster-member provenance, the relationship graph's
``conversation:`` nodes and the edges to them, and the payloads of deferred jobs not yet done (so a job owed from
before the repair cannot write the old id back). A finished job's record of what it did is left alone.

Review surface: ``conversation_id`` stays in it (``permissions_v2.evidence.REVIEW_SURFACE_EXCLUSIONS``). It is the
row's identity, and a review should re-ask after the row moved to another conversation. So a review pinning a
rewritten coding row reads stale until its next pass; no other row's revision moves. On the first live node no
review, grant or share index named a coding-session message.

A group whose rows name two owners is left alone and counted. A stored id of no shape a coding writer produced is left
alone. Re-running finds nothing to do: a renamed row no longer differs from its canonical id.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .coding_tool_mapper import TOOL_BY_SOURCE_ID, canonical_coding_conversation_id

logger = logging.getLogger("topos.storage.canonical.ai_chat.coding_conversation_repair")

CODING_SOURCE_IDS: Tuple[str, ...] = tuple(TOOL_BY_SOURCE_ID)
_SOURCES_SQL = ",".join("?" * len(CODING_SOURCE_IDS))
#: Deferred jobs whose payload may still be run; a ``done`` job's payload is its history.
_OWED_JOB_STATUSES = ("queued", "running", "failed")
_CONVERSATION_COLUMNS = ("conversation_id", "owner_user_id", "title", "latest_title", "source_id", "created_at",
                         "updated_at", "source_record_id", "ingested_at", "sync_batch_id")


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _rename_plan(conn: sqlite3.Connection) -> Tuple[Dict[Tuple[str, str], str], int]:
    """``{(source_id, stored_id): canonical_id}`` for every stored id that differs, and the count left alone."""
    stored = set()
    for table in ("ai_chat_messages", "ai_chat_conversations"):
        stored.update(
            (str(source_id), str(conversation_id))
            for source_id, conversation_id in conn.execute(
                f"SELECT DISTINCT source_id, conversation_id FROM {table} WHERE source_id IN ({_SOURCES_SQL})",
                CODING_SOURCE_IDS,
            ).fetchall()
            if conversation_id is not None
        )
    plan: Dict[Tuple[str, str], str] = {}
    left_alone = 0
    for source_id, conversation_id in stored:
        canonical = canonical_coding_conversation_id(source_id, conversation_id)
        if canonical is None:
            left_alone += 1
        elif canonical != conversation_id:
            plan[(source_id, conversation_id)] = canonical
    return plan, left_alone


def _merged_conversation(rows: List[Dict[str, Any]], canonical: str) -> Optional[Dict[str, Any]]:
    """One row from the rows a session was split into, or None when they name two owners."""
    owners = {r["owner_user_id"] for r in rows if r["owner_user_id"] not in (None, "")}
    if len(owners) > 1:
        return None
    by_created = sorted(rows, key=lambda r: (r["created_at"] or "￿", r["conversation_id"]))
    by_updated = sorted(rows, key=lambda r: (r["updated_at"] or "", r["ingested_at"] or "", r["conversation_id"]))
    newest = by_updated[-1]
    created = [r["created_at"] for r in rows if r["created_at"]]
    updated = [r["updated_at"] for r in rows if r["updated_at"]]
    return {
        "conversation_id": canonical,
        "owner_user_id": next(iter(owners), newest["owner_user_id"]),
        "title": next((r["title"] for r in by_created if r["title"]), None),
        "latest_title": next((r["latest_title"] for r in reversed(by_updated) if r["latest_title"]), None),
        "source_id": newest["source_id"],
        "created_at": min(created) if created else None,
        "updated_at": max(updated) if updated else None,
        "source_record_id": canonical,
        "ingested_at": newest["ingested_at"],
        "sync_batch_id": newest["sync_batch_id"],
    }


def _rewrite_ids(value: Any, renames: Dict[str, str]) -> Tuple[Any, int]:
    """Replace every ``conversation_id`` value naming an old id, at any depth of a job payload."""
    if isinstance(value, dict):
        changed = 0
        out = {}
        for key, item in value.items():
            if key == "conversation_id" and isinstance(item, str) and item in renames:
                out[key] = renames[item]
                changed += 1
            else:
                out[key], n = _rewrite_ids(item, renames)
                changed += n
        return out, changed
    if isinstance(value, list):
        changed = 0
        out_list = []
        for item in value:
            rewritten, n = _rewrite_ids(item, renames)
            out_list.append(rewritten)
            changed += n
        return out_list, changed
    return value, 0


def _repair_derived(conn: sqlite3.Connection, renames: Dict[str, str]) -> Dict[str, int]:
    """Carry each old id into the derived rows that copied it. ``renames`` is old id → new id (coding sources only)."""
    counts = {"signal_embeddings": 0, "topic_cluster_members": 0, "graph_nodes": 0, "graph_edges": 0,
              "pipeline_jobs": 0}
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _coding_conversation_renames (old TEXT PRIMARY KEY, new TEXT NOT NULL)")
    conn.execute("DELETE FROM _coding_conversation_renames")
    conn.executemany("INSERT INTO _coding_conversation_renames VALUES (?, ?)", sorted(renames.items()))
    lookup = "(SELECT new FROM temp._coding_conversation_renames WHERE old = {})"

    embeddings = _table_columns(conn, "signal_embeddings")
    if {"conversation_id", "provenance_json", "source_id"} <= embeddings:
        counts["signal_embeddings"] = conn.execute(
            f"""UPDATE signal_embeddings SET
                    conversation_id = COALESCE({lookup.format('signal_embeddings.conversation_id')}, conversation_id),
                    provenance_json = CASE
                        WHEN json_valid(provenance_json)
                             AND json_extract(provenance_json, '$.conversation_id') IN
                                 (SELECT old FROM temp._coding_conversation_renames)
                        THEN json_set(provenance_json, '$.conversation_id',
                                      {lookup.format("json_extract(provenance_json, '$.conversation_id')")})
                        ELSE provenance_json END
                WHERE source_id IN ({_SOURCES_SQL})
                  AND (conversation_id IN (SELECT old FROM temp._coding_conversation_renames)
                       OR (json_valid(provenance_json) AND json_extract(provenance_json, '$.conversation_id') IN
                           (SELECT old FROM temp._coding_conversation_renames)))""",
            CODING_SOURCE_IDS,
        ).rowcount

    if {"metadata_json", "source_id"} <= _table_columns(conn, "topic_cluster_members"):
        counts["topic_cluster_members"] = conn.execute(
            f"""UPDATE topic_cluster_members SET metadata_json = json_set(metadata_json, '$.conversation_id',
                    {lookup.format("json_extract(metadata_json, '$.conversation_id')")})
                WHERE source_id IN ({_SOURCES_SQL}) AND json_valid(metadata_json)
                  AND json_extract(metadata_json, '$.conversation_id') IN
                      (SELECT old FROM temp._coding_conversation_renames)""",
            CODING_SOURCE_IDS,
        ).rowcount

    if _table_columns(conn, "graph_nodes") and _table_columns(conn, "graph_edges"):
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _coding_node_renames (old TEXT PRIMARY KEY, new TEXT NOT NULL)")
        conn.execute("DELETE FROM _coding_node_renames")
        conn.execute("INSERT INTO _coding_node_renames SELECT 'conversation:' || old, 'conversation:' || new"
                     " FROM temp._coding_conversation_renames")
        present = conn.execute(
            "SELECT n.node_id, r.new FROM graph_nodes n JOIN temp._coding_node_renames r ON r.old = n.node_id"
            " ORDER BY n.node_id"
        ).fetchall()
        for old_node, new_node in present:
            # The writer's label rule (job_writer, relationship_edges): the id after its first colon, 48 characters.
            label = new_node.split(":", 1)[-1][:48]
            if conn.execute("SELECT 1 FROM graph_nodes WHERE node_id = ?", (new_node,)).fetchone():
                moved = conn.execute("DELETE FROM graph_nodes WHERE node_id = ?", (old_node,)).rowcount
            else:
                moved = conn.execute(
                    """UPDATE graph_nodes SET node_id = ?, label = ?,
                           metadata_json = CASE WHEN json_valid(metadata_json)
                               THEN json_set(metadata_json, '$.node_id', ?, '$.label', ?) ELSE metadata_json END
                       WHERE node_id = ?""",
                    (new_node, label, new_node, label, old_node),
                ).rowcount
            counts["graph_nodes"] += moved
        node_lookup = "(SELECT new FROM temp._coding_node_renames WHERE old = graph_edges.{})"
        for end in ("src_node_id", "dst_node_id"):
            counts["graph_edges"] += conn.execute(
                f"""UPDATE graph_edges SET {end} = {node_lookup.format(end)},
                        metadata_json = CASE WHEN json_valid(metadata_json)
                            THEN json_set(metadata_json, '$.{end}', {node_lookup.format(end)}) ELSE metadata_json END
                    WHERE {end} IN (SELECT old FROM temp._coding_node_renames)"""
            ).rowcount
        conn.execute("DROP TABLE IF EXISTS temp._coding_node_renames")

    if {"payload_json", "status", "source_id"} <= _table_columns(conn, "pipeline_jobs"):
        statuses = ",".join("?" * len(_OWED_JOB_STATUSES))
        for job_id, payload_json in conn.execute(
            f"SELECT job_id, payload_json FROM pipeline_jobs WHERE status IN ({statuses})"
            f" AND source_id IN ({_SOURCES_SQL}) AND payload_json LIKE '%conversation_id%'",
            (*_OWED_JOB_STATUSES, *CODING_SOURCE_IDS),
        ).fetchall():
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            rewritten, changed = _rewrite_ids(payload, renames)
            if changed:
                conn.execute("UPDATE pipeline_jobs SET payload_json = ? WHERE job_id = ?",
                             (json.dumps(rewritten), job_id))
                counts["pipeline_jobs"] += 1
    conn.execute("DROP TABLE IF EXISTS temp._coding_conversation_renames")
    return counts


def repair_coding_conversation_ids(conn: sqlite3.Connection, *, dry_run: bool = False) -> Dict[str, Any]:
    """Rename coding-session conversations to ``<tool>:<session>`` and merge the rows a session was split into."""
    try:
        if not (_table_columns(conn, "ai_chat_messages") and _table_columns(conn, "ai_chat_conversations")):
            return {"status": "skipped", "reason": "no ai_chat tables", "messages_rewritten": 0}
        plan, left_alone = _rename_plan(conn)
    except sqlite3.Error as exc:
        logger.warning("coding conversation repair could not read the ai_chat tables: %s", exc)
        return {"status": "skipped", "error": str(exc), "messages_rewritten": 0}

    # Group every stored id by the canonical id it becomes, the canonical row itself included when it exists.
    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for (source_id, old), new in plan.items():
        groups[(source_id, new)].append(old)
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        group_rows: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for (source_id, new), olds in groups.items():
            ids = [*olds, new]
            group_rows[(source_id, new)] = [
                dict(r) for r in conn.execute(
                    f"SELECT {', '.join(_CONVERSATION_COLUMNS)} FROM ai_chat_conversations"
                    f" WHERE source_id = ? AND conversation_id IN ({','.join('?' * len(ids))})",
                    (source_id, *ids),
                ).fetchall()
            ]
    finally:
        conn.row_factory = previous_factory

    merged: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {
        key: _merged_conversation(rows, key[1]) if rows else None for key, rows in group_rows.items()
    }
    conflicts = {key for key, rows in group_rows.items() if rows and merged[key] is None}
    applied = {key: olds for key, olds in groups.items() if key not in conflicts}
    renames = {old: new for (source_id, new), olds in applied.items() for old in olds}
    messages = sum(
        int(conn.execute(
            f"SELECT count(*) FROM ai_chat_messages WHERE source_id = ? AND conversation_id IN ({','.join('?' * len(olds))})",
            (source_id, *olds),
        ).fetchone()[0])
        for (source_id, _new), olds in applied.items()
    )
    # Conversations after the repair that were stored under another id; ``merged`` of them came from two rows or more.
    renamed = sum(1 for key in applied if group_rows[key])
    merged_count = sum(1 for key in applied if len(group_rows[key]) > 1)
    report: Dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "messages_rewritten": messages,
        "conversations_renamed": renamed,
        "conversations_merged": merged_count,
        "owner_conflicts": len(conflicts),
        "left_alone": left_alone,
        "by_source": {
            source_id: sum(1 for (s, _new) in applied if s == source_id) for source_id in CODING_SOURCE_IDS
        },
    }
    if dry_run or not applied:
        if not dry_run:
            report["derived"] = {}
        return report

    from ...db.write_gate import commit_connection, with_db_write
    from .tables import CanonicalTablesManager

    with with_db_write():
        try:
            for (source_id, new), olds in sorted(applied.items()):
                conn.execute(
                    f"UPDATE ai_chat_messages SET conversation_id = ?"
                    f" WHERE source_id = ? AND conversation_id IN ({','.join('?' * len(olds))})",
                    (new, source_id, *olds),
                )
                row = merged[(source_id, new)]
                if row is None:
                    continue   # messages with no conversation row: renamed, nothing to merge
                ids = [*olds, new]
                conn.execute(
                    f"DELETE FROM ai_chat_conversations WHERE source_id = ? AND conversation_id IN ({','.join('?' * len(ids))})",
                    (source_id, *ids),
                )
                conn.execute(
                    f"INSERT INTO ai_chat_conversations ({', '.join(_CONVERSATION_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(_CONVERSATION_COLUMNS))})",
                    tuple(row[c] for c in _CONVERSATION_COLUMNS),
                )
            report["derived"] = _repair_derived(conn, renames)
            commit_connection(conn)
        except Exception:
            conn.rollback()
            raise

    # A merged chat's messages came from two numberings; one pass by event time, as every batch write ends.
    tables = CanonicalTablesManager(conn)
    for key in sorted(applied):
        if len(group_rows[key]) > 1:
            tables.update_message_sequences(key[1])

    logger.info(
        "coding conversation repair: %d message(s), %d conversation(s) renamed, %d merged, %d owner conflict(s)",
        messages, renamed, merged_count, len(conflicts),
    )
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m topos.storage.canonical.ai_chat.coding_conversation_repair --db PATH [--dry-run]``: counts only."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--db", required=True, help="the node database to repair (a copy, unless you mean it)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        print(json.dumps(repair_coding_conversation_ids(conn, dry_run=args.dry_run), indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
