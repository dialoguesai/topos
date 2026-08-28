#!/usr/bin/env python3
"""Mint the entities the journal DECLARES, for rows ingested before that lane existed.

WHY A SCRIPT AND NOT THE BACKFILL ENDPOINT
------------------------------------------
`/v1/sources/{source_id}/enrichments/entities/backfill` is source-scoped, and neither
`grow_journal` nor `grow_data_file` is in `sources.registry.REGISTRY` (22 sources, neither
journal id among them). So the journal cannot be reached by the normal re-enrichment lane
at all. Registering it is the real fix; this closes the gap in the meantime.

WHAT IT DOES
------------
Exactly what `entities_job` now does at ingest, over rows that predate it:

  * `journal_entries.people`   -> person mentions, one per name (STRUCTURED_ENTITY_FIELDS)
  * `journal_entries.category` -> a project entity + self--worked_on-->project
                                  (DECLARED_ENTITY_MAPPINGS), pastimes excluded
  * both folded into ONE per-record bucket, then co-occurrence over that bucket

It is idempotent: mentions are INSERT OR IGNORE and edges upsert by (src, dst, type).

SAFETY
------
Writes to whatever `TOPOS_DATABASE_PATH` resolves to. Touching the live database at
~/.topos/database.db additionally requires `REPAIR_ALLOW_LIVE=yes`, so a copy is the
default and the live run has to be typed on purpose. Back up first:

    sqlite3 "file:$HOME/.topos/database.db?mode=ro" ".backup /tmp/pre-journal-backfill.db"

Stop the node before running: two writers on one SQLite file is how this codebase has
corrupted transactions before.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if not os.environ.get("TOPOS_DATABASE_PATH"):
        print("refusing: set TOPOS_DATABASE_PATH to the database to repair", file=sys.stderr)
        return 2

    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        print("no database connection", file=sys.stderr)
        return 2
    resolved = conn.execute("PRAGMA database_list").fetchall()[0][2]
    print(f"database: {resolved}")
    if "/.topos/database.db" in resolved and os.environ.get("REPAIR_ALLOW_LIVE") != "yes":
        print("refusing to write the live database without REPAIR_ALLOW_LIVE=yes",
              file=sys.stderr)
        return 2

    from topos.features.entities.declared_mappings import extract_declared_entities
    from topos.features.entities.edges import record_cooccurrence_pairs, update_edge
    from topos.features.entities.resolver import EntityResolver
    from topos.features.entities.structured_fields import (
        STRUCTURED_CONFIDENCE,
        record_structured_mentions,
    )

    cur = conn.execute("SELECT * FROM journal_entries")
    cols = [d[0] for d in cur.description]
    messages = []
    for row in cur.fetchall():
        msg = dict(zip(cols, row))
        msg["_table"] = "journal_entries"
        msg["record_id"] = msg["entry_id"]
        msg["event_at"] = msg.get("entry_at")
        messages.append(msg)
    print(f"journal rows: {len(messages)}")

    resolver = EntityResolver(conn)
    by_record: dict = {}

    declared = 0
    for msg in messages:
        for rec in extract_declared_entities(
            msg, record_id=msg["record_id"], event_at=msg["event_at"]
        ):
            entity_id, _ = resolver.resolve(
                rec["entity_text"], entity_type=rec["entity_type"],
                record_id=rec["record_id"], queue_review=False,
            )
            if not entity_id:
                continue
            resolver.record_mention(
                entity_id, record_id=rec["record_id"], surface_text=rec["entity_text"],
                source_id=rec["source_id"], canonical_table="journal_entries",
                confidence=STRUCTURED_CONFIDENCE, event_at=rec["event_at"],
                authored_by_owner=1,
            )
            by_record.setdefault(rec["record_id"], []).append(entity_id)
            declared += 1

    for record_id, ids in record_structured_mentions(conn, resolver, messages).items():
        by_record.setdefault(record_id, []).extend(ids)
    conn.commit()
    print(f"declared project mentions: {declared}   records with structured mentions: "
          f"{len(by_record)}")

    at = {m["record_id"]: m["event_at"] for m in messages}
    pairs = 0
    for record_id, ids in by_record.items():
        for src, dst in record_cooccurrence_pairs(ids):
            update_edge(conn, src_entity_id=src, dst_entity_id=dst,
                        edge_type="co_occurrence", event_at=at.get(record_id))
            pairs += 1
    conn.commit()
    print(f"co-occurrence pairs folded: {pairs}")

    # What the person cards will now say, so the run reports its own outcome rather than
    # leaving the operator to go and look.
    from topos.analytics.person_graph import attach_coactivity

    nodes = [
        {"node_id": f"ent:{eid}", "entity_id": eid, "label": name, "is_owner": False}
        for eid, name in conn.execute(
            "SELECT entity_id, canonical_name FROM entities WHERE entity_type='person'")
    ]
    attach_coactivity(conn, nodes)
    readings = sorted(
        (n for n in nodes if n.get("coactivity")),
        key=lambda n: -n["coactivity"]["sessions"],
    )
    print(f"\nco-activity readings: {len(readings)}")
    for node in readings:
        co = node["coactivity"]
        print(f"   {str(node['label'])[:24]:24} {co['label']} · {co['sessions']} sessions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
