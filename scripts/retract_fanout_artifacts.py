#!/usr/bin/env python3
"""Retract derived rows produced by fan-outs that should never have made them.

Three populations exist on a node that has run the affected code, and they need
three DIFFERENT recovery rules — this is a per-site migration, not a schema one:

  a. **fabricated goals** — a location fan-out child's whole content is a place
     name, and until the table-stamp fix it was misfiled as a journal entry, so
     the belief-role gate treated it as the owner's own writing and sent it to a
     model. The result is goals the owner never had ("Watch Northgate- The
     Convent", "Seeking information about the book 'The Foundry' by Northgate").
     Measured 2026-08-27: 154 rows over 63 records (77 real plus 77 untyped
     twins from the double write), 76 distinct texts, which minted 37 `goal`
     entities carrying 54 edges. Those entities are EXEMPT from orphan pruning by
     an explicit `goal_%` keep rule, so nothing else will ever remove them.

  b. **the retired GitHub per-commit fan-out** — the code path was deleted on
     2026-08-14 and no backfill retracted what it had already minted. 121
     `journal_entries` rows survive, feeding 485 stale relationship facts, 121
     embeddings that duplicate an `activity_events` embedding verbatim, and 121
     duplicate timeline rows. Retracting only the derived rows is not stable: the
     canonical rows would re-derive them on the next reprocess.

  c. **one unrecoverable orphan** — `tl-job-time-log-1-loc` has a timeline row,
     no `location_events` row, and no parent. It cannot be linked, only deleted.

Safety
------
Dry run is the DEFAULT and `--apply` is the only way to write. The database path
must be given explicitly; there is no fallback to `~/.topos/database.db`, because
a backfill that finds the owner's live database on its own is a backfill that
will eventually run against it by accident.

Black-holed entities are never removed, whatever population they fall in — the
protection outranks the retraction, and `derived_scrub.is_entity_protected` is
the shared predicate.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOC_SUFFIX = "-loc"
GITHUB_SOURCE = "github_activity"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open with the sqlite-vec extension loaded.

    ``signal_embeddings_vec`` is a vec0 VIRTUAL table: without the extension the
    module is missing and every statement against it raises. ``delete_vec_rows``
    swallows that error, and ``_sqlite_vec_ready`` only checks that the table
    exists in ``sqlite_master`` — not that the module is loaded — so a plain
    ``sqlite3.connect`` deletes the base rows and silently leaves the ANN
    companions behind.

    Caught by running this script against a real snapshot: the base table went
    9,583 -> 9,462 while ``signal_embeddings_vec_rowids`` stayed at 9,583, for 121
    orphaned ANN rows against a baseline of zero. An orphaned vector is still a
    nearest neighbour, so this is a retraction that leaves the thing it retracted
    reachable.
    """
    # 60s busy timeout: on a live node the app holds the same database, and the
    # default 5s is short enough that a checkpoint or an enrichment batch can make
    # this fail partway. Each population commits separately and every step is
    # idempotent, so a re-run after a lock error is safe — but waiting is better.
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        from topos.storage.db.connection_tuning import load_sqlite_vec

        conn.execute("PRAGMA busy_timeout=60000")
        if not load_sqlite_vec(conn):
            print(
                "warn: sqlite-vec could not be loaded; ANN companion rows cannot be "
                "removed. Re-run in an environment with the extension available.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"warn: sqlite-vec load skipped ({exc})", file=sys.stderr)
    return conn


def _ann_orphan_count(conn: sqlite3.Connection) -> int:
    """ANN companion rows whose base embedding is gone. Must be 0 after a run.

    This names a physical ANN table, which ``tests/storage/test_vector_index_seam.py``
    forbids inside ``topos/``. Deliberate and confined to here: the seam exists so
    WRITES go through ``VectorIndex``, and every write in this script does. This is
    a read, and it exists precisely to verify that the seam did its job — a check
    routed through the abstraction it is checking would prove nothing.
    """
    if not (
        _table_exists(conn, "signal_embeddings_vec_rowids")
        and _table_exists(conn, "signal_embeddings")
    ):
        return 0
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM signal_embeddings_vec_rowids r WHERE NOT EXISTS ("
                " SELECT 1 FROM signal_embeddings e WHERE e.embedding_id = r.id)"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        return 0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _protected(conn: sqlite3.Connection, entity_id: str, name: str | None = None) -> bool:
    try:
        from topos.features.lifecycle.derived_scrub import is_entity_protected

        return is_entity_protected(conn, entity_id, name)
    except Exception:  # noqa: BLE001 — a node with no black-hole schema protects nothing
        return False


# --------------------------------------------------------------- population (a)


def plan_fabricated_goals(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Goals extracted from a fan-out child, and the graph vertices they minted."""
    goal_rows = [
        str(r[0])
        for r in conn.execute(
            "SELECT goal_id FROM user_goals WHERE record_id LIKE ?", (f"%{LOC_SUFFIX}",)
        )
    ]
    texts = {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT goal_text FROM user_goals WHERE record_id LIKE ?"
            " AND goal_text IS NOT NULL AND goal_text <> ''",
            (f"%{LOC_SUFFIX}",),
        )
    }
    entities: List[str] = []
    protected_kept: List[str] = []
    if texts and _table_exists(conn, "entities"):
        placeholders = ",".join("?" for _ in texts)
        for entity_id, name in conn.execute(
            f"SELECT entity_id, normalized_name FROM entities"
            f" WHERE entity_type='goal' AND canonical_name IN ({placeholders})",
            sorted(texts),
        ):
            if _protected(conn, str(entity_id), str(name or "") or None):
                protected_kept.append(str(entity_id))
            else:
                entities.append(str(entity_id))

    edges = 0
    if entities and _table_exists(conn, "entity_edges"):
        placeholders = ",".join("?" for _ in entities)
        edges = conn.execute(
            f"SELECT COUNT(*) FROM entity_edges WHERE src_entity_id IN ({placeholders})"
            f" OR dst_entity_id IN ({placeholders})",
            entities + entities,
        ).fetchone()[0]

    return {
        "population": "fabricated_goals",
        "user_goals": goal_rows,
        "distinct_texts": len(texts),
        "goal_entities": entities,
        "entity_edges": edges,
        "protected_kept": protected_kept,
    }


def apply_fabricated_goals(conn: sqlite3.Connection, plan: Dict[str, Any]) -> Dict[str, int]:
    counts = {"user_goals": 0, "goal_entities": 0, "entity_edges": 0}
    if plan["user_goals"]:
        placeholders = ",".join("?" for _ in plan["user_goals"])
        counts["user_goals"] = conn.execute(
            f"DELETE FROM user_goals WHERE goal_id IN ({placeholders})", plan["user_goals"]
        ).rowcount
    for entity_id in plan["goal_entities"]:
        # Go through the guarded cascade rather than a raw DELETE, so a black hole
        # applied between plan and apply still wins.
        from topos.features.lifecycle.derived_scrub import _delete_entity_cascade

        result = _delete_entity_cascade(conn, entity_id)
        if result.get("skipped_protected"):
            continue
        counts["goal_entities"] += 1
        counts["entity_edges"] += int(result.get("edges") or 0)
    return counts


# --------------------------------------------------------------- population (b)


def plan_retired_github_fanout(conn: sqlite3.Connection) -> Dict[str, Any]:
    entry_ids = [
        str(r[0])
        for r in conn.execute(
            "SELECT entry_id FROM journal_entries WHERE source_id=?", (GITHUB_SOURCE,)
        )
    ]
    derived: Dict[str, int] = {}
    if entry_ids:
        placeholders = ",".join("?" for _ in entry_ids)
        for table, column in (
            ("signal_facts", "record_id"),
            ("signal_embeddings", "record_id"),
            ("timeline", "record_id"),
            ("entity_mentions", "record_id"),
            ("message_entities", "record_id"),
            ("signal_scores", "record_id"),
        ):
            if not _table_exists(conn, table):
                continue
            derived[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", entry_ids
            ).fetchone()[0]
    return {
        "population": "retired_github_fanout",
        "journal_entries": entry_ids,
        "derived": derived,
    }


def apply_retired_github_fanout(conn: sqlite3.Connection, plan: Dict[str, Any]) -> Dict[str, int]:
    entry_ids = plan["journal_entries"]
    counts: Dict[str, int] = {}
    if not entry_ids:
        return counts
    placeholders = ",".join("?" for _ in entry_ids)

    # Embeddings go through the vector index so the ANN companions and the
    # external-content FTS trigger both fire. A raw DELETE leaves both behind.
    if _table_exists(conn, "signal_embeddings"):
        ids = [
            str(r[0])
            for r in conn.execute(
                f"SELECT embedding_id FROM signal_embeddings WHERE record_id IN ({placeholders})",
                entry_ids,
            )
        ]
        if ids:
            try:
                from topos.storage.adapters.sqlite.stores import SQLiteVectorIndex

                SQLiteVectorIndex(conn).delete_embeddings(ids)
            except Exception as exc:  # noqa: BLE001
                print(f"  warn: ANN companion delete failed ({exc}); continuing", file=sys.stderr)
        counts["signal_embeddings"] = conn.execute(
            f"DELETE FROM signal_embeddings WHERE record_id IN ({placeholders})", entry_ids
        ).rowcount

    for table, column in (
        ("signal_facts", "record_id"),
        ("timeline", "record_id"),
        ("entity_mentions", "record_id"),
        ("message_entities", "record_id"),
        ("signal_scores", "record_id"),
    ):
        if not _table_exists(conn, table):
            continue
        counts[table] = conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})", entry_ids
        ).rowcount

    # The canonical rows go too. Leaving them is not stable: the next reprocess
    # re-derives everything above from them, and their content already exists on
    # the sibling activity_events rows the same push produced.
    counts["journal_entries"] = conn.execute(
        f"DELETE FROM journal_entries WHERE entry_id IN ({placeholders})", entry_ids
    ).rowcount
    return counts


# --------------------------------------------------------------- population (c)


def plan_unlinkable_orphans(conn: sqlite3.Connection) -> Dict[str, Any]:
    if not (_table_exists(conn, "timeline") and _table_exists(conn, "location_events")):
        return {"population": "unlinkable_orphans", "timeline_rows": []}
    rows = [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT t.record_id FROM timeline t"
            " WHERE t.record_id LIKE ?"
            "   AND NOT EXISTS (SELECT 1 FROM location_events l WHERE l.event_id = t.record_id)",
            (f"%{LOC_SUFFIX}",),
        )
    ]
    return {"population": "unlinkable_orphans", "timeline_rows": rows}


def apply_unlinkable_orphans(conn: sqlite3.Connection, plan: Dict[str, Any]) -> Dict[str, int]:
    rows = plan["timeline_rows"]
    if not rows:
        return {}
    placeholders = ",".join("?" for _ in rows)
    return {
        "timeline": conn.execute(
            f"DELETE FROM timeline WHERE record_id IN ({placeholders})", rows
        ).rowcount
    }


# --------------------------------------------------------------- population (d)


def plan_orphan_graph_nodes(conn: sqlite3.Connection) -> Dict[str, Any]:
    """``graph_nodes`` rows that no edge references and no entity resolves to.

    The entities job called ``upsert_node`` with no ``node_id``, so a fresh uuid4
    was minted for EVERY mention. Measured on the owner's node 2026-08-27: 32,631
    rows of type ``entity``, **0** matching a spine ``entity_id`` and **0**
    referenced by any edge — only 365 of 32,996 nodes were reachable at all.

    Deliberately narrow. Only ``entity``-typed rows that are BOTH unreferenced by
    any edge AND unresolvable in the entity spine qualify: the contact and
    conversation nodes form the connected ``message_frequency`` graph the legacy
    ``signal_list_graph`` route still serves, and a node keyed on a resolved
    ``person_id`` is exactly what PRD_04 asks for. Deleting by node_type alone
    would take those too.
    """
    if not _table_exists(conn, "graph_nodes"):
        return {"population": "orphan_graph_nodes", "graph_nodes": []}
    rows = [
        str(r[0])
        for r in conn.execute(
            """
            SELECT node_id FROM graph_nodes n
            WHERE n.node_type = 'entity'
              AND NOT EXISTS (
                    SELECT 1 FROM graph_edges e
                    WHERE e.src_node_id = n.node_id OR e.dst_node_id = n.node_id)
              AND NOT EXISTS (
                    SELECT 1 FROM entities x WHERE x.entity_id = n.node_id)
            """
        )
    ]
    return {"population": "orphan_graph_nodes", "graph_nodes": rows}


def apply_orphan_graph_nodes(conn: sqlite3.Connection, plan: Dict[str, Any]) -> Dict[str, int]:
    ids = plan["graph_nodes"]
    if not ids:
        return {}
    removed = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        placeholders = ",".join("?" for _ in chunk)
        removed += conn.execute(
            f"DELETE FROM graph_nodes WHERE node_id IN ({placeholders})", chunk
        ).rowcount
    return {"graph_nodes": removed}


POPULATIONS = {
    "goals": (plan_fabricated_goals, apply_fabricated_goals),
    "github": (plan_retired_github_fanout, apply_retired_github_fanout),
    "orphans": (plan_unlinkable_orphans, apply_unlinkable_orphans),
    "graph_nodes": (plan_orphan_graph_nodes, apply_orphan_graph_nodes),
}


def _summarize(plan: Dict[str, Any]) -> str:
    parts = []
    for key, value in plan.items():
        if key == "population":
            continue
        if isinstance(value, list):
            parts.append(f"{key}={len(value)}")
        elif isinstance(value, dict):
            parts.append(f"{key}={{{', '.join(f'{k}:{v}' for k, v in value.items())}}}")
        else:
            parts.append(f"{key}={value}")
    return "  ".join(parts)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--database", required=True, help="explicit path; no default, on purpose")
    parser.add_argument(
        "--population",
        action="append",
        choices=sorted(POPULATIONS),
        help="repeatable; default is all three",
    )
    parser.add_argument("--apply", action="store_true", help="write. Omit for a dry run.")
    parser.add_argument("--json", action="store_true", help="machine-readable plan on stdout")
    args = parser.parse_args(argv)

    db_path = Path(args.database).expanduser()
    if not db_path.exists():
        print(f"error: no database at {db_path}", file=sys.stderr)
        return 2

    chosen = args.population or sorted(POPULATIONS)
    exit_code = 0
    conn = _connect(db_path)
    ann_orphans_before = _ann_orphan_count(conn)
    report: Dict[str, Any] = {"database": str(db_path), "applied": bool(args.apply), "plans": []}
    try:
        for name in chosen:
            plan_fn, apply_fn = POPULATIONS[name]
            plan = plan_fn(conn)
            entry: Dict[str, Any] = {"plan": plan}
            if args.apply:
                entry["applied"] = apply_fn(conn, plan)
                conn.commit()
            report["plans"].append(entry)
            if not args.json:
                verb = "RETRACTED" if args.apply else "would retract"
                print(f"[{name}] {verb}: {_summarize(plan)}")
                if plan.get("protected_kept"):
                    print(f"  kept {len(plan['protected_kept'])} black-holed entities")
                if args.apply:
                    print(f"  applied: {entry['applied']}")
        if args.apply:
            # A retraction that leaves an orphaned vector leaves the thing it
            # retracted reachable, so this is an error, not a warning.
            ann_orphans_after = _ann_orphan_count(conn)
            leaked = ann_orphans_after - ann_orphans_before
            report["ann_orphans"] = {
                "before": ann_orphans_before,
                "after": ann_orphans_after,
                "leaked": leaked,
            }
            if leaked > 0:
                print(
                    f"ERROR: {leaked} ANN companion rows were left without a base "
                    "embedding. The vectors are still searchable. Load sqlite-vec and "
                    "re-run, or the retraction is incomplete.",
                    file=sys.stderr,
                )
                exit_code = 3
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif not args.apply:
        print("\nDry run. Re-run with --apply to write.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
