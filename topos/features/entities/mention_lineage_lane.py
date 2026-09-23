"""The stopped-node lane for the entity-mention lineage repair.

``repair_mention_lineage`` is the repair; this is the door a person uses to run
it on a database file with no node attached — a stopped node's database, or a
copy of one. The upgrade runner's ``derived_rebuild`` target is the other door
and needs none of this, because the node that runs it already owns the file.

What the lane adds, and why each piece is there:

* **An explicit path, opened raw.** ``sqlite3.connect`` on the named file, no
  ``AdapterFactory``, no ``get_db_connection``, no migration runner. Opening a
  node's database through a checkout whose registry is ahead of the installed
  node stamps ``user_version`` past it and fences the node out (2026-08-19,
  2026-09-16). The lane reads ``user_version`` before and after and fails if it
  moved.
* **Stopped means no sidecars.** A node that has the file open in WAL mode
  keeps ``-wal``/``-shm`` beside it; a clean stop removes them. The lane
  refuses while any ``-wal``, ``-shm`` or ``-journal`` sidecar exists, and
  checkpoints and closes so it leaves none behind. A lock probe cannot stand
  in: under WAL an idle node holds no lock a writer would see.
* **The owner's home is opt-in.** A path under ``~/.topos`` is refused unless
  ``--node-stopped`` says the operator stopped that node. Copies live anywhere
  else and need no flag.
* **Counts per canonical table, before and after.** :func:`lineage_coverage`
  is the table the D8 decision reads: rows, rows with a stamped mention,
  unstamped mentions that resolve to the table, rows extracted into
  ``message_entities``, and those extracted rows with no mention at all. Every
  value is an integer; nothing selects or prints content.
* **Resumable by construction.** Each pass of the repair derives its remaining
  work from the database and commits per chunk, so an interrupted run leaves
  committed chunks in place and the next run finishes the rest. There is no
  side ledger to fall out of step with the file.

Nothing here opens a database the caller did not name.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .mention_lineage import (
    CANONICAL_ID_COLUMNS,
    _has_column,
    _json_dict,
    _present_tables,
    _SpineIndex,
    repair_mention_lineage,
    resolve_record_tables,
    screen_extracted,
)

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class StoppedNodeLaneError(RuntimeError):
    """The lane refused to open or finish on the named database."""


def owner_home_root() -> Path:
    return (Path.home() / ".topos").resolve()


def sidecars(path: Path) -> List[str]:
    """Names of the journal sidecars that exist beside ``path``."""
    return [suffix for suffix in SIDECAR_SUFFIXES if Path(f"{path}{suffix}").exists()]


def check_stopped(path: Path, *, node_stopped: bool = False) -> Path:
    """The resolved path, or :class:`StoppedNodeLaneError` saying why not."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise StoppedNodeLaneError(f"not a database file: {resolved}")
    home = owner_home_root()
    if (resolved == home or home in resolved.parents) and not node_stopped:
        raise StoppedNodeLaneError(
            "refusing a database under ~/.topos without --node-stopped; "
            "stop the node first, or run the lane on a copy"
        )
    present = sidecars(resolved)
    if present:
        raise StoppedNodeLaneError(
            f"database has open-journal sidecars {present}: a process has it open or "
            "it was not closed cleanly; stop the node (or close the copy) first"
        )
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def lineage_coverage(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Per canonical table, the lineage counts D8 reads. Integers only.

    * ``rows`` — rows in the table.
    * ``rows_with_mention`` — rows at least one mention cites by ``record_id``.
    * ``rows_with_stamped_mention`` — rows at least one mention cites AND
      names this table; the only lineage a table-scoped read travels along.
    * ``mentions_resolving`` / ``mentions_stamped_here`` — mentions whose
      ``record_id`` is in the table, and of those the ones stamped with it.
    * ``unstamped_mentions`` — mentions with no stamp whose record is here.
    * ``extracted_rows`` — rows with at least one ``message_entities`` row.
    * ``extracted_unlinked_rows`` — of those, rows no mention cites at all.
    """
    out: Dict[str, Any] = {"tables": {}}
    if not _exists(conn, "entity_mentions"):
        out["skipped"] = "no entity_mentions table"
        return out
    has_ner = _exists(conn, "message_entities")
    out["mentions_total"] = _count(conn, "SELECT COUNT(*) FROM entity_mentions")
    out["mentions_unstamped_total"] = _count(
        conn, "SELECT COUNT(*) FROM entity_mentions WHERE COALESCE(canonical_table,'')=''"
    )
    for table, col in CANONICAL_ID_COLUMNS.items():
        if not _exists(conn, table):
            continue
        if not _has_column(conn, table, col):
            out.setdefault("id_column_missing", []).append(table)
            continue
        row: Dict[str, int] = {"rows": _count(conn, f"SELECT COUNT(*) FROM {table}")}
        ids = f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL"
        row["rows_with_mention"] = _count(
            conn,
            f"SELECT COUNT(*) FROM {table} t WHERE EXISTS"
            f" (SELECT 1 FROM entity_mentions m WHERE m.record_id = t.{col})",
        )
        row["rows_with_stamped_mention"] = _count(
            conn,
            f"SELECT COUNT(*) FROM {table} t WHERE EXISTS"
            f" (SELECT 1 FROM entity_mentions m WHERE m.record_id = t.{col}"
            f"  AND m.canonical_table = ?)",
            (table,),
        )
        row["mentions_resolving"] = _count(
            conn, f"SELECT COUNT(*) FROM entity_mentions WHERE record_id IN ({ids})"
        )
        row["mentions_stamped_here"] = _count(
            conn,
            f"SELECT COUNT(*) FROM entity_mentions WHERE canonical_table = ?"
            f" AND record_id IN ({ids})",
            (table,),
        )
        row["unstamped_mentions"] = _count(
            conn,
            f"SELECT COUNT(*) FROM entity_mentions WHERE COALESCE(canonical_table,'')=''"
            f" AND record_id IN ({ids})",
        )
        if has_ner:
            extracted = (
                f"SELECT DISTINCT record_id FROM message_entities"
                f" WHERE record_id IN ({ids})"
            )
            row["extracted_rows"] = _count(conn, f"SELECT COUNT(*) FROM ({extracted})")
            row["extracted_unlinked_rows"] = _count(
                conn,
                f"SELECT COUNT(*) FROM ({extracted}) x WHERE NOT EXISTS"
                f" (SELECT 1 FROM entity_mentions m WHERE m.record_id = x.record_id)",
            )
        else:
            row["extracted_rows"] = 0
            row["extracted_unlinked_rows"] = 0
        out["tables"][table] = row
    return out


#: Why an extracted row still has no mention, strongest reason first. A row
#: takes the first class any of its ``message_entities`` rows reaches.
UNLINKED_CLASSES = (
    "resolves_to_existing_entity",  # a link the repair should have written
    "named_but_no_existing_entity",  # passes every writer filter; would need a mint
    "named_below_confidence_floor",  # a person/org/place... the writer drops
    "excluded_by_owner",
    "invalid_surface_only",
    "value_types_only",  # dates, numbers, money: the writer never links these
)


def extracted_unlinked_breakdown(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Per canonical table, the extracted rows with no mention, by reason.

    ``extracted_unlinked_rows`` alone reads every such row as a coverage hole.
    It is not: a row whose NER output is only dates and amounts, or only
    low-confidence spans, is one the resolver processed and would never link.
    The classes separate that from a row a link is owed on. Entity text is read,
    screened and discarded in-process; only counts leave. ``person_below_floor``
    counts rows whose strongest class is the confidence floor AND that hold a
    person span — the rows a name scan exists for.
    """
    out: Dict[str, Any] = {"classes": list(UNLINKED_CLASSES), "tables": {}}
    if not (_exists(conn, "message_entities") and _exists(conn, "entity_mentions")):
        return out
    present = _present_tables(conn)
    index = _SpineIndex(conn)
    rank = {name: i for i, name in enumerate(UNLINKED_CLASSES)}
    best: Dict[str, int] = {}
    person_low: set = set()
    for record_id, entity_text, provider, payload_json in conn.execute(
        """
        SELECT e.record_id, e.entity_text, e.provider, e.payload_json
        FROM message_entities e
        WHERE COALESCE(e.record_id,'') <> ''
          AND NOT EXISTS (SELECT 1 FROM entity_mentions m WHERE m.record_id = e.record_id)
        """
    ):
        screened = screen_extracted(index, entity_text, provider, _json_dict(payload_json))
        reason, etype = screened["reason"], screened["etype"]
        if reason is None:
            cls = (
                "resolves_to_existing_entity"
                if index.resolve(screened["surface"], etype)
                else "named_but_no_existing_entity"
            )
        elif reason == "low_confidence":
            cls = "value_types_only" if etype is None else "named_below_confidence_floor"
            if etype == "person":
                person_low.add(str(record_id))
        elif reason == "value_type":
            cls = "value_types_only"
        elif reason == "invalid_surface":
            cls = "invalid_surface_only"
        else:
            cls = "excluded_by_owner"
        key = str(record_id)
        if key not in best or rank[cls] < best[key]:
            best[key] = rank[cls]
    for record_id, r in best.items():
        matches = resolve_record_tables(conn, record_id, present=present)
        table = matches[0] if len(matches) == 1 else "<unattributed>"
        row = out["tables"].setdefault(
            table, {**{name: 0 for name in UNLINKED_CLASSES}, "person_below_floor": 0}
        )
        cls = UNLINKED_CLASSES[r]
        row[cls] += 1
        if cls == "named_below_confidence_floor" and record_id in person_low:
            row["person_below_floor"] += 1
    return out


def _assert_counts_only(value: Any, path: str = "report") -> None:
    """Every leaf is a number, a bool, None, or a short closed-shape string.

    The lane's output goes to a terminal and a file; the repair's counters and
    the coverage table are all integers, and the few strings are table names,
    hex digests, a path the caller supplied and fixed messages. A leaf outside
    that is a bug in this module, and it fails before anything is printed.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 64:
                raise StoppedNodeLaneError(f"non-count key at {path}")
            _assert_counts_only(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_counts_only(item, f"{path}[{index}]")
    elif isinstance(value, (bool, int, float)) or value is None:
        return
    elif isinstance(value, str):
        if len(value) > 512:
            raise StoppedNodeLaneError(f"oversized string at {path}")
    else:
        raise StoppedNodeLaneError(f"unexpected value type at {path}")


def run_stopped_node_lane(
    database: Path,
    *,
    dry_run: bool = False,
    batch_size: int = 2000,
    node_stopped: bool = False,
    hash_file: bool = False,
) -> Dict[str, Any]:
    """Coverage before, the repair, coverage after, and the fence checks."""
    path = check_stopped(database, node_stopped=node_stopped)
    report: Dict[str, Any] = {"lane": "stopped-node", "dry_run": bool(dry_run)}
    if hash_file:
        report["sha256_before"] = file_sha256(path)
    # A dry run is a pure read of a file nothing else has open (checked above):
    # immutable, so it creates no sidecars and cannot change a byte.
    uri = f"file:{path}?mode=ro&immutable=1" if dry_run else f"file:{path}"
    conn = sqlite3.connect(uri, uri=True, timeout=0.0)
    try:
        conn.execute("PRAGMA busy_timeout = 0")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        report["user_version"] = user_version
        report["coverage_before"] = lineage_coverage(conn)
        report["repair"] = repair_mention_lineage(conn, dry_run=dry_run, batch_size=batch_size)
        if not dry_run:
            conn.commit()
            report["coverage_after"] = lineage_coverage(conn)
        report["extracted_unlinked_by_reason"] = extracted_unlinked_breakdown(conn)
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != user_version:
            raise StoppedNodeLaneError("user_version moved; the lane must never migrate")
        report["quick_check_ok"] = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        if not dry_run:
            journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if journal == "wal":
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    left = sidecars(path)
    if left:
        raise StoppedNodeLaneError(f"sidecars left behind after close: {left}")
    if hash_file:
        report["sha256_after"] = file_sha256(path)
    _assert_counts_only(report)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import logging

    parser = argparse.ArgumentParser(
        description=(
            "Stopped-node lane: repair entity-mention lineage on a database file "
            "no node has open, and report per-table coverage before and after."
        )
    )
    parser.add_argument("--database", required=True, help="the database file to repair")
    parser.add_argument("--dry-run", action="store_true", help="open read-only; count, write nothing")
    parser.add_argument("--batch-size", type=int, default=2000, help="rows per committed chunk")
    parser.add_argument(
        "--node-stopped",
        action="store_true",
        help="required for a database under ~/.topos: the operator stopped that node",
    )
    parser.add_argument("--hash", action="store_true", help="record the file's sha256 before and after")
    parser.add_argument("--report", help="also write the JSON report here (mode 0600)")
    args = parser.parse_args(argv)
    # The repair's own log lines are counts, but the lane's contract is its
    # report; keep library logging out of the terminal for the run, and put
    # the level back after — a leaked level silenced later loggers in-process.
    topos_logger = logging.getLogger("topos")
    previous = topos_logger.level
    topos_logger.setLevel(logging.ERROR)
    try:
        report = run_stopped_node_lane(
            Path(args.database),
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            node_stopped=args.node_stopped,
            hash_file=args.hash,
        )
    except StoppedNodeLaneError as exc:
        print(json.dumps({"refused": str(exc)}), flush=True)
        return 2
    finally:
        topos_logger.setLevel(previous)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.report:
        target = Path(args.report)
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text + "\n")
    print(text, flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
