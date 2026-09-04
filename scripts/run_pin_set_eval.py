#!/usr/bin/env python3
"""Pin-set eval for the transcript isolation gate.

Runs G1/G2/SIM1/SIM2/Q2/Q3/Q4/D3/F1/F2 on a snapshot and records per-case
pass, stores_touched, evidence source_id mix, and graph edge types.

    TOPOS_DATABASE_PATH=/tmp/topos-tx-eval/control.db \\
      uv run python scripts/run_pin_set_eval.py --out /tmp/topos-tx-eval/arm-a.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gap" / "qq" / "engine"))

if not os.environ.get("TOPOS_SCOPE_SHADOW", "").strip():
    os.environ["TOPOS_SCOPE_SHADOW"] = "0"

from query_eval_cases import QUALITY_CASES, _public_result, _stores, manifest_for_scope
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory

PIN_IDS = ("G1", "G2", "SIM1", "SIM2", "Q2", "Q3", "Q4", "D3", "F1", "F2")


def _items(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    pr = _public_result(response)
    raw = (
        pr.get("summaries")
        or pr.get("summary_items")
        or pr.get("items")
        or pr.get("scores")
        or []
    )
    return [i for i in raw if isinstance(i, dict)]


def _source_mix(items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for item in items:
        sid = str(item.get("source_id") or "").strip()
        if sid:
            counts[sid] += 1
    return dict(sorted(counts.items()))


def _graph_edge_types(items: Iterable[Dict[str, Any]]) -> List[str]:
    types = {
        str(i.get("edge_type") or "")
        for i in items
        if str(i.get("retrieval_source") or "").startswith("graph:") and i.get("edge_type")
    }
    return sorted(t for t in types if t)


async def _run(db_path: Path) -> List[Dict[str, Any]]:
    adapters = AdapterFactory.create("local_database", db_path=db_path)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    by_id = {c.id: c for c in QUALITY_CASES}
    rows: List[Dict[str, Any]] = []
    for case_id in PIN_IDS:
        case = by_id[case_id]
        manifest = manifest_for_scope(case.scope_id)
        t0 = time.perf_counter()
        out = await orch.execute(
            query_text=case.query,
            scope_id=case.scope_id,
            access_mode=case.access_mode,  # type: ignore[arg-type]
            manifest=manifest,
            query_session_id=f"pin-{case.id}-{uuid.uuid4().hex[:8]}",
        )
        elapsed = (time.perf_counter() - t0) * 1000
        quality_ok, reason = case.evaluate(out)
        items = _items(out)
        rows.append(
            {
                "case_id": case.id,
                "scope_id": case.scope_id,
                "quality_pass": quality_ok,
                "quality_reason": reason,
                "latency_ms": round(elapsed, 1),
                "turn_outcome": str(out.get("turn_outcome") or ""),
                "stores_touched": _stores(out),
                "source_id_mix": _source_mix(items),
                "graph_edge_types": _graph_edge_types(items),
                "item_count": len(items),
                "retrieval_sources": sorted(
                    {str(i.get("retrieval_source") or "") for i in items if i.get("retrieval_source")}
                ),
            }
        )
        print(
            f"{case.id:5} {'PASS' if quality_ok else 'FAIL':4} "
            f"{elapsed:8.1f}ms  {reason}",
            flush=True,
        )
    return rows


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    env_db = (os.environ.get("TOPOS_DATABASE_PATH") or "").strip()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(env_db).expanduser() if env_db else None,
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.db:
        raise SystemExit("pass --db or TOPOS_DATABASE_PATH")

    import asyncio

    rows = asyncio.run(_run(args.db))
    payload = {
        "db": str(args.db),
        "pin_ids": list(PIN_IDS),
        "passed": sum(1 for r in rows if r["quality_pass"]),
        "total": len(rows),
        "cases": rows,
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"wrote {args.out}", flush=True)
    else:
        print(text)
    return 0 if payload["passed"] == payload["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
