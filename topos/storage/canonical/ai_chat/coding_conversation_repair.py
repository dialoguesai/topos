"""One-time repair for coding-session conversations filed under the wrong prefix.

A runtime install of a coding source put a shim bound to that source's id over the ``ai_coding_tool`` mapper all three
coding sources share (``sources/runtime_install.BUNDLED_STORAGE_MAPPER_IDS``). The first coding source a process
installed then named every coding conversation: on the first live node checked (20 Oct 2026) 48,634 Claude Code
messages written by 1.5.13 sat under ``codex_sessions:<session>`` and the next build's under
``claude_code_sessions:<session>``, and a session that spanned the restart was two ``ai_chat_conversations`` rows.
The install is fixed for new writes; this sweep puts the rows on disk right.

Every coding-source message and conversation is renamed to ``<tool>:<session>``
(``coding_tool_mapper.canonical_coding_conversation_id``), the tool taken from the row's own ``source_id``: renames are
keyed by ``(source_id, stored id)``, because the shim's no-thread fallback could give a Claude Code and a Codex row
one id. Rows that then share an id merge into one: earliest ``created_at``, latest ``updated_at``; ``title`` is the
earliest row's (the first record's title never moves), ``latest_title`` the newest row's that has one, and every other
column the newest row's, as the upsert would have left a single row. A merged chat's messages are renumbered by event
time (message id breaks a tie). The derived rows that copied an old id follow it: embedding and cluster-member
provenance, the relationship graph's ``conversation:`` nodes and the edges to them, and the payloads of deferred jobs
not yet done. A finished job's record of what it did is left alone.

The derived sweep finds its own work: any coding-source derived row whose id is not canonical, whether or not a
message moved in this run. A deferred job already running when the repair commits holds its payload in memory and can
still write an old id; the next run of the sweep (the CLI door below, or a later step) puts it right.

Everything, the reads that decide the plan included, happens under one write-gate hold and one transaction, rolled
back on any exception, so a crash or a concurrent lane write cannot leave a chat half-renamed, half-merged or
half-renumbered.

Review surface: ``conversation_id`` stays in it (``permissions_v2.evidence.REVIEW_SURFACE_EXCLUSIONS``). It is the
row's identity, and a review should re-ask after the row moved to another conversation. So a review pinning a
rewritten coding row reads stale until its next pass; no other row's revision moves. On the first live node no
review, grant or share index named a coding-session message.

A group whose rows name two owners is left alone, its derived copies with it, and counted. A stored id of no shape a
coding writer produced is left alone and counted. Re-running finds nothing to do.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .coding_tool_mapper import TOOL_BY_SOURCE_ID, canonical_coding_conversation_id

logger = logging.getLogger("topos.storage.canonical.ai_chat.coding_conversation_repair")

CODING_SOURCE_IDS: Tuple[str, ...] = tuple(TOOL_BY_SOURCE_ID)
_SOURCES_SQL = ",".join("?" * len(CODING_SOURCE_IDS))
#: Deferred jobs whose payload may still be run; a ``done`` job's payload is its history.
_OWED_JOB_STATUSES = ("queued", "running", "failed")
_NODE_PREFIX = "conversation:"
_RENAMES = "temp._coding_conversation_renames"

Key = Tuple[str, str]   # (source_id, conversation id)


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _marks(items: Iterable[Any]) -> str:
    return ",".join("?" * len(list(items)))


class _Plan:
    """What the repair would do, read from the database as it is now."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        stored: Set[Key] = set()
        message_ids: Set[Key] = set()
        for table in ("ai_chat_messages", "ai_chat_conversations"):
            rows = conn.execute(
                f"SELECT DISTINCT source_id, conversation_id FROM {table} WHERE source_id IN ({_SOURCES_SQL})",
                CODING_SOURCE_IDS,
            ).fetchall()
            keys = {(str(s), str(c)) for s, c in rows if c is not None}
            stored |= keys
            if table == "ai_chat_messages":
                message_ids = keys
        self.left_alone = 0
        #: The sources that stored each id: a graph row written with no source follows the one source there is.
        self.sources_by_id: Dict[str, Set[str]] = defaultdict(set)
        for source_id, conversation_id in stored:
            self.sources_by_id[conversation_id].add(source_id)
        renames: Dict[Key, str] = {}
        for source_id, conversation_id in stored:
            canonical = canonical_coding_conversation_id(source_id, conversation_id)
            if canonical is None:
                self.left_alone += 1
            elif canonical != conversation_id:
                renames[(source_id, conversation_id)] = canonical
        # Group every stored id by the canonical id it becomes, the canonical row itself included when it exists.
        groups: Dict[Key, List[str]] = defaultdict(list)
        for (source_id, old), new in renames.items():
            groups[(source_id, new)].append(old)
        self.columns = _table_columns(conn, "ai_chat_conversations")
        self.rows: Dict[Key, List[Dict[str, Any]]] = {}
        for (source_id, new), olds in groups.items():
            ids = [*olds, new]
            self.rows[(source_id, new)] = [
                dict(zip(self.columns, r)) for r in conn.execute(
                    f"SELECT {', '.join(self.columns)} FROM ai_chat_conversations"
                    f" WHERE source_id = ? AND conversation_id IN ({_marks(ids)})",
                    (source_id, *ids),
                ).fetchall()
            ]
        self.merged: Dict[Key, Optional[Dict[str, Any]]] = {
            key: _merged_conversation(rows, key[1]) if rows else None for key, rows in self.rows.items()
        }
        self.conflicts: Set[Key] = {key for key, rows in self.rows.items() if rows and self.merged[key] is None}
        self.groups: Dict[Key, List[str]] = {k: v for k, v in groups.items() if k not in self.conflicts}
        #: Old ids held back with a conflicted group: their derived copies stay with them.
        self.held: Set[Key] = {(s, old) for (s, _new), olds in groups.items() if (s, _new) in self.conflicts
                               for old in olds}
        #: Groups whose messages came from more than one stored id, and so from more than one numbering.
        self.renumber: List[Key] = sorted(
            key for key, olds in self.groups.items()
            if sum(1 for i in (*olds, key[1]) if (key[0], i) in message_ids) > 1
        )
        self.messages = sum(
            int(conn.execute(
                f"SELECT count(*) FROM ai_chat_messages WHERE source_id = ? AND conversation_id IN ({_marks(olds)})",
                (source_id, *olds),
            ).fetchone()[0])
            for (source_id, _new), olds in self.groups.items()
        )

    def rename(self, source_id: Optional[str], conversation_id: Any) -> Optional[str]:
        """The new id for a derived copy, or None when it stays as it is."""
        if not isinstance(conversation_id, str) or not source_id or (source_id, conversation_id) in self.held:
            return None
        canonical = canonical_coding_conversation_id(source_id, conversation_id)
        if canonical is None or canonical == conversation_id or (source_id, canonical) in self.conflicts:
            return None
        return canonical

    def report(self, status: str) -> Dict[str, Any]:
        return {
            "status": status,
            "messages_rewritten": self.messages,
            # Conversations after the repair that were stored under another id; ``merged`` came from two rows or more.
            "conversations_renamed": sum(1 for key in self.groups if self.rows[key]),
            "conversations_merged": sum(1 for key in self.groups if len(self.rows[key]) > 1),
            # Renamed messages with no conversation row: the stored id was one row's key, and another source held it.
            "conversations_missing": sum(1 for key in self.groups if not self.rows[key]),
            "owner_conflicts": len(self.conflicts),
            "left_alone": self.left_alone,
            "by_source": {s: sum(1 for (src, _new) in self.groups if src == s) for s in CODING_SOURCE_IDS},
        }


def _merged_conversation(rows: List[Dict[str, Any]], canonical: str) -> Optional[Dict[str, Any]]:
    """One row from the rows a session was split into, or None when they name two owners."""
    owners = {r["owner_user_id"] for r in rows if r["owner_user_id"] not in (None, "")}
    if len(owners) > 1:
        return None
    by_created = sorted(rows, key=lambda r: (r["created_at"] or "￿", r["conversation_id"]))
    by_updated = sorted(rows, key=lambda r: (r["updated_at"] or "", r["ingested_at"] or "", r["conversation_id"]))
    created = [r["created_at"] for r in rows if r["created_at"]]
    updated = [r["updated_at"] for r in rows if r["updated_at"]]
    # Every column the rules below do not name is the newest row's, a column a later migration adds included.
    merged = dict(by_updated[-1])
    merged.update({
        "conversation_id": canonical,
        "owner_user_id": next(iter(owners), merged["owner_user_id"]),
        "title": next((r["title"] for r in by_created if r["title"]), None),
        "latest_title": next((r["latest_title"] for r in reversed(by_updated) if r["latest_title"]), None),
        "created_at": min(created) if created else None,
        "updated_at": max(updated) if updated else None,
        "source_record_id": canonical,
    })
    return merged


def _rewrite_ids(value: Any, rename: Callable[[Any], Optional[str]]) -> Tuple[Any, int]:
    """Replace every ``conversation_id`` value ``rename`` maps, at any depth of a job payload."""
    if isinstance(value, dict):
        changed = 0
        out = {}
        for key, item in value.items():
            new = rename(item) if key == "conversation_id" else None
            if new is not None:
                out[key] = new
                changed += 1
            else:
                out[key], n = _rewrite_ids(item, rename)
                changed += n
        return out, changed
    if isinstance(value, list):
        changed = 0
        out_list = []
        for item in value:
            rewritten, n = _rewrite_ids(item, rename)
            out_list.append(rewritten)
            changed += n
        return out_list, changed
    return value, 0


def _renumber(conn: sqlite3.Connection, conversation_ids: List[str]) -> None:
    """Sequence by event time inside the open transaction, as every batch write ends (``update_message_sequences``)."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _coding_sequences (message_id TEXT PRIMARY KEY, seq INTEGER NOT NULL)")
    conn.execute("DELETE FROM temp._coding_sequences")
    for start in range(0, len(conversation_ids), 400):
        chunk = conversation_ids[start:start + 400]
        conn.execute(
            "INSERT INTO temp._coding_sequences SELECT message_id,"
            " ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY event_at, message_id) - 1"
            f" FROM ai_chat_messages WHERE conversation_id IN ({_marks(chunk)})",
            chunk,
        )
    conn.execute(
        "UPDATE ai_chat_messages SET sequence = (SELECT seq FROM temp._coding_sequences s"
        " WHERE s.message_id = ai_chat_messages.message_id)"
        " WHERE message_id IN (SELECT message_id FROM temp._coding_sequences)"
    )
    conn.execute("DROP TABLE IF EXISTS temp._coding_sequences")


def _derived_keys(conn: sqlite3.Connection) -> Set[Key]:
    """Every ``(source_id, conversation id)`` a coding-source derived row holds."""
    keys: Set[Key] = set()

    def add(sql: str, strip: str = "") -> None:
        for source_id, value in conn.execute(sql, CODING_SOURCE_IDS).fetchall():
            if isinstance(value, str) and value.startswith(strip):
                keys.add((str(source_id), value[len(strip):]))

    embeddings = set(_table_columns(conn, "signal_embeddings"))
    if {"conversation_id", "source_id"} <= embeddings:
        add(f"SELECT DISTINCT source_id, conversation_id FROM signal_embeddings WHERE source_id IN ({_SOURCES_SQL})")
    if {"provenance_json", "source_id"} <= embeddings:
        add("SELECT DISTINCT source_id, json_extract(provenance_json, '$.conversation_id') FROM signal_embeddings"
            f" WHERE source_id IN ({_SOURCES_SQL}) AND json_valid(provenance_json)")
    if {"metadata_json", "source_id"} <= set(_table_columns(conn, "topic_cluster_members")):
        add("SELECT DISTINCT source_id, json_extract(metadata_json, '$.conversation_id') FROM topic_cluster_members"
            f" WHERE source_id IN ({_SOURCES_SQL}) AND json_valid(metadata_json)")
    return keys


def _graph_renames(conn: sqlite3.Connection, plan: _Plan) -> Dict[str, str]:
    """``conversation:`` node id → its new id. A node id names one conversation, so nodes and the edges to them move
    by id alone: under the coding source the graph rows name, or, for rows written with no coding source (the
    relationship writer keeps whatever its records carried), under the one source that stored the id. An id two
    sources could claim stays as it is."""
    sources: Dict[str, Set[Optional[str]]] = defaultdict(set)
    queries = []
    if {"node_id", "source_id"} <= set(_table_columns(conn, "graph_nodes")):
        queries.append(f"SELECT DISTINCT source_id, node_id FROM graph_nodes WHERE node_id LIKE '{_NODE_PREFIX}%'")
    if {"src_node_id", "dst_node_id", "source_id"} <= set(_table_columns(conn, "graph_edges")):
        queries += [f"SELECT DISTINCT source_id, {end} FROM graph_edges WHERE {end} LIKE '{_NODE_PREFIX}%'"
                    for end in ("src_node_id", "dst_node_id")]
    for sql in queries:
        for source_id, node_id in conn.execute(sql).fetchall():
            sources[str(node_id)].add(source_id)
    renames: Dict[str, str] = {}
    for node_id, named in sources.items():
        old = node_id[len(_NODE_PREFIX):]
        coding = {s for s in named if s in TOOL_BY_SOURCE_ID}
        candidates = {plan.rename(s, old) for s in (coding or plan.sources_by_id.get(old, set()))}
        if len(candidates) == 1 and None not in candidates:
            renames[node_id] = _NODE_PREFIX + candidates.pop()
    return renames


def _repair_derived(conn: sqlite3.Connection, plan: _Plan) -> Dict[str, int]:
    """Carry every non-canonical coding id into canonical form in the derived rows that copied it."""
    counts = {"signal_embeddings": 0, "topic_cluster_members": 0, "graph_nodes": 0, "graph_edges": 0,
              "pipeline_jobs": 0}
    renames = {key: new for key in _derived_keys(conn) if (new := plan.rename(*key)) is not None}
    node_renames = _graph_renames(conn, plan)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _coding_conversation_renames"
                 " (source_id TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, PRIMARY KEY (source_id, old))")
    conn.execute(f"DELETE FROM {_RENAMES}")
    conn.executemany(f"INSERT INTO {_RENAMES} VALUES (?, ?, ?)", sorted((s, o, n) for (s, o), n in renames.items()))

    def lookup(table: str, expr: str) -> str:
        return f"(SELECT r.new FROM {_RENAMES} r WHERE r.source_id = {table}.source_id AND r.old = {expr})"

    def named(table: str, expr: str) -> str:
        return f"EXISTS (SELECT 1 FROM {_RENAMES} r WHERE r.source_id = {table}.source_id AND r.old = {expr})"

    if renames:
        embeddings = set(_table_columns(conn, "signal_embeddings"))
        if {"conversation_id", "provenance_json", "source_id"} <= embeddings:
            prov = "json_extract(provenance_json, '$.conversation_id')"
            counts["signal_embeddings"] = conn.execute(
                f"""UPDATE signal_embeddings SET
                        conversation_id = COALESCE({lookup('signal_embeddings', 'signal_embeddings.conversation_id')},
                                                   conversation_id),
                        provenance_json = CASE WHEN json_valid(provenance_json) AND {named('signal_embeddings', prov)}
                            THEN json_set(provenance_json, '$.conversation_id', {lookup('signal_embeddings', prov)})
                            ELSE provenance_json END
                    WHERE source_id IN ({_SOURCES_SQL})
                      AND ({named('signal_embeddings', 'signal_embeddings.conversation_id')}
                           OR (json_valid(provenance_json) AND {named('signal_embeddings', prov)}))""",
                CODING_SOURCE_IDS,
            ).rowcount

        if {"metadata_json", "source_id"} <= set(_table_columns(conn, "topic_cluster_members")):
            meta = "json_extract(metadata_json, '$.conversation_id')"
            counts["topic_cluster_members"] = conn.execute(
                f"""UPDATE topic_cluster_members
                    SET metadata_json = json_set(metadata_json, '$.conversation_id',
                                                 {lookup('topic_cluster_members', meta)})
                    WHERE source_id IN ({_SOURCES_SQL}) AND json_valid(metadata_json)
                      AND {named('topic_cluster_members', meta)}""",
                CODING_SOURCE_IDS,
            ).rowcount

    conn.execute(f"DROP TABLE IF EXISTS {_RENAMES}")

    if node_renames:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _coding_node_renames (old TEXT PRIMARY KEY, new TEXT NOT NULL)")
        conn.execute("DELETE FROM temp._coding_node_renames")
        conn.executemany("INSERT INTO temp._coding_node_renames VALUES (?, ?)", sorted(node_renames.items()))
        for old_node, new_node in sorted(node_renames.items()):
            if not conn.execute("SELECT 1 FROM graph_nodes WHERE node_id = ?", (old_node,)).fetchone():
                continue   # an edge-only straggler: the edges move below
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
        for end in ("src_node_id", "dst_node_id"):
            new_end = f"(SELECT r.new FROM temp._coding_node_renames r WHERE r.old = graph_edges.{end})"
            counts["graph_edges"] += conn.execute(
                f"""UPDATE graph_edges SET {end} = {new_end},
                        metadata_json = CASE WHEN json_valid(metadata_json)
                            THEN json_set(metadata_json, '$.{end}', {new_end}) ELSE metadata_json END
                    WHERE {end} IN (SELECT old FROM temp._coding_node_renames)"""
            ).rowcount
        conn.execute("DROP TABLE IF EXISTS temp._coding_node_renames")

    if {"payload_json", "status", "source_id"} <= set(_table_columns(conn, "pipeline_jobs")):
        statuses = _marks(_OWED_JOB_STATUSES)
        for job_id, job_source, payload_json in conn.execute(
            f"SELECT job_id, source_id, payload_json FROM pipeline_jobs WHERE status IN ({statuses})"
            f" AND source_id IN ({_SOURCES_SQL}) AND payload_json LIKE '%conversation_id%'",
            (*_OWED_JOB_STATUSES, *CODING_SOURCE_IDS),
        ).fetchall():
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            rewritten, changed = _rewrite_ids(payload, lambda value: plan.rename(job_source, value))
            if changed:
                conn.execute("UPDATE pipeline_jobs SET payload_json = ? WHERE job_id = ?",
                             (json.dumps(rewritten), job_id))
                counts["pipeline_jobs"] += 1
    return counts


def _apply(conn: sqlite3.Connection, plan: _Plan) -> Dict[str, int]:
    for (source_id, new), olds in sorted(plan.groups.items()):
        conn.execute(
            f"UPDATE ai_chat_messages SET conversation_id = ?"
            f" WHERE source_id = ? AND conversation_id IN ({_marks(olds)})",
            (new, source_id, *olds),
        )
        row = plan.merged[(source_id, new)]
        if row is None:
            continue   # messages with no conversation row: renamed, nothing to merge
        ids = [*olds, new]
        conn.execute(
            f"DELETE FROM ai_chat_conversations WHERE source_id = ? AND conversation_id IN ({_marks(ids)})",
            (source_id, *ids),
        )
        conn.execute(
            f"INSERT INTO ai_chat_conversations ({', '.join(plan.columns)}) VALUES ({_marks(plan.columns)})",
            tuple(row.get(c) for c in plan.columns),
        )
    _renumber(conn, [new for _source, new in plan.renumber])
    return _repair_derived(conn, plan)


def repair_coding_conversation_ids(conn: sqlite3.Connection, *, dry_run: bool = False) -> Dict[str, Any]:
    """Rename coding-session conversations to ``<tool>:<session>`` and merge the rows a session was split into."""
    try:
        if not (_table_columns(conn, "ai_chat_messages") and _table_columns(conn, "ai_chat_conversations")):
            return {"status": "skipped", "reason": "no ai_chat tables", "messages_rewritten": 0}
        if dry_run:
            plan = _Plan(conn)
            report = plan.report("dry_run")
            report["derived_ids"] = (
                sum(1 for key in _derived_keys(conn) if plan.rename(*key) is not None) + len(_graph_renames(conn, plan))
            )
            return report
    except sqlite3.Error as exc:
        logger.warning("coding conversation repair could not read the ai_chat tables: %s", exc)
        return {"status": "skipped", "error": str(exc), "messages_rewritten": 0}

    from ...db.write_gate import commit_connection, with_db_write

    with with_db_write():
        try:
            # The plan is read under the gate: a lane write between reading and writing would otherwise be
            # overwritten by a merged row built from older reads.
            plan = _Plan(conn)
            report = plan.report("ok")
            report["derived"] = _apply(conn, plan)
            commit_connection(conn)
        except BaseException:
            conn.rollback()
            raise

    logger.info(
        "coding conversation repair: %d message(s), %d conversation(s) renamed, %d merged, %d owner conflict(s),"
        " derived %s",
        report["messages_rewritten"], report["conversations_renamed"], report["conversations_merged"],
        report["owner_conflicts"], report["derived"],
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
