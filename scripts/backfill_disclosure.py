#!/usr/bin/env python3
"""Backfill canonical disclosure columns and/or ingest-time NSFW tags."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_disclosure")

NSFW_BACKFILL_TABLES = (
    "ai_chat_messages",
    "conversation_messages",
    "journal_entries",
)


async def _run(
    source_id: str | None,
    batch_size: int,
    dry_run: bool,
    engine_url: str | None,
    *,
    nsfw_only: bool,
) -> int:
    from topos.core.state import get_db_connection
    from topos.disclosure.field_registry import PII_DISCLOSURE_FIELDS
    from topos.disclosure.privacy_layer import PrivacyLayerClient, run_privacy_disclosure_layer
    from topos.storage.db.migrations.canonical_disclosure_v1 import apply_canonical_disclosure_v1_up
    from topos.storage.db.migrations.canonical_nsfw_v1 import apply_canonical_nsfw_v1_up

    conn = get_db_connection()
    if conn is None:
        logger.error("No database connection")
        return 1

    apply_canonical_disclosure_v1_up(conn)
    apply_canonical_nsfw_v1_up(conn)

    client = PrivacyLayerClient(engine_url=engine_url) if engine_url else PrivacyLayerClient.from_settings()
    updated_total = 0
    nsfw_total = 0
    tables = NSFW_BACKFILL_TABLES if nsfw_only else list(PII_DISCLOSURE_FIELDS.keys())

    for table in tables:
        if not nsfw_only and table not in PII_DISCLOSURE_FIELDS:
            continue
        clauses: list[str] = []
        params: list[str] = []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if nsfw_only:
            clauses.append("content_nsfw_model IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM {table}{where}"
        try:
            rows = conn.execute(query, params).fetchall()
        except Exception as exc:
            logger.warning("skip table %s: %s", table, exc)
            continue
        cols = [d[0] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        messages = []
        for row in rows:
            rec = dict(zip(cols, row))
            rec["_table"] = table
            messages.append(rec)
        if not messages:
            logger.info("table=%s no rows to backfill", table)
            continue
        for i in range(0, len(messages), batch_size):
            batch = messages[i : i + batch_size]
            if dry_run:
                logger.info(
                    "dry-run table=%s batch=%d-%d would process %d rows (nsfw_only=%s)",
                    table,
                    i,
                    i + len(batch),
                    len(batch),
                    nsfw_only,
                )
                continue
            result = await run_privacy_disclosure_layer(
                conn,
                batch,
                client=client,
                nsfw_only=nsfw_only,
            )
            updated_total += int(result.get("records_updated") or 0)
            nsfw_total += int(result.get("nsfw_tagged") or 0)
            logger.info(
                "table=%s batch=%d-%d disclosure_updated=%s nsfw_tagged=%s",
                table,
                i,
                i + len(batch),
                result.get("records_updated"),
                result.get("nsfw_tagged"),
            )

    if nsfw_only:
        logger.info("NSFW backfill complete: %d rows tagged", nsfw_total)
    else:
        logger.info(
            "Backfill complete: %d disclosure rows updated, %d NSFW rows tagged",
            updated_total,
            nsfw_total,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill ingest-time PII disclosure columns and/or NSFW content tags",
    )
    parser.add_argument("--source-id", default=None, help="Limit to one source_id")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--dry-run", action="store_true", help="Report batches without writing")
    parser.add_argument("--engine-url", default=None, help="Remote Topos Engine URL for split deploy")
    parser.add_argument(
        "--nsfw-only",
        action="store_true",
        help="Only classify and tag rows missing content_nsfw_model (skip PII redaction)",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            args.source_id,
            args.batch_size,
            args.dry_run,
            args.engine_url,
            nsfw_only=args.nsfw_only,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
