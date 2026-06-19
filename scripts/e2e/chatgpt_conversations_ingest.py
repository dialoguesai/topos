#!/usr/bin/env python3
"""E2E: ingest ChatGPT conversations.json → canonical → embeddings + graph.

Uses source ``chatgpt_file_ingestion`` (schema ``chatgpt.conversation.v2``).

Example:
  python scripts/e2e/chatgpt_conversations_ingest.py \\
    --input ../../conversations.json \\
    --max-conversations 3 \\
    --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE_ID = "chatgpt_file_ingestion"
SCHEMA_ID = "chatgpt.conversation.v2"


def _slice_conversations(input_path: Path, max_conversations: Optional[int]) -> tuple[Path, int]:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"Expected JSON array in {input_path}")
    total = len(raw)
    if max_conversations is not None and max_conversations > 0:
        raw = raw[:max_conversations]
    if len(raw) == total:
        return input_path, total
    tmp = Path(tempfile.mkdtemp(prefix="chatgpt_e2e_")) / "conversations_slice.json"
    tmp.write_text(json.dumps(raw), encoding="utf-8")
    return tmp, len(raw)


def _configure_runtime(*, db_path: Path, ingest_root: Path) -> None:
    os.environ.setdefault("TOPOS_KEY", "e2e-chatgpt-ingest")
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    os.environ["TOPOS_DATABASE_MODE"] = "local"
    os.environ["TOPOS_INGESTION_BASE_PATH"] = str(ingest_root)
    os.environ.setdefault("CONTROL_PLANE_URL", "")
    os.environ.setdefault("TOPOS_CONTROL_PLANE_URL", "")


def _write_stub_embeddings(conn: sqlite3.Connection, messages: List[Dict[str, Any]]) -> int:
    """Deterministic stub vectors when HF/torch is unavailable (E2E dev environments)."""
    import hashlib

    from topos.enrichment.job_writer import write_signal_records
    from topos.storage.adapters.factory import AdapterFactory

    bundle = AdapterFactory.create("local_database", conn=conn)
    records: List[Dict[str, Any]] = []
    for msg in messages:
        content = str(msg.get("content") or "").strip()
        message_id = msg.get("message_id")
        if not message_id or not content:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).digest()
        vector = [((byte / 255.0) * 2.0 - 1.0) for byte in digest[:16]]
        records.append(
            {
                "message_id": message_id,
                "record_id": message_id,
                "source_id": msg.get("source_id") or SOURCE_ID,
                "vector": vector,
                "dims": len(vector),
                "model": "e2e-stub-v1",
                "provider": "stub",
                "signal_dimension": "memory",
                "text_preview": content[:200],
            }
        )
    if not records:
        return 0
    return write_signal_records(
        "embeddings",
        records,
        adapters=bundle,
        provenance={"provider": "stub", "model": "e2e-stub-v1"},
        conn=conn,
    )


async def _ensure_signal_outputs(
    conn: sqlite3.Connection,
    *,
    dataset_id: str,
    embedding_fallback: str,
) -> Dict[str, Any]:
    from topos.enrichment.orchestrator import SignalDerivationOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    bundle = AdapterFactory.create("local_database", conn=conn)
    messages = _load_canonical_messages(conn)
    meta = {"embedding_source": "none", "signal_derivation_rerun": False}

    needs_derive = bundle.vector.list_metadata(limit=1).total == 0 and not bundle.graph.list_graph(limit_edges=1)["edges"]
    if messages and needs_derive:
        meta["signal_derivation_rerun"] = True
        orch = SignalDerivationOrchestrator(adapters=bundle)
        await orch.run_signal_derivation(
            messages,
            source_id=SOURCE_ID,
            sync_batch_id=f"e2e-{dataset_id}",
        )

    if bundle.vector.list_metadata(limit=1).total == 0 and embedding_fallback == "stub" and messages:
        written = _write_stub_embeddings(conn, messages)
        if written > 0:
            meta["embedding_source"] = "stub"
    elif bundle.vector.list_metadata(limit=1).total > 0:
        meta["embedding_source"] = "huggingface"

    return meta


def _load_canonical_messages(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT message_id, conversation_id, sender_type, content, source_id
        FROM ai_chat_messages
        ORDER BY message_id
        LIMIT 5000
        """
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "message_id": row[0],
                "conversation_id": row[1],
                "thread_id": row[1],
                "sender_type": row[2],
                "content": row[3],
                "source_id": row[4] or SOURCE_ID,
            }
        )
    return out


def _verify(conn: sqlite3.Connection, *, dataset_id: str, require_embeddings: bool) -> Dict[str, Any]:
    from topos.storage.adapters.factory import AdapterFactory

    bundle = AdapterFactory.create("local_database", conn=conn)

    msg_count = conn.execute("SELECT COUNT(*) FROM ai_chat_messages").fetchone()[0]
    conv_count = conn.execute("SELECT COUNT(*) FROM ai_chat_conversations").fetchone()[0]

    vector_page = bundle.vector.list_metadata(source_id=SOURCE_ID, limit=5)
    if vector_page.total == 0:
        vector_page = bundle.vector.list_metadata(limit=5)

    graph = bundle.graph.list_graph(limit_nodes=20, limit_edges=20)
    signal_facts = conn.execute("SELECT COUNT(*) FROM signal_facts").fetchone()[0]

    audit_rows = conn.execute(
        "SELECT stage, status, records_in, records_out FROM ingest_audit ORDER BY audit_id"
    ).fetchall()

    report = {
        "dataset_id": dataset_id,
        "source_id": SOURCE_ID,
        "schema_id": SCHEMA_ID,
        "ai_chat_messages": int(msg_count),
        "ai_chat_conversations": int(conv_count),
        "embedding_metadata_total": int(vector_page.total),
        "embedding_sample": vector_page.items[:2],
        "graph_nodes": len(graph.get("nodes") or []),
        "graph_edges": len(graph.get("edges") or []),
        "graph_sample_edges": (graph.get("edges") or [])[:3],
        "signal_facts": int(signal_facts),
        "ingest_audit_stages": [
            {"stage": r[0], "status": r[1], "records_in": r[2], "records_out": r[3]} for r in audit_rows
        ],
    }

    failures: List[str] = []
    if msg_count <= 0:
        failures.append("no rows in ai_chat_messages")
    if require_embeddings and vector_page.total <= 0:
        failures.append("no embedding metadata in signal_embeddings (HF model may be unavailable)")
    if report["graph_edges"] <= 0 and report["graph_nodes"] <= 0:
        failures.append("no graph nodes or edges after signal derivation")

    report["ok"] = not failures
    report["failures"] = failures
    return report


async def run_e2e(
    *,
    input_path: Path,
    max_conversations: Optional[int],
    dataset_id: str,
    db_path: Optional[Path],
    require_embeddings: bool,
    embedding_fallback: str,
) -> Dict[str, Any]:
    ingest_path, conv_count = _slice_conversations(input_path, max_conversations)

    with tempfile.TemporaryDirectory(prefix="chatgpt_e2e_work_") as work_dir:
        work = Path(work_dir)
        db_file = db_path or (work / "e2e_chatgpt.db")
        ingest_root = work / "ingestion"
        ingest_root.mkdir(parents=True, exist_ok=True)
        _configure_runtime(db_path=db_file, ingest_root=ingest_root)

        # Fresh imports after env is set.
        for mod in (
            "topos.config.settings",
            "topos.core.state",
            "topos.ingestion.manager",
            "topos.ingestion.ingest_helpers",
            "topos.storage.adapters.factory",
        ):
            sys.modules.pop(mod, None)

        from topos.ingestion.ingest_helpers import ingest_file_payload

        ingest_result = await ingest_file_payload(
            dataset_id=dataset_id,
            schema_id=SCHEMA_ID,
            file_path=str(ingest_path),
            file_format="json",
            source_id=SOURCE_ID,
        )

        from topos.core.state import get_db_connection

        conn = get_db_connection()
        if conn is None:
            raise RuntimeError("database connection unavailable after ingest")

        signal_meta = await _ensure_signal_outputs(
            conn,
            dataset_id=dataset_id,
            embedding_fallback=embedding_fallback,
        )

        verify = _verify(conn, dataset_id=dataset_id, require_embeddings=require_embeddings)
        verify["embedding_source"] = signal_meta.get("embedding_source")
        return {
            "ingest": ingest_result,
            "conversations_in_file": conv_count,
            "signal": signal_meta,
            "verify": verify,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E ChatGPT conversations.json ingest + signal verify")
    parser.add_argument(
        "--input",
        type=Path,
        default=ROOT.parent / "conversations.json",
        help="Path to ChatGPT conversations.json export",
    )
    parser.add_argument(
        "--max-conversations",
        type=int,
        default=3,
        help="Limit conversations ingested (default 3; use 0 for entire file)",
    )
    parser.add_argument("--dataset-id", default="e2e-user:chatgpt-conversations")
    parser.add_argument("--db-path", type=Path, default=None, help="SQLite path (temp file if omitted)")
    parser.add_argument(
        "--embedding-fallback",
        choices=("stub", "none"),
        default="stub",
        help="When HF/torch unavailable, write deterministic stub embeddings (default stub)",
    )
    parser.add_argument(
        "--skip-embedding-check",
        action="store_true",
        help="Do not fail when no embedding metadata is present",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 2

    max_conv = None if args.max_conversations == 0 else args.max_conversations
    report = asyncio.run(
        run_e2e(
            input_path=args.input.resolve(),
            max_conversations=max_conv,
            dataset_id=args.dataset_id,
            db_path=args.db_path,
            require_embeddings=not args.skip_embedding_check,
            embedding_fallback=args.embedding_fallback,
        )
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        v = report["verify"]
        print(f"Source: {SOURCE_ID} ({SCHEMA_ID})")
        print(f"Conversations processed: {report['conversations_in_file']}")
        print(f"Records ingested: {report['ingest'].get('records_processed')}")
        print(f"ai_chat_messages: {v['ai_chat_messages']}")
        print(f"Embeddings (metadata): {v['embedding_metadata_total']} ({v.get('embedding_source', 'unknown')})")
        print(f"Graph nodes/edges: {v['graph_nodes']}/{v['graph_edges']}")
        if v["failures"]:
            print("FAIL:", "; ".join(v["failures"]))
        else:
            print("OK — canonical, embeddings, and graph checks passed")

    return 0 if report["verify"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
