#!/usr/bin/env python3
"""Count message bodies that are un-decoded iMessage `attributedBody` archives.

An iMessage with no plain `text` keeps its body in the `attributedBody` column
as an NSAttributedString archive. Until the decode in
`topos/ingestion/sources/imessage_reader.py` landed, the reader scraped the
longest printable byte run out of that archive and stored it, which produced
three distinct shapes on disk:

  streamtyped   the whole body collapsed to the format's header word
  classtable    a crumb of the keyed archive's class table
                ("()*+Z$classnameX$classesWNSValue*,XNSObject...")
  lengthprefix  real text with the archive's length byte still on the front
                ("++can you send me that link again when you get a chance")

The third shape is the one that matters most and is the easiest to miss: it
looks like text, so it passed `is_derivable_content` and reached the embedding
index. Searching for blob-shaped bodies alone undercounts the damage by more
than half.

Read-only. Run it before and after a re-sync to see whether the repair landed:

    python3 scripts/audit_imessage_body_decode.py
    python3 scripts/audit_imessage_body_decode.py --db ~/.topos/database.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from typing import Optional

# Tables whose `content` came through a source reader and can carry the damage.
CONTENT_TABLES = ("conversation_messages", "ai_chat_messages")
# Derived text, to show how far a body travelled before anyone looked at it.
DERIVED_TABLES = (("signal_embeddings", "search_text"), ("signal_embeddings", "text_preview"))


def classify(body: Optional[str]) -> Optional[str]:
    """Name the damage in one body, or None when it reads as text.

    `lengthprefix` is checked arithmetically rather than by pattern: typedstream
    writes `+` then the body's length in bytes, so the second character's code
    point must equal the UTF-8 length of the rest. A message that merely opens
    with "+" does not satisfy that, which keeps "+1 to that plan" out of the
    count.
    """
    if not body:
        return None
    if "streamtyped" in body:
        return "streamtyped"
    if "$classname" in body or "$classes" in body or "bplist00" in body:
        return "classtable"
    if len(body) >= 3 and body[0] == "+" and 0x20 <= ord(body[1]) <= 0x7E:
        if ord(body[1]) == len(body[2:].encode("utf-8", errors="ignore")):
            return "lengthprefix"
    return None


def audit(db_path: str) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")
    total_damaged = 0

    for table in CONTENT_TABLES:
        try:
            rows = conn.execute(f"SELECT source_id, content FROM {table}").fetchall()
        except sqlite3.OperationalError as exc:
            print(f"{table}: skipped ({exc})")
            continue
        counts: Counter = Counter()
        per_source: Counter = Counter()
        for source_id, content in rows:
            shape = classify(content)
            if shape:
                counts[shape] += 1
                per_source[source_id or "?"] += 1
        damaged = sum(counts.values())
        total_damaged += damaged
        share = f"{100 * damaged / len(rows):.1f}%" if rows else "n/a"
        print(f"\n{table}: {damaged} damaged of {len(rows)} ({share})")
        for shape in ("streamtyped", "classtable", "lengthprefix"):
            if counts[shape]:
                print(f"    {shape:14s} {counts[shape]}")
        for source_id, n in per_source.most_common():
            print(f"    via {source_id}: {n}")

    for table, column in DERIVED_TABLES:
        try:
            rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        damaged = sum(1 for (value,) in rows if classify(value))
        if damaged:
            print(f"\n{table}.{column}: {damaged} damaged of {len(rows)}")
            print("    these are vectors of corrupted text; re-embed after healing content")

    conn.close()
    if total_damaged == 0:
        print("\nNo un-decoded archive bodies found.")
        return 0
    print(
        f"\n{total_damaged} bodies still hold archive residue. "
        "Re-sync the source with a history window (mode 6m/1y/5y, which restarts "
        "from rowid 0) so the fixed decoder can overwrite them, then re-run "
        "scripts/backfill_disclosure.py for the affected source."
    )
    print(
        "    Run this again afterwards. A re-sync heals a row by upserting over it, "
        "so anything left is a message the reader now skips entirely -- a body with "
        "no text, no attachment and no reaction, whose stale row nothing rewrites. "
        "Those need a decision, not another sync."
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("TOPOS_DATABASE_PATH", os.path.expanduser("~/.topos/database.db")),
        help="database to audit (opened read-only)",
    )
    args = parser.parse_args()
    if not os.path.exists(args.db):
        print(f"no database at {args.db}", file=sys.stderr)
        return 2
    return audit(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
