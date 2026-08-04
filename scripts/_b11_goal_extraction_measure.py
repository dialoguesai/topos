#!/usr/bin/env python3
"""B11 BEFORE/AFTER measure: user_goals by source + optional D3M live probe.

Examples:
  cd topos
  uv run python scripts/_b11_goal_extraction_measure.py --out ../topos-ops-wiki/90_EXPERIMENTS/_b11_goal_extraction_before.json
  uv run python scripts/_b11_goal_extraction_measure.py --probe-d3m --out ../topos-ops-wiki/90_EXPERIMENTS/_b11_goal_extraction_after.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MESSENGER_SOURCES = ("imessage", "signal", "demo_messenger_file")
JOURNAL_SOURCES = ("grow_journal", "grow_data_file", "demo_journal_file")


def _goal_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    by_source = {
        str(sid or ""): int(n)
        for sid, n in conn.execute(
            "SELECT source_id, COUNT(*) FROM user_goals GROUP BY source_id"
        ).fetchall()
    }
    messenger = sum(by_source.get(s, 0) for s in MESSENGER_SOURCES)
    journal = sum(by_source.get(s, 0) for s in JOURNAL_SOURCES)
    return {
        "total": sum(by_source.values()),
        "by_source": by_source,
        "messenger_total": messenger,
        "journal_total": journal,
        "chatgpt_total": sum(
            n for sid, n in by_source.items() if sid.startswith("chatgpt")
        ),
    }


def _corpus(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for label, sql in [
        ("conversation_messages", "SELECT COUNT(*) FROM conversation_messages"),
        (
            "imessage_authored_long",
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE source_id='imessage' AND is_from_self=1 "
            "AND length(trim(coalesce(content,''))) >= 40",
        ),
        ("journal_entries", "SELECT COUNT(*) FROM journal_entries"),
        ("ai_chat_messages", "SELECT COUNT(*) FROM ai_chat_messages"),
    ]:
        try:
            out[label] = int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            out[label] = -1
    return out


def _probe_d3m(db_path: Path) -> Dict[str, Any]:
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    os.environ.setdefault("TOPOS_DATABASE_MODE", "local")
    os.environ.setdefault("TOPOS_KEY", "b11-measure")
    tests_root = ROOT / "tests"
    if str(tests_root) not in sys.path:
        sys.path.insert(0, str(tests_root))
    try:
        from gap.qq.engine.query_eval_cases import (  # type: ignore
            QUERY_CATALOG_VERSION,
            eval_d3m_messenger_goals,
        )
        from topos.query.manifest_validation import resolve_scope_manifest
        from topos.query.retrieval import DefaultSignalRetrievalAdapter
        from topos.query.types import RetrievalRequest
        from topos.storage.adapters.factory import AdapterFactory

        conn = sqlite3.connect(str(db_path))
        adapters = AdapterFactory.create("local_database", conn=conn)
        adapter = DefaultSignalRetrievalAdapter(adapters)
        manifest = resolve_scope_manifest("messages:read")
        bundle = adapter.retrieve(
            RetrievalRequest(
                manifest=manifest,
                access_mode="summary",
                query_text="What goals have I mentioned in my messages?",
                installed_source_ids=[],
            )
        )
        summaries = bundle.context_packet.get("summaries") or []
        live_response = {
            "status": "ok",
            "public_result": {"summaries": summaries},
        }
        ok, detail = eval_d3m_messenger_goals(live_response)
        messenger_goals = [
            s
            for s in summaries
            if s.get("retrieval_source") == "user_goal"
            and str(s.get("source_id") or "") in MESSENGER_SOURCES
        ]
        return {
            "pass": bool(ok),
            "detail": detail,
            "summary_count": len(summaries),
            "messenger_goal_count": len(messenger_goals),
            "messenger_goal_samples": [
                str(s.get("summary_text") or "")[:120] for s in messenger_goals[:5]
            ],
            "registry_has_user_goals": "user_goals" in (manifest.signal_objects or []),
            "catalog": QUERY_CATALOG_VERSION,
        }
    except Exception as exc:  # noqa: BLE001 — measure must not crash
        return {"pass": False, "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description="B11 goal_extraction measure")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db")),
    )
    parser.add_argument("--probe-d3m", action="store_true", help="Run messages:read D3M probe")
    parser.add_argument("--out", type=Path, help="Write JSON report")
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db_path))
    report: Dict[str, Any] = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(args.db_path),
        "goals": _goal_counts(conn),
        "corpus": _corpus(conn),
    }
    conn.close()
    if args.probe_d3m:
        report["d3m"] = _probe_d3m(args.db_path)

    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
