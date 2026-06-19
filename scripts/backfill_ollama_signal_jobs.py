#!/usr/bin/env python3
"""Backfill Ollama signal jobs (goal_extraction, topics) from existing canonical messages.

Example:
  cd topos
  export TOPOS_DATABASE_PATH=$HOME/.topos/database.db
  export TOPOS_OLLAMA_QUERY_MODEL=llama3.2:latest
  uv run python scripts/backfill_ollama_signal_jobs.py \\
    --source-id chatgpt_ingestion \\
    --jobs goal_extraction,topics \\
    --sample-per-conversation \\
    --max-messages 200 \\
    --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SOURCE_IDS = ("chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation")
DEFAULT_JOBS = ("goal_extraction", "topics")


def _configure_runtime(db_path: Path) -> None:
    os.environ.setdefault("TOPOS_KEY", "backfill-ollama-signal")
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    os.environ.setdefault("TOPOS_DATABASE_MODE", "local")


def _load_messages(
    conn: sqlite3.Connection,
    *,
    source_ids: Sequence[str],
    max_messages: int,
    sample_per_conversation: bool,
    user_only: bool,
    min_content_chars: int,
) -> List[Dict[str, Any]]:
    placeholders = ",".join("?" for _ in source_ids)
    query = f"""
        SELECT message_id, conversation_id, sender_type, content, source_id, event_at
        FROM ai_chat_messages
        WHERE source_id IN ({placeholders})
          AND length(trim(coalesce(content, ''))) >= ?
        ORDER BY event_at DESC
    """
    params: List[Any] = [*source_ids, min_content_chars]
    if not sample_per_conversation:
        query += " LIMIT ?"
        params.append(max_messages)

    rows = conn.execute(query, params).fetchall()
    messages: List[Dict[str, Any]] = []
    seen_conversations: set[str] = set()

    for row in rows:
        conversation_id = str(row[1] or "")
        sender_type = str(row[2] or "").lower()
        if user_only and sender_type not in ("user", "human"):
            continue
        if sample_per_conversation:
            if conversation_id in seen_conversations:
                continue
            seen_conversations.add(conversation_id)
        messages.append(
            {
                "message_id": row[0],
                "conversation_id": conversation_id,
                "sender_type": row[2],
                "content": row[3],
                "source_id": row[4],
                "event_at": row[5],
            }
        )
        if len(messages) >= max_messages:
            break

    return messages


def _counts(conn: sqlite3.Connection) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for table, sql in [
        ("user_goals", "SELECT COUNT(*) FROM user_goals"),
        ("message_topics", "SELECT COUNT(*) FROM message_topics"),
        ("work_signal_facts", "SELECT COUNT(*) FROM signal_facts WHERE dimension='work'"),
    ]:
        try:
            out[table] = int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            out[table] = -1
    return out


async def _run_backfill(
    *,
    db_path: Path,
    source_ids: Sequence[str],
    jobs: Sequence[str],
    max_messages: int,
    sample_per_conversation: bool,
    user_only: bool,
    min_content_chars: int,
) -> Dict[str, Any]:
    from topos.enrichment.derived_tables import DerivedTablesManager
    from topos.enrichment.orchestrator import SignalDerivationOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    conn = sqlite3.connect(str(db_path))
    before = _counts(conn)
    messages = _load_messages(
        conn,
        source_ids=source_ids,
        max_messages=max_messages,
        sample_per_conversation=sample_per_conversation,
        user_only=user_only,
        min_content_chars=min_content_chars,
    )
    if not messages:
        conn.close()
        return {
            "status": "skipped",
            "reason": "no_messages",
            "source_ids": list(source_ids),
            "before": before,
            "after": before,
        }

    primary_source = str(messages[0].get("source_id") or source_ids[0])
    bundle = AdapterFactory.create("local_database", conn=conn)
    tables_manager = DerivedTablesManager(conn)
    orch = SignalDerivationOrchestrator(adapters=bundle, tables_manager=tables_manager)

    result = await orch.run_signal_derivation(
        messages,
        source_id=primary_source,
        job_names=list(jobs),
        sync_batch_id=f"backfill-ollama-{primary_source}",
    )

    after = _counts(conn)
    conn.close()
    return {
        "status": "completed",
        "source_ids": list(source_ids),
        "primary_source_id": primary_source,
        "jobs": list(jobs),
        "messages_selected": len(messages),
        "sample_per_conversation": sample_per_conversation,
        "user_only": user_only,
        "records_created": result.get("records_created") or {},
        "deferred_jobs": result.get("deferred_jobs") or [],
        "errors": result.get("errors") or [],
        "before": before,
        "after": after,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Ollama goal/topic signal jobs")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db")),
    )
    parser.add_argument(
        "--source-id",
        action="append",
        dest="source_ids",
        help="Canonical source_id filter (repeatable). Default: all ChatGPT sources",
    )
    parser.add_argument(
        "--jobs",
        default=",".join(DEFAULT_JOBS),
        help="Comma-separated jobs: goal_extraction, topics",
    )
    parser.add_argument("--max-messages", type=int, default=200, help="Cap messages processed per run")
    parser.add_argument(
        "--sample-per-conversation",
        action="store_true",
        help="Take at most one message per conversation (recommended for goals)",
    )
    parser.add_argument(
        "--all-messages",
        action="store_true",
        help="Disable user-only filter (default: user/human senders only)",
    )
    parser.add_argument("--min-content-chars", type=int, default=40, help="Skip very short messages")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    args = parser.parse_args()

    source_ids = tuple(args.source_ids or DEFAULT_SOURCE_IDS)
    jobs = tuple(j.strip() for j in args.jobs.split(",") if j.strip())
    _configure_runtime(args.db_path)

    report = asyncio.run(
        _run_backfill(
            db_path=args.db_path,
            source_ids=source_ids,
            jobs=jobs,
            max_messages=max(1, args.max_messages),
            sample_per_conversation=args.sample_per_conversation,
            user_only=not args.all_messages,
            min_content_chars=max(1, args.min_content_chars),
        )
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"status: {report.get('status')}")
        print(f"messages: {report.get('messages_selected')}")
        print(f"records_created: {report.get('records_created')}")
        print(f"deferred: {report.get('deferred_jobs')}")
        print(f"before: {report.get('before')}")
        print(f"after: {report.get('after')}")
        if report.get("errors"):
            print(f"errors: {report.get('errors')}")

    if report.get("deferred_jobs") or report.get("errors"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
