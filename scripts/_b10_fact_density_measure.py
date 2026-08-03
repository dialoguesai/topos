#!/usr/bin/env python3
"""B10 BEFORE/AFTER measure: fact density + fact_llm progress + FD lane status.

Examples:
  cd topos
  uv run python scripts/_b10_fact_density_measure.py \\
    --out ../topos-ops-wiki/90_EXPERIMENTS/_b10_fact_density_before.json
  uv run python scripts/_b10_fact_density_measure.py --run-fd-lane \\
    --out ../topos-ops-wiki/90_EXPERIMENTS/_b10_fact_density_after.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fact_density(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        out["active_facts"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM signal_objects "
                "WHERE object_type='fact' AND valid_to IS NULL"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        out["active_facts"] = -1
    try:
        by_ab = {
            str(ab or ""): int(n)
            for ab, n in conn.execute(
                """
                SELECT json_extract(payload_json, '$.asserted_by'), COUNT(*)
                FROM signal_objects
                WHERE object_type='fact' AND valid_to IS NULL
                GROUP BY 1
                """
            ).fetchall()
        }
        out["active_facts_by_asserted_by"] = by_ab
    except sqlite3.Error:
        out["active_facts_by_asserted_by"] = {}
    try:
        out["fact_llm_progress"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM extraction_artifacts "
                "WHERE artifact_type='fact_llm_pass'"
            ).fetchone()[0]
        )
        out["fact_llm_progress_with_facts"] = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM extraction_artifacts
                WHERE artifact_type='fact_llm_pass'
                  AND CAST(json_extract(payload_json, '$.facts_written') AS INT) > 0
                """
            ).fetchone()[0]
        )
    except sqlite3.Error:
        out["fact_llm_progress"] = -1
        out["fact_llm_progress_with_facts"] = -1
    return out


def _corpus(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for label, sql in [
        (
            "imessage_authored_long",
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE source_id='imessage' AND is_from_self=1 "
            "AND length(trim(coalesce(content,''))) >= 40",
        ),
        (
            "ai_chat_authored_long",
            "SELECT COUNT(*) FROM ai_chat_messages "
            "WHERE lower(coalesce(sender_type,'')) IN ('user','human') "
            "AND length(trim(coalesce(content,''))) >= 40",
        ),
        (
            "journal_long",
            "SELECT COUNT(*) FROM journal_entries "
            "WHERE length(trim(coalesce(content,''))) >= 40",
        ),
    ]:
        try:
            out[label] = int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            out[label] = -1
    return out


def _concurrency_config() -> Dict[str, Any]:
    from topos.features.facts.llm_extract import (
        FACTS_LLM_CONCURRENCY,
        facts_llm_enabled,
    )
    from topos.config.settings import settings

    return {
        "FACTS_LLM_CONCURRENCY": FACTS_LLM_CONCURRENCY,
        "TOPOS_FACTS_LLM_CONCURRENCY_env": os.environ.get("TOPOS_FACTS_LLM_CONCURRENCY"),
        "facts_llm_enabled": bool(facts_llm_enabled()),
        "ollama_extraction_model": getattr(settings, "ollama_extraction_model", ""),
        "facts_llm_model": getattr(settings, "facts_llm_model", ""),
    }


def _run_fd_lane() -> Dict[str, Any]:
    """Run the hermetic FD lane (stub LLM; no network)."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/gap/qq/engine/test_fact_density_corpus.py",
        "-p",
        "no:warnings",
        "-q",
        "--tb=line",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - t0
    return {
        "exit_code": proc.returncode,
        "elapsed_s": round(elapsed, 3),
        "stdout_tail": (proc.stdout or "")[-800:],
        "stderr_tail": (proc.stderr or "")[-400:],
        "pass": proc.returncode == 0,
    }


def _synthetic_throughput() -> Dict[str, Any]:
    """Wall-clock serial vs concurrent with a sleep stub (no Ollama)."""
    import sqlite3 as _sqlite3
    import tempfile
    from pathlib import Path as _Path

    from topos.features.facts.llm_extract import extract_owner_facts_llm
    from topos.storage.db.migrations import apply_all_migrations

    def _rows(n: int) -> List[Dict[str, Any]]:
        return [
            {
                "message_id": f"t{i}",
                "conversation_id": "bench:1",
                "sender_type": "human",
                "content": f"I have been practicing yoga habit number {i} for years",
                "event_at": "2026-06-01T10:00:00+00:00",
                "_table": "ai_chat_messages",
            }
            for i in range(n)
        ]

    def _stub(prompt, row):
        time.sleep(0.05)
        mid = str(row.get("message_id"))
        return [{"predicate": "practices", "object": f"yoga-{mid}"}]

    def _timed(concurrency: int, n: int = 8) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory() as td:
            db = _sqlite3.connect(str(_Path(td) / "bench.db"))
            db.row_factory = _sqlite3.Row
            apply_all_migrations(db)
            db.execute(
                "INSERT INTO entities (entity_id, entity_type, canonical_name, "
                "normalized_name, is_self) VALUES "
                "('ent-owner', 'person', 'Owner', 'owner', 1)"
            )
            db.commit()
            t0 = time.perf_counter()
            written = extract_owner_facts_llm(
                db, _rows(n), extractor=_stub, concurrency=concurrency
            )
            elapsed = time.perf_counter() - t0
            db.close()
        return {
            "concurrency": concurrency,
            "rows": n,
            "facts_written": written,
            "elapsed_s": round(elapsed, 3),
            "sec_per_row": round(elapsed / max(1, n), 3),
        }

    serial = _timed(1)
    concurrent = _timed(4)
    speedup = (
        round(serial["elapsed_s"] / concurrent["elapsed_s"], 2)
        if concurrent["elapsed_s"] > 0
        else None
    )
    return {
        "serial": serial,
        "concurrent": concurrent,
        "speedup_x": speedup,
        "note": "sleep-stub wall clock; proves fan-out, not live Ollama latency",
    }


def measure(
    db_path: Path,
    *,
    run_fd_lane: bool,
    run_synthetic_throughput: bool,
) -> Dict[str, Any]:
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    os.environ.setdefault("TOPOS_DATABASE_MODE", "local")
    os.environ.setdefault("TOPOS_KEY", "b10-measure")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    payload: Dict[str, Any] = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "density": _fact_density(conn),
        "corpus": _corpus(conn),
        "config": _concurrency_config(),
    }
    conn.close()

    if run_fd_lane:
        payload["fd_lane"] = _run_fd_lane()
    if run_synthetic_throughput:
        payload["synthetic_throughput"] = _synthetic_throughput()
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="B10 fact-density measure")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(
            os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db")
        ),
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--run-fd-lane", action="store_true")
    parser.add_argument("--run-synthetic-throughput", action="store_true")
    args = parser.parse_args(argv)

    report = measure(
        args.db_path,
        run_fd_lane=args.run_fd_lane,
        run_synthetic_throughput=args.run_synthetic_throughput,
    )
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
