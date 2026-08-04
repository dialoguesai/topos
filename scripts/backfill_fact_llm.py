#!/usr/bin/env python3
"""Bulk / sampled LLM fact extraction with bounded concurrency (B10 / P4.3).

Calls ``extract_owner_facts_llm`` directly (role gate + pre-filter + resume
markers + semaphore fan-out). Does **not** reopen C1 / retrieval knobs —
extraction throughput only.

Examples:
  cd topos
  export TOPOS_DATABASE_PATH=$HOME/.topos/database.db
  # Sampled imessage pass at concurrency=4 (default):
  uv run python scripts/backfill_fact_llm.py \\
    --source-id imessage \\
    --sample-per-conversation \\
    --max-messages 8 \\
    --min-content-chars 60 \\
    --concurrency 4 \\
    --json
  # Serial baseline for BEFORE/AFTER wall-clock:
  uv run python scripts/backfill_fact_llm.py \\
    --source-id imessage --max-messages 4 --concurrency 1 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SOURCE_IDS = ("imessage",)
MESSENGER_SOURCE_IDS = ("imessage", "signal", "demo_messenger_file")
JOURNAL_SOURCE_IDS = ("demo_journal_file", "grow_journal", "grow_data_file")
AI_CHAT_SOURCE_IDS = (
    "chatgpt_ingestion",
    "chatgpt_file_ingestion",
    "chatgpt_ui_conversation",
)


def _configure_runtime(db_path: Path) -> None:
    os.environ.setdefault("TOPOS_KEY", "backfill-fact-llm")
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    os.environ.setdefault("TOPOS_DATABASE_MODE", "local")
    # Ensure the additive LLM pass is ON for this ops script.
    os.environ.setdefault("TOPOS_FACTS_LLM", "1")


def _fact_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    try:
        active = int(
            conn.execute(
                "SELECT COUNT(*) FROM signal_objects "
                "WHERE object_type='fact' AND valid_to IS NULL"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        active = -1
    try:
        progress = int(
            conn.execute(
                "SELECT COUNT(*) FROM extraction_artifacts "
                "WHERE artifact_type='fact_llm_pass'"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        progress = -1
    try:
        progress_with_facts = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM extraction_artifacts
                WHERE artifact_type='fact_llm_pass'
                  AND CAST(json_extract(payload_json, '$.facts_written') AS INT) > 0
                """
            ).fetchone()[0]
        )
    except sqlite3.Error:
        progress_with_facts = -1
    return {
        "active_facts": active,
        "fact_llm_progress": progress,
        "fact_llm_progress_with_facts": progress_with_facts,
    }


def _looks_extractable_content(content: str) -> bool:
    """Skip iMessage attributed-body dumps / binary-ish payloads.

    Those rows survive length filters but burn Ollama for zero facts.
    """
    text = str(content or "")
    if not text.strip():
        return False
    if any(tok in text for tok in ("classname", "NSObject", "NSValue", "NSDictionary")):
        return False
    letters = sum(ch.isalpha() for ch in text)
    return (letters / max(1, len(text))) >= 0.45 and (" " in text)


def _is_authored_row(msg: Dict[str, Any]) -> bool:
    """Owner-authored only — mirrors provenance ROLE_AUTHORED for sampling.

    Messenger stamps ``sender_type='human'`` for *other people* too; only
    ``is_from_self`` distinguishes the owner on conversation_messages. AI-chat
    uses sender_type user/human for the owner. Journal/profile are owned by
    construction.
    """
    table = str(msg.get("_table") or "")
    if msg.get("is_from_self") in (1, True, "1", "true", "True"):
        return True
    if msg.get("is_from_self") in (0, False, "0", "false", "False"):
        # Explicitly not-from-self (messenger contact / inbound SMS).
        return False
    sender = str(msg.get("sender_type") or "").lower()
    if table in ("ai_chat_messages",) or not table:
        if sender in ("user", "human", "me", "self"):
            # ai_chat: human/user = owner. conversation_messages without
            # is_from_self fail closed below.
            if table == "ai_chat_messages":
                return True
            if "is_from_self" not in msg and "sender_id" not in msg:
                return True  # ambiguous-but-ai-chat-shaped (extract.py contract)
    if not sender and msg.get("content"):
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
    skip_processed: bool,
) -> List[Dict[str, Any]]:
    from topos.features.facts.llm_extract import _already_processed_record_ids
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY

    messages: List[Dict[str, Any]] = []
    seen_conversations: set[str] = set()
    per_source_budget = max(1, max_messages)

    for source_id in source_ids:
        source_def = REGISTRY.get(source_id)
        if source_def is None:
            from topos.sources.definitions import DataSourceDefinition

            if source_id in JOURNAL_SOURCE_IDS:
                group = "journal"
            elif source_id in MESSENGER_SOURCE_IDS:
                group = "conversations"
            elif source_id in AI_CHAT_SOURCE_IDS:
                group = "ai_messages"
            else:
                group = "conversations"
            source_def = DataSourceDefinition(
                source_id=source_id,
                display_name=source_id,
                source_type="file",
                schema_id=f"runtime.{source_id}.v1",
                parser_id=f"runtime.{source_id}.v1",
                canonical_group_id=group,
            )

        # Pull a wide window: recent messenger traffic is inbound-heavy, and
        # skip_processed further thins the set. Need enough authored long rows
        # to fill max_messages after filters.
        rows = load_canonical_records_for_signal(
            conn, source_def, limit=max(per_source_budget * 40, 2000)
        )
        group = getattr(source_def, "canonical_group_id", None) or ""
        table_for_group = {
            "conversations": "conversation_messages",
            "journal": "journal_entries",
            "ai_messages": "ai_chat_messages",
            "profile": "profile_records",
        }.get(str(group), "")

        batch: List[Dict[str, Any]] = []
        for msg in rows:
            content = str(msg.get("content") or "").strip()
            if len(content) < min_content_chars:
                continue
            if not _looks_extractable_content(content):
                continue
            if table_for_group and not msg.get("_table"):
                msg = {**msg, "_table": table_for_group}
            if user_only and not _is_authored_row(msg):
                continue
            conversation_id = str(
                msg.get("conversation_id")
                or msg.get("entry_id")
                or msg.get("message_id")
                or ""
            )
            if sample_per_conversation:
                if conversation_id in seen_conversations:
                    continue
                seen_conversations.add(conversation_id)
            if not msg.get("message_id"):
                msg = {
                    **msg,
                    "message_id": msg.get("entry_id")
                    or msg.get("record_id")
                    or msg.get("id"),
                }
            if not msg.get("source_id"):
                msg = {**msg, "source_id": source_id}
            batch.append(msg)

        if skip_processed and batch:
            eligible_probe = [
                {
                    "record_id": m.get("message_id") or m.get("id"),
                    "table": str(m.get("_table") or table_for_group or ""),
                    "row": m,
                }
                for m in batch
            ]
            already = _already_processed_record_ids(conn, eligible_probe)
            if already:
                batch = [
                    m
                    for m in batch
                    if str(m.get("message_id") or m.get("id") or "") not in already
                ]

        for msg in batch:
            messages.append(msg)
            if len(messages) >= max_messages:
                return messages

    return messages


def run_backfill(
    *,
    db_path: Path,
    source_ids: Sequence[str],
    max_messages: int,
    sample_per_conversation: bool,
    user_only: bool,
    min_content_chars: int,
    concurrency: int,
    skip_processed: bool,
    dry_run: bool,
) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db_path), timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    before = _fact_counts(conn)
    messages = _load_messages(
        conn,
        source_ids=source_ids,
        max_messages=max_messages,
        sample_per_conversation=sample_per_conversation,
        user_only=user_only,
        min_content_chars=min_content_chars,
        skip_processed=skip_processed,
    )
    if not messages:
        conn.close()
        return {
            "status": "skipped",
            "reason": "no_messages",
            "source_ids": list(source_ids),
            "concurrency": concurrency,
            "messages_selected": 0,
            "before": before,
            "after": before,
            "facts_written": 0,
            "elapsed_s": 0.0,
            "sec_per_row": None,
        }

    if dry_run:
        after = before
        conn.close()
        return {
            "status": "dry_run",
            "source_ids": list(source_ids),
            "concurrency": concurrency,
            "messages_selected": len(messages),
            "sample_ids": [
                str(m.get("message_id") or m.get("id")) for m in messages[:12]
            ],
            "before": before,
            "after": after,
            "facts_written": 0,
            "elapsed_s": 0.0,
            "sec_per_row": None,
        }

    from topos.features.facts.llm_extract import extract_owner_facts_llm

    t0 = time.perf_counter()
    written = extract_owner_facts_llm(
        conn,
        messages,
        concurrency=concurrency,
        # Loader already applied skip_processed; when include-processed was
        # requested, force re-extract (ignore fact_llm_pass markers).
        resume=skip_processed,
    )
    elapsed = time.perf_counter() - t0
    after = _fact_counts(conn)
    conn.close()
    n = max(1, len(messages))
    return {
        "status": "completed",
        "source_ids": list(source_ids),
        "concurrency": concurrency,
        "messages_selected": len(messages),
        "sample_per_conversation": sample_per_conversation,
        "user_only": user_only,
        "skip_processed": skip_processed,
        "facts_written": int(written or 0),
        "elapsed_s": round(elapsed, 3),
        "sec_per_row": round(elapsed / n, 3),
        "before": before,
        "after": after,
        "active_facts_delta": int(after["active_facts"]) - int(before["active_facts"]),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill role-gated LLM fact extraction (concurrent)"
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(
            os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db")
        ),
    )
    parser.add_argument(
        "--source-id",
        action="append",
        dest="source_ids",
        help="Canonical source_id filter (repeatable). Default: imessage",
    )
    parser.add_argument("--max-messages", type=int, default=40)
    parser.add_argument(
        "--sample-per-conversation",
        action="store_true",
        help="At most one message per conversation",
    )
    parser.add_argument(
        "--all-messages",
        action="store_true",
        help="Disable authored-only filter (default: owner/human only)",
    )
    parser.add_argument("--min-content-chars", type=int, default=40)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("TOPOS_FACTS_LLM_CONCURRENCY", "4")),
        help="Fan-out bound (default TOPOS_FACTS_LLM_CONCURRENCY or 4)",
    )
    parser.add_argument(
        "--include-processed",
        action="store_true",
        help="Do not skip rows already marked fact_llm_pass (re-pays Ollama)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Select only; no LLM")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    source_ids = tuple(args.source_ids or DEFAULT_SOURCE_IDS)
    _configure_runtime(args.db_path)
    # Keep module-level default in sync for anything that still reads the env.
    os.environ["TOPOS_FACTS_LLM_CONCURRENCY"] = str(max(1, args.concurrency))

    report = run_backfill(
        db_path=args.db_path,
        source_ids=source_ids,
        max_messages=max(1, args.max_messages),
        sample_per_conversation=args.sample_per_conversation,
        user_only=not args.all_messages,
        min_content_chars=max(1, args.min_content_chars),
        concurrency=max(1, args.concurrency),
        skip_processed=not args.include_processed,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"status: {report.get('status')}")
        print(f"messages: {report.get('messages_selected')}")
        print(f"concurrency: {report.get('concurrency')}")
        print(f"facts_written: {report.get('facts_written')}")
        print(f"elapsed_s: {report.get('elapsed_s')}")
        print(f"sec_per_row: {report.get('sec_per_row')}")
        print(f"before: {report.get('before')}")
        print(f"after: {report.get('after')}")

    if report.get("status") in ("failed",):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
