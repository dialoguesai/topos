"""One-shot manual QA for the HF model playground (real download + inference).

Usage: .venv/bin/python scripts/qa_enrichment_lab_playground.py

Uses an in-memory SQLite DB (no node data touched) and drives the REAL lab
worker path: resolve -> task guard -> snapshot download -> job.enrich().
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QA_MODEL = "distilbert-base-uncased-finetuned-sst-2-english"


def main() -> int:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    import topos.core.state as state_mod
    import topos.enrichment_lab.service as service_mod
    from topos.enrichment_lab import store as lab_store
    from topos.enrichment_lab import worker as lab_worker

    state_mod.get_db_connection = lambda: conn  # type: ignore[assignment]
    service_mod.get_db_connection = lambda: conn  # type: ignore[assignment]

    # 1. Task guard: a NER model pasted under sentiment must fail fast.
    gid_bad = lab_store.insert_group(
        conn,
        job_id="sentiment",
        dataset_kind="bundle",
        models=["hf:dslim/bert-base-NER"],
        record_inputs={"r1": {"body": "I love this"}},
        bundle_id="enrich.messages.personal",
        bundle_version="v1",
    )
    import asyncio

    asyncio.run(lab_worker._process_group(gid_bad))
    bad_runs = [dict(r) for r in lab_store.list_runs(conn, gid_bad)]
    print("[guard] status:", bad_runs[0]["status"], "|", bad_runs[0]["error_code"])
    assert bad_runs[0]["status"] == "failed"
    assert str(bad_runs[0]["error_code"]).startswith("task_mismatch")

    # 2. Real playground run: compare default vs pasted model on a bundle.
    gid = service_mod.create_job_group(
        job_id="sentiment",
        models=[f"hf:{QA_MODEL}"],
        dataset_kind="bundle",
        bundle_id="enrich.messages.personal",
    )
    detail = service_mod.serialize_job_group(conn, gid)
    print("[run] group status:", detail["group"]["status"])
    ok = 0
    for run in detail["runs"]:
        status = run["status"]
        out = run.get("output") or []
        first = out[0] if out else {}
        label = first.get("label") or first.get("sentiment") or first.get("emotion_label")
        print(
            f"  {run['model_tag'][:44]:44s} {run['record_id']:>4s} {status:9s} "
            f"{run.get('latency_ms')}ms label={label} err={run.get('error_code')}"
        )
        if status == "succeeded":
            ok += 1
    print(f"[run] succeeded {ok}/{len(detail['runs'])}")
    assert detail["group"]["status"] in ("completed", "completed_with_errors")
    assert ok == len(detail["runs"]), "all runs should succeed"

    # 3. Node data untouched: only lab tables exist.
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "message_sentiment" not in tables, tables
    print("[dry-run] no enrichment output tables created:", sorted(tables))
    print("QA PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
