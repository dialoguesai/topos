#!/usr/bin/env python3
"""Convert legacy JSON vector blobs to float32 in signal_embeddings."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from topos.features.signal.vector_codec import decode_vector, encode_f32
from topos.storage.db.migrations import ensure_migrations_applied


def migrate(db_path: Path, *, dry_run: bool = False, batch_size: int = 500) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    ensure_migrations_applied(conn)
    rows = conn.execute(
        """
        SELECT embedding_id, vector_blob, vector_format
        FROM signal_embeddings
        WHERE vector_blob IS NOT NULL
          AND COALESCE(vector_format, 'json') = 'json'
        """
    ).fetchall()
    converted = skipped = errors = 0
    pending = 0
    for embedding_id, blob, vector_format in rows:
        try:
            vector = decode_vector(blob, vector_format or "json")
            encoded = encode_f32(vector)
        except Exception:
            errors += 1
            continue
        if dry_run:
            converted += 1
            continue
        conn.execute(
            """
            UPDATE signal_embeddings
            SET vector_blob=?, vector_format='f32', dims=?
            WHERE embedding_id=?
            """,
            (encoded, len(vector), embedding_id),
        )
        converted += 1
        pending += 1
        if pending >= batch_size:
            conn.commit()
            pending = 0
    if not dry_run:
        conn.commit()
    conn.close()
    return {"converted": converted, "skipped": skipped, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate signal_embeddings JSON blobs to f32")
    parser.add_argument("--db-path", required=True, help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Count rows without writing")
    args = parser.parse_args()
    stats = migrate(Path(args.db_path), dry_run=args.dry_run)
    print(stats)
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
