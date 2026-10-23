"""One-time repair for derived-object provenance refs that name a record table their record is not in.

``signal_objects.source_refs_json`` is a list of ``{"table", "record_id" | "id", ...}``, read by
``derived_scrub.ref_record_key``. A reader that looks a ref up in the table it names (a provenance
follower, the derived-drift sweep) finds nothing for many refs whose record is alive. Measured read-only
on the first live node checked (22 Oct 2026), none of the misses were a re-ingest of messages under new
ids. They were writers naming the wrong record table:

- The scope materializer stamps every ``message_entities`` row ``conversation_messages`` whatever source
  the row came from (AI chats, browser visits, the journal). It now cites the table that holds the
  record (``record_table``); it re-mirrors its rows every pass, so without that it would put the old
  name back.
- Facts extracted in July 2026 named grow-journal location rows ``journal_entries``, and GitHub commits
  too, as ``github:<repo>:<sha>`` from a journal fan-out that is not stored. The same commit lives in
  ``activity_events`` as ``github:push:<repo>:<sha>`` (``derivation/social_bench.py`` dates topic-cluster
  members across the same gap). None has been written since July.

A ref naming a record table (``RECORD_TABLES``) that does not hold its id moves to the one record table
that does. A ``github:`` ref that resolves nowhere moves to the one GitHub event whose id is the same
commit with an event kind in front. Only the table, and for a GitHub commit the id, change; every other
key of the ref, and every other ref, stay as written.

A source label used as a table name (``TABLE_ALIASES``: the person reading cites ``"iMessage"``) is read
as the table it stands for and never rewritten; ``resolve_ref`` is that reading for any follower. Left
alone and counted: a ref that resolves where it says or through its alias; one naming another label or a
derived table (topic clusters, goals, triage verdicts, entities: a second hop, not a repair); one with no
record id; one whose id is in two places (ambiguous); one whose stated ``source_id`` is not the matched
row's; one that matches nowhere (its record is gone: on the measured node, rows of removed demo sources
and topic mirrors of an import whose messages are no longer stored).

Rows are rewritten in place. None is deleted, closed or re-dated: ``updated_at`` stays, so no reader
that orders by it moves. The reads that decide the plan and the writes happen under one write-gate hold
and one transaction, rolled back on any exception. Re-running finds nothing to do, because a moved ref
resolves in its new table.

Review surface: ``source_refs_json`` stays in it (``permissions_v2.evidence.REVIEW_SURFACE_EXCLUSIONS``).
Lineage is part of what a review approved, so a review pinning a rewritten object reads stale until its
next pass, and a share index naming a rewritten fact as a witness rebuilds once. The permissions reads
match a ref by its id, and the ref keys they index (``permissions_fact_lineage_keys_v1``) are id keys
its triggers rebuild on the update; a ref only ever moves to the table its id is in, so no match is lost.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .derived_scrub import ref_record_key

logger = logging.getLogger("topos.features.lifecycle.source_ref_repair")

#: Canonical record tables, and the column a ref's id names in each: where a misfiled ref can move.
RECORD_TABLES: Tuple[Tuple[str, str], ...] = (
    ("conversation_messages", "message_id"),
    ("ai_chat_messages", "message_id"),
    ("journal_entries", "entry_id"),
    ("activity_events", "event_id"),
    ("location_events", "event_id"),
    ("calendar_events", "event_id"),
    ("profile_records", "record_id"),
)
_ID_COLUMN = dict(RECORD_TABLES)
#: Source labels writers put where a table belongs, and the table each stands for. Read, never rewritten.
TABLE_ALIASES: Dict[str, str] = {
    "iMessage": "conversation_messages",
    "Grow Journal": "journal_entries",
}
GITHUB_SOURCE_ID = "github_activity"
_GITHUB = "github:"
_ABSENT = object()


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _holds(conn: sqlite3.Connection, table: str, record_id: str) -> bool:
    column = _ID_COLUMN[table]
    try:
        return column in _columns(conn, table) and conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (record_id,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def record_table(conn: sqlite3.Connection, record_id: Any) -> Optional[str]:
    """The one canonical record table holding ``record_id``; None when none does, or more than one.

    For a writer that knows a record's id but not its table (the scope materializer): the table it cites
    is the one the repair below would move the ref to."""
    rid = str(record_id or "").strip()
    found = [table for table, _column in RECORD_TABLES if rid and _holds(conn, table, rid)]
    return found[0] if len(found) == 1 else None


def resolve_ref(conn: sqlite3.Connection, ref: Any) -> Optional[Tuple[str, str]]:
    """``(table, record id)`` of the record a ref points at, or None when its table does not hold it.

    The id is ``record_id``, else ``id`` (``derived_scrub.ref_record_key``); a source label in
    ``TABLE_ALIASES`` reads as the table it stands for."""
    key = ref_record_key(ref)
    if key is None:
        return None
    table, record_id = key
    table = TABLE_ALIASES.get(table, table)
    if table not in _ID_COLUMN or not _holds(conn, table, record_id):
        return None
    return table, record_id


class _Plan:
    """What the repair would do, read from the database as it is now."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.tables: Dict[str, str] = {}
        self.sourced: Set[str] = set()
        for table, column in RECORD_TABLES:
            columns = _columns(conn, table)
            if column in columns:
                self.tables[table] = column
                if "source_id" in columns:
                    self.sourced.add(table)
        self.db_tables = {str(name) for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()}
        self._rows: Dict[Tuple[str, str], Any] = {}
        self._github: Optional[Dict[str, List[str]]] = None
        self.objects = 0
        self.refs = 0
        self.left: Counter = Counter()
        self.relinked: Counter = Counter()
        self.github_commits = 0
        self.unmatched: Counter = Counter()
        #: (object_id, stored text, rewritten text)
        self.rewrites: List[Tuple[str, str, str]] = []
        for object_id, refs_json in conn.execute(
            "SELECT object_id, source_refs_json FROM signal_objects WHERE source_refs_json IS NOT NULL"
        ).fetchall():
            self.objects += 1
            try:
                refs = json.loads(refs_json)
            except (TypeError, ValueError):
                self.left["unparseable"] += 1
                continue
            if not isinstance(refs, list):
                self.left["unparseable"] += 1
                continue
            changed = False
            out = []
            for ref in refs:
                if not isinstance(ref, dict):
                    self.left["not_an_object"] += 1
                    out.append(ref)
                    continue
                moved = self._judge(ref)
                out.append(ref if moved is None else moved)
                changed = changed or moved is not None
            if changed:
                self.rewrites.append((str(object_id), refs_json, json.dumps(out)))

    def _source(self, table: str, record_id: str) -> Any:
        """The row's source_id ("" when the table has none), or ``_ABSENT`` when no row has that id."""
        key = (table, record_id)
        if key not in self._rows:
            column = self.tables[table]
            selected = "source_id" if table in self.sourced else "''"
            row = self._conn.execute(
                f"SELECT {selected} FROM {table} WHERE {column} = ? LIMIT 1", (record_id,)
            ).fetchone()
            self._rows[key] = _ABSENT if row is None else str(row[0] or "")
        return self._rows[key]

    def _github_events(self, record_id: str) -> List[str]:
        """The GitHub events naming the commit ``github:<repo>:<sha>`` cites, ``github:<kind>:<repo>:<sha>``."""
        if not record_id.startswith(_GITHUB) or "activity_events" not in self.sourced:
            return []
        if self._github is None:
            self._github = defaultdict(list)
            for (event_id,) in self._conn.execute(
                "SELECT event_id FROM activity_events WHERE source_id = ? AND event_id LIKE 'github:%'",
                (GITHUB_SOURCE_ID,),
            ).fetchall():
                kind, colon, commit = str(event_id)[len(_GITHUB):].partition(":")
                # The kind is one word; a repository path (owner/repo) is never mistaken for one.
                if colon and kind and "/" not in kind and commit:
                    self._github[commit].append(str(event_id))
        return list(self._github.get(record_id[len(_GITHUB):], ()))

    def _judge(self, ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The ref as it should read, or None when it stays as written. Counts either way."""
        self.refs += 1
        key = ref_record_key(ref)
        if key is None:
            self.left["no_record_id"] += 1
            return None
        table, record_id = key
        if table in TABLE_ALIASES:
            self.left["alias_resolves" if resolve_ref(self._conn, ref) else "alias_unmatched"] += 1
            return None
        if table not in _ID_COLUMN:
            self.left["not_a_record_table" if table in self.db_tables else "label"] += 1
            return None
        if table in self.tables and self._source(table, record_id) is not _ABSENT:
            self.left["resolves"] += 1
            return None
        hits = [(t, record_id) for t in self.tables if self._source(t, record_id) is not _ABSENT]
        if not hits:
            hits = [("activity_events", event_id) for event_id in self._github_events(record_id)]
        if not hits:
            self.left["unmatched"] += 1
            self.unmatched[table] += 1
            return None
        if len(hits) > 1:
            self.left["ambiguous"] += 1
            return None
        new_table, new_id = hits[0]
        stated_source = str(ref.get("source_id") or "")
        if stated_source and new_table in self.sourced and self._source(new_table, new_id) != stated_source:
            self.left["source_mismatch"] += 1
            return None
        moved = dict(ref)
        moved["table"] = new_table
        if new_id != record_id:
            for field in ("record_id", "id"):
                if field in moved and str(moved[field]).strip() == record_id:
                    moved[field] = new_id
            self.github_commits += 1
        self.relinked[f"{table}->{new_table}"] += 1
        return moved

    def report(self, status: str) -> Dict[str, Any]:
        return {
            "status": status,
            "objects_scanned": self.objects,
            "refs_scanned": self.refs,
            "objects_rewritten": len(self.rewrites),
            "refs_relinked": sum(self.relinked.values()),
            # "<stated table>-><table it moved to>"
            "relinked": dict(sorted(self.relinked.items())),
            # Of those, refs whose id also changed: GitHub commits moved to their push event.
            "github_commits_relinked": self.github_commits,
            "left_alone": dict(sorted(self.left.items())),
            "unmatched_by_stated_table": dict(sorted(self.unmatched.items())),
        }


def repair_source_refs(conn: sqlite3.Connection, *, dry_run: bool = False) -> Dict[str, Any]:
    """Move every provenance ref that misses its stated record table to the one record table that holds it."""
    try:
        if not _columns(conn, "signal_objects") >= {"object_id", "source_refs_json"}:
            return {"status": "skipped", "reason": "no signal_objects table", "refs_relinked": 0}
        if dry_run:
            return _Plan(conn).report("dry_run")
    except sqlite3.Error as exc:
        logger.warning("source ref repair could not read signal_objects: %s", exc)
        return {"status": "skipped", "error": str(exc), "refs_relinked": 0}

    from ...storage.db.write_gate import commit_connection, with_db_write

    with with_db_write():
        try:
            # Read under the gate: a fact write between reading and writing would otherwise be overwritten.
            plan = _Plan(conn)
            report = plan.report("ok")
            raced = 0
            for object_id, stored, rewritten in plan.rewrites:
                raced += conn.execute(
                    "UPDATE signal_objects SET source_refs_json = ? WHERE object_id = ? AND source_refs_json = ?",
                    (rewritten, object_id, stored),
                ).rowcount != 1
            report["objects_raced"] = raced
            commit_connection(conn)
        except BaseException:
            conn.rollback()
            raise

    logger.info(
        "source ref repair: %d ref(s) relinked in %d object(s) (%d GitHub commit(s)), left alone %s",
        report["refs_relinked"], report["objects_rewritten"], report["github_commits_relinked"],
        report["left_alone"],
    )
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m topos.features.lifecycle.source_ref_repair --db PATH [--dry-run]``: counts only.

    ``--dry-run`` opens the database read-only, so it cannot write whatever the code path does."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--db", required=True, help="the node database to repair (a copy, unless you mean it)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        from pathlib import Path

        conn = sqlite3.connect(f"{Path(args.db).resolve().as_uri()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(args.db)
    try:
        print(json.dumps(repair_source_refs(conn, dry_run=args.dry_run), indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
