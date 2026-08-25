#!/usr/bin/env python3
"""P0-1 — fold the misnamed ``test-dataset`` iMessage corpus into the owner's dataset.

WHY THIS EXISTS
---------------
The iMessage source was registered with ``dataset_id='test-dataset'`` (almost
certainly during install QA) and then real data was synced into it. It is not a
fixture: the source config points at ``~/Library/Messages/chat.db``, no demo
pack is installed, the largest thread holds 1,649 messages, and 38 of its 153
phone identifiers already appear in the owner's address book. See
``PLAN_SOCIAL_GRAPH.md`` §2a for the full determination.

The consequence is that every join between messages and the address book is
broken, because the two halves sit under different ``dataset_id`` values. Every
layer of the social graph joins across exactly that seam, so this runs first.

WHAT IT DOES
------------
1. Contacts that already exist in the address book (matched on a normalised
   phone identifier) are MERGED: references are rewritten to the address-book
   contact, which is the one carrying a real display name, and the messenger
   duplicate is dropped.
2. Contacts with no counterpart are RE-POINTED — they are real people the owner
   texts who were never saved to the address book, and deleting them would
   delete the relationship.
3. Messages, conversations and participants are re-pointed.
4. The ingestion checkpoint and source registration move, so the next
   incremental sync resumes against the unified corpus instead of recreating
   the split.
5. Messenger analytics are DELETED for both datasets, not migrated. The
   canonical rows describe periods 2025-11..2026-02 from a corpus that no
   longer exists, and participant ids embed the old dataset id. They are
   recomputed from the unified corpus afterwards.

SAFETY
------
- ``--dry-run`` (the default) reports and changes nothing.
- ``--db`` runs against a copy; always rehearse there first.
- ``--apply`` requires the engine to be stopped: it holds long-lived
  connections, and this rewrites the identity columns every later layer joins
  on. Take a backup first (``sqlite3 db ".backup copy.db"``).
- Idempotent: re-running after a successful pass finds nothing to do.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Dict, List, Tuple

STALE_DATASET = "test-dataset"


def _normalise_phone(raw: str) -> str:
    v = "".join(ch for ch in str(raw or "") if ch not in "+- ()")
    return v[-10:] if len(v) > 10 else v


def _canonical_dataset(conn: sqlite3.Connection) -> str:
    """The dataset the address book lives under — the merge target."""
    row = conn.execute(
        "SELECT dataset_id, COUNT(*) c FROM contacts WHERE dataset_id != ? "
        "GROUP BY dataset_id ORDER BY c DESC LIMIT 1",
        (STALE_DATASET,),
    ).fetchone()
    if not row:
        sys.exit("no non-stale dataset found in contacts; nothing to unify into")
    return str(row[0])


def _merge_map(conn: sqlite3.Connection, canonical: str) -> Dict[str, str]:
    """stale contact_id -> canonical contact_id, matched on a phone identifier."""
    rows = conn.execute(
        "SELECT dataset_id, contact_id, identifier FROM contact_identifiers "
        "WHERE identifier_type='phone'"
    ).fetchall()
    stale: Dict[str, str] = {}
    canon: Dict[str, str] = {}
    for dataset_id, contact_id, identifier in rows:
        key = _normalise_phone(identifier)
        if not key:
            continue
        if dataset_id == STALE_DATASET:
            stale.setdefault(key, str(contact_id))
        elif dataset_id == canonical:
            canon.setdefault(key, str(contact_id))
    return {cid: canon[k] for k, cid in stale.items() if k in canon and canon[k] != cid}


def survey(conn: sqlite3.Connection, canonical: str) -> Tuple[Dict[str, int], Dict[str, str]]:
    counts: Dict[str, int] = {}
    for table in (
        "conversation_messages",
        "conversations",
        "conversation_participants",
        "contacts",
        "contact_identifiers",
        "ingestion_checkpoints",
        "user_ingestion_sources",
        "messenger_social_edges",
        "messenger_participant_importance",
        "messenger_communities",
    ):
        try:
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE dataset_id=?", (STALE_DATASET,)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = 0
    counts["_stale_canonical_analytics"] = conn.execute(
        "SELECT COUNT(*) FROM messenger_social_edges WHERE dataset_id=?", (canonical,)
    ).fetchone()[0]
    return counts, _merge_map(conn, canonical)


def apply(conn: sqlite3.Connection, canonical: str, merges: Dict[str, str]) -> List[str]:
    log: List[str] = []
    cur = conn.cursor()

    # 1. Rewrite contact references for merged duplicates, THEN drop them.
    #    Order matters: the FK-less schema would otherwise orphan the rows.
    orphans_dropped = 0
    for stale_cid, canon_cid in merges.items():
        # `contact_id` is part of this table's primary key, so the UPDATE is
        # IGNORED wherever the merged row already exists — both contacts were
        # in the same conversation. Those leftovers must be deleted, not left
        # pointing at a contact this loop is about to remove; the rehearsal
        # caught exactly 2 of them becoming orphans.
        cur.execute(
            "UPDATE OR IGNORE conversation_participants SET contact_id=? WHERE contact_id=?",
            (canon_cid, stale_cid),
        )
        cur.execute("DELETE FROM conversation_participants WHERE contact_id=?", (stale_cid,))
        orphans_dropped += cur.rowcount
        cur.execute(
            "UPDATE OR IGNORE entities SET contact_id=? WHERE contact_id=?",
            (canon_cid, stale_cid),
        )
        # Same hazard: `entities.contact_id` is not unique-constrained today,
        # but a future index would make this silently skip too. Point any
        # stragglers at the survivor rather than leaving them dangling.
        cur.execute(
            "UPDATE entities SET contact_id=? WHERE contact_id=?", (canon_cid, stale_cid)
        )
        # The duplicate's identifiers move over so the address-book contact
        # gains any handle only the messenger knew about.
        cur.execute(
            "UPDATE OR IGNORE contact_identifiers SET contact_id=?, dataset_id=? WHERE contact_id=?",
            (canon_cid, canonical, stale_cid),
        )
        cur.execute("DELETE FROM contact_identifiers WHERE contact_id=?", (stale_cid,))
        cur.execute("DELETE FROM contacts WHERE contact_id=?", (stale_cid,))
    log.append(f"merged {len(merges)} duplicate contacts into the address book")
    if orphans_dropped:
        log.append(f"  dropped {orphans_dropped} participant row(s) the merge made redundant")

    # 2. Re-point everything that remains under the stale dataset.
    for table in (
        "contacts",
        "contact_identifiers",
        "conversations",
        "conversation_messages",
        "conversation_participants",
        "ingestion_checkpoints",
        "user_ingestion_sources",
    ):
        cur.execute(
            f"UPDATE OR IGNORE {table} SET dataset_id=? WHERE dataset_id=?",
            (canonical, STALE_DATASET),
        )
        log.append(f"re-pointed {cur.rowcount:>5} rows in {table}")
        # Anything left could not move (a PK it would collide with); it is a
        # duplicate of a row already present under the canonical id.
        cur.execute(f"DELETE FROM {table} WHERE dataset_id=?", (STALE_DATASET,))
        if cur.rowcount:
            log.append(f"  dropped {cur.rowcount} colliding duplicate(s) from {table}")

    # 3. Analytics are recomputed, never migrated — participant ids embed the
    #    old dataset id, and the canonical generation describes a dead corpus.
    for table in (
        "messenger_social_edges",
        "messenger_participant_importance",
        "messenger_communities",
    ):
        cur.execute(f"DELETE FROM {table}")
        log.append(f"cleared {cur.rowcount:>5} rows from {table} (recompute required)")

    conn.commit()
    return log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.path.expanduser("~/.topos/database.db"))
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=30000")
    canonical = _canonical_dataset(conn)
    counts, merges = survey(conn, canonical)

    print(f"canonical dataset : {canonical}")
    print(f"stale dataset     : {STALE_DATASET}")
    print("\nrows under the stale dataset:")
    for table, n in counts.items():
        if table.startswith("_"):
            continue
        print(f"  {table:<34} {n:>6}")
    print(f"\n  stale analytics under the canonical id  {counts['_stale_canonical_analytics']:>6}"
          "  (dead corpus — will be cleared)")
    print(f"  contacts to merge into the address book {len(merges):>6}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply (engine stopped, backup taken).")
        return 0

    print("\napplying…")
    for line in apply(conn, canonical, merges):
        print("  " + line)

    left = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE dataset_id=?", (STALE_DATASET,)
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE dataset_id=?", (canonical,)
    ).fetchone()[0]
    print(f"\nmessages still under the stale dataset: {left} (must be 0)")
    print(f"messages under the canonical dataset  : {total}")
    print("\nNEXT: restart the engine, then recompute analytics:")
    print("  POST /v1/messenger-analytics/recompute")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
