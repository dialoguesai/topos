#!/usr/bin/env python3
"""Backfill Ollama signal jobs (goal_extraction, topics) from canonical rows.

Supports ChatGPT (ai_chat_messages), messenger (conversation_messages), and
journal (journal_entries) via ``load_canonical_records_for_signal``.

Example:
  cd topos
  export TOPOS_DATABASE_PATH=$HOME/.topos/database.db
  export TOPOS_OLLAMA_QUERY_MODEL=llama3.2:latest
  uv run python scripts/backfill_ollama_signal_jobs.py \\
    --source-id imessage \\
    --jobs goal_extraction \\
    --sample-per-conversation \\
    --max-messages 40 \\
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
MESSENGER_SOURCE_IDS = ("imessage", "signal", "demo_messenger_file")
JOURNAL_SOURCE_IDS = ("demo_journal_file", "grow_journal", "grow_data_file")


def _configure_runtime(db_path: Path) -> None:
    os.environ.setdefault("TOPOS_KEY", "backfill-ollama-signal")
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    os.environ.setdefault("TOPOS_DATABASE_MODE", "local")


def _counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for table, sql in [
        ("user_goals", "SELECT COUNT(*) FROM user_goals"),
        ("message_topics", "SELECT COUNT(*) FROM message_topics"),
        ("work_signal_facts", "SELECT COUNT(*) FROM signal_facts WHERE dimension='work'"),
    ]:
        try:
            out[table] = int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            out[table] = -1
    try:
        by_source = {
            str(sid): int(n)
            for sid, n in conn.execute(
                "SELECT source_id, COUNT(*) FROM user_goals GROUP BY source_id"
            ).fetchall()
        }
        out["user_goals_by_source"] = by_source
    except sqlite3.Error:
        out["user_goals_by_source"] = {}
    return out


def _is_authored_row(msg: Dict[str, Any]) -> bool:
    """Prefer owner-authored rows when sampling (mirrors GoalExtractionJob gate)."""
    if msg.get("is_from_self") in (1, True, "1", "true", "True"):
        return True
    sender = str(msg.get("sender_type") or "").lower()
    if sender in ("user", "human", "me", "self"):
        return True
    # Journal / personal-by-construction rows have no sender_type.
    if not sender and msg.get("content"):
        table = str(msg.get("_table") or "")
        if table in ("journal_entries", "profile_records") or msg.get("entry_id"):
            return True
    return False


def _load_messages(
    conn: sqlite3.Connection,
    *,
    source_ids: Sequence[str],
    max_messages: int,
    sample_per_conversation: bool,
    user_only: bool,
    min_content_chars: int,
) -> List[Dict[str, Any]]:
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY

    messages: List[Dict[str, Any]] = []
    seen_conversations: set[str] = set()
    per_source_budget = max(1, max_messages)

    for source_id in source_ids:
        source_def = REGISTRY.get(source_id)
        if source_def is None:
            # Runtime journal installs (grow_*) — synthesize a minimal def.
            from topos.sources.definitions import DataSourceDefinition

            group = "journal" if source_id in JOURNAL_SOURCE_IDS else (
                "conversations" if source_id in MESSENGER_SOURCE_IDS else "ai_messages"
            )
            source_def = DataSourceDefinition(
                source_id=source_id,
                display_name=source_id,
                source_type="file",
                schema_id=f"runtime.{source_id}.v1",
                parser_id=f"runtime.{source_id}.v1",
                canonical_group_id=group,
            )

        rows = load_canonical_records_for_signal(
            conn, source_def, limit=max(per_source_budget * 8, 200)
        )
        # Stamp table for role gate / authored filter when loader omitted it.
        group = getattr(source_def, "canonical_group_id", None) or ""
        table_for_group = {
            "conversations": "conversation_messages",
            "journal": "journal_entries",
            "ai_messages": "ai_chat_messages",
            "profile": "profile_records",
        }.get(str(group), "")

        for msg in rows:
            content = str(msg.get("content") or "").strip()
            if len(content) < min_content_chars:
                continue
            if table_for_group and not msg.get("_table"):
                msg = {**msg, "_table": table_for_group}
            if user_only and not _is_authored_row(msg):
                continue
            conversation_id = str(
                msg.get("conversation_id") or msg.get("entry_id") or msg.get("message_id") or ""
            )
            if sample_per_conversation:
                if conversation_id in seen_conversations:
                    continue
                seen_conversations.add(conversation_id)
            if not msg.get("message_id"):
                msg = {
                    **msg,
                    "message_id": msg.get("entry_id") or msg.get("record_id") or msg.get("id"),
                }
            if not msg.get("source_id"):
                msg = {**msg, "source_id": source_id}
            messages.append(msg)
            if len(messages) >= max_messages:
                return messages

    return messages


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
    """Run jobs on one sqlite connection.

    The SignalDerivationOrchestrator opens ``get_db_connection()`` separately;
    holding this script's connection at the same time self-locks WAL writers
    ("database is locked") and rolls back goal rows. Use the job +
    DerivedTablesManager path on a single conn instead.
    """
    from topos.enrichment.derived_tables import DerivedTablesManager
    from topos.enrichment.job_writer import write_signal_records
    from topos.enrichment.jobs import SIGNAL_JOB_REGISTRY
    from topos.storage.adapters.factory import AdapterFactory

    conn = sqlite3.connect(str(db_path), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=120000")
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
    records_created: Dict[str, int] = {}
    deferred_jobs: List[str] = []
    errors: List[Dict[str, str]] = []

    for job_name in jobs:
        job = SIGNAL_JOB_REGISTRY.get(job_name)
        if job is None:
            errors.append({"job": job_name, "error": "unknown_job"})
            continue
        try:
            rows = await job.enrich(messages)
            if rows and isinstance(rows[0], dict) and rows[0].get("_deferred"):
                deferred_jobs.append(job_name)
                continue
            written = write_signal_records(
                job_name,
                rows,
                adapters=bundle,
                tables_manager=tables_manager,
                conn=conn,
            )
            records_created[job_name] = int(written or 0)
        except Exception as exc:  # noqa: BLE001 — report and continue other jobs
            errors.append({"job": job_name, "error": str(exc)})

    after = _counts(conn)
    conn.close()
    status = "completed"
    if deferred_jobs and not records_created:
        status = "deferred"
    elif errors and not records_created:
        status = "failed"
    return {
        "status": status,
        "source_ids": list(source_ids),
        "primary_source_id": primary_source,
        "jobs": list(jobs),
        "messages_selected": len(messages),
        "sample_per_conversation": sample_per_conversation,
        "user_only": user_only,
        "records_created": records_created,
        "deferred_jobs": deferred_jobs,
        "errors": errors,
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
        help="Canonical source_id filter (repeatable). Default: ChatGPT sources",
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
        help="Disable authored-only filter (default: owner/human senders only)",
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
