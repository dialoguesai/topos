#!/usr/bin/env python3
"""
Run query quality eval: quality rubrics, latency budgets, permission boundaries.

Engine path (direct pipeline on local DB):
  TOPOS_DATABASE_PATH=$HOME/.topos/database.db python scripts/run_query_eval.py

Include MCP path (requires TOPOS_KEY + connected node):
  TOPOS_KEY=... python scripts/run_query_eval.py --mcp --mcp-url https://cp.logu3s.com
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gap" / "qq" / "engine"))

# Do not observe this eval's traffic as if it were a person's. The engine path below
# runs QueryPipeline in THIS process against the operator's own ~/.topos database, and
# scope shadow arms itself from ~/.topos/scope_shadow.on — a file this script inherits
# without anyone deciding it should. Left alone, every eval case would append synthetic
# text to the same ~/.topos/scope_shadow.jsonl that real turns land in, polluting the
# one corpus of real traffic the classifier work exists to collect. Set before the
# engine imports so nothing can read the flag first.
# Absent-or-blank is "no opinion" here, exactly as `scope_shadow.enabled()` reads it, so
# an explicit TOPOS_SCOPE_SHADOW=1 still opts this run in. Governs the in-process engine
# path only: under --mcp the observing happens in the node's process, under its own flag.
if not os.environ.get("TOPOS_SCOPE_SHADOW", "").strip():
    os.environ["TOPOS_SCOPE_SHADOW"] = "0"

from query_eval_cases import (  # noqa: E402
    LIVE_DB_PATH,
    PERMISSION_CASES,
    PRIVACY_CASES,
    QUALITY_CASES,
    EvalRunResult,
    manifest_for_scope,
)
from topos.query.manifest_validation import ManifestValidationError, resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory


def _print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    cols = ["case_id", "path", "quality", "latency_ms", "budget_ms", "outcome", "reason"]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


async def run_engine_eval(db_path: Path) -> List[EvalRunResult]:
    adapters = AdapterFactory.create("local_database", db_path=db_path)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    results: List[EvalRunResult] = []

    for case in [*QUALITY_CASES, *PRIVACY_CASES]:
        manifest = manifest_for_scope(case.scope_id)
        t0 = time.perf_counter()
        out = await orch.execute(
            query_text=case.query,
            scope_id=case.scope_id,
            access_mode=case.access_mode,  # type: ignore[arg-type]
            manifest=manifest,
            query_session_id=f"eval-{case.id}-{uuid.uuid4().hex[:8]}",
        )
        elapsed = (time.perf_counter() - t0) * 1000
        quality_ok, reason = case.evaluate(out)
        results.append(
            EvalRunResult(
                case_id=case.id,
                path="engine",
                quality_pass=quality_ok,
                quality_reason=reason,
                latency_ms=round(elapsed, 1),
                latency_pass=elapsed <= case.max_latency_ms,
                turn_outcome=str(out.get("turn_outcome") or ""),
                denied=out.get("turn_outcome") == "denied",
                optional_seed=case.optional_seed,
            )
        )

    for case in PERMISSION_CASES:
        if case.use_legacy_scope:
            t0 = time.perf_counter()
            try:
                resolve_scope_manifest(case.scope_id)
                quality_ok, reason = False, "expected legacy rejection"
            except ManifestValidationError as exc:
                quality_ok = any(s in str(exc.message).lower() for s in case.deny_substrings)
                reason = str(exc.message)
            elapsed = (time.perf_counter() - t0) * 1000
            results.append(
                EvalRunResult(
                    case_id=case.id,
                    path="engine",
                    quality_pass=quality_ok,
                    quality_reason=reason,
                    latency_ms=round(elapsed, 1),
                    latency_pass=elapsed <= case.max_latency_ms,
                    turn_outcome="denied" if quality_ok else "unexpected",
                    denied=True,
                )
            )
            continue

        manifest = manifest_for_scope(case.scope_id)
        t0 = time.perf_counter()
        out = await orch.execute(
            query_text=case.query,
            scope_id=case.scope_id,
            access_mode=case.access_mode,  # type: ignore[arg-type]
            manifest=manifest,
            query_session_id=f"eval-{case.id}-{uuid.uuid4().hex[:8]}",
        )
        elapsed = (time.perf_counter() - t0) * 1000
        denied = out.get("turn_outcome") == "denied"
        if case.expect_denied:
            deny_text = str(out.get("deny_reason") or "").lower()
            quality_ok = denied and (
                not case.deny_substrings or any(s in deny_text for s in case.deny_substrings)
            )
            reason = deny_text or str(out.get("turn_outcome"))
        else:
            quality_ok = not denied
            reason = "granted" if quality_ok else str(out.get("deny_reason"))
        results.append(
            EvalRunResult(
                case_id=case.id,
                path="engine",
                quality_pass=quality_ok,
                quality_reason=reason,
                latency_ms=round(elapsed, 1),
                latency_pass=elapsed <= case.max_latency_ms,
                turn_outcome=str(out.get("turn_outcome") or ""),
                denied=denied,
            )
        )

    return results


async def run_mcp_eval(mcp_url: str, topos_key: str) -> List[EvalRunResult]:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        # Raise (not SystemExit) so release runners can capture mcp_error without aborting.
        raise RuntimeError(f"MCP SDK required for MCP lane: {exc}") from exc

    results: List[EvalRunResult] = []
    base = mcp_url.rstrip("/")
    url = f"{base}/mcp"

    async with streamablehttp_client(
        url,
        headers={
            "Authorization": f"Bearer {topos_key}",
            "X-Topos-Client": "topos-home-chat/1",
        },
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for case in QUALITY_CASES:
                t0 = time.perf_counter()
                raw = await session.call_tool(
                    "query_scope",
                    {
                        "scope_id": case.scope_id,
                        "access_mode": case.access_mode,
                        "intent": case.query,
                    },
                )
                elapsed = (time.perf_counter() - t0) * 1000
                payload: Dict[str, Any] = {}
                if raw.structuredContent:
                    payload = dict(raw.structuredContent)
                elif raw.content:
                    text = next((c.text for c in raw.content if getattr(c, "text", None)), "")
                    if text:
                        payload = json.loads(text)
                quality_ok, reason = case.evaluate(payload)
                results.append(
                    EvalRunResult(
                        case_id=case.id,
                        path="mcp",
                        quality_pass=quality_ok,
                        quality_reason=reason,
                        latency_ms=round(elapsed, 1),
                        latency_pass=elapsed <= case.max_latency_ms,
                        turn_outcome=str(payload.get("turn_outcome") or ""),
                        denied=payload.get("turn_outcome") == "denied",
                        optional_seed=case.optional_seed,
                    )
                )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Topos query quality eval runner")
    parser.add_argument("--db", default=str(LIVE_DB_PATH), help="SQLite database path")
    parser.add_argument("--mcp", action="store_true", help="Also run MCP path eval")
    parser.add_argument("--mcp-url", default=os.environ.get("MCP_BASE_URL", "https://cp.logu3s.com"))
    parser.add_argument("--seed", action="store_true", help="Apply minimal Q5/Q6 seed rows before eval")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    if args.seed:
        import sqlite3

        sys.path.insert(0, str(ROOT / "tests"))
        from fixtures.query_eval_seed.apply_seed import apply_query_eval_seed

        conn = sqlite3.connect(str(db_path))
        try:
            apply_query_eval_seed(conn)
        finally:
            conn.close()
        print(f"Applied query eval seed to {db_path}")

    all_results: List[EvalRunResult] = []
    all_results.extend(asyncio.run(run_engine_eval(db_path)))

    if args.mcp:
        key = os.environ.get("TOPOS_KEY") or os.environ.get("MCP_AUTH_TOKEN")
        if not key:
            print("Set TOPOS_KEY for --mcp", file=sys.stderr)
            return 2
        all_results.extend(asyncio.run(run_mcp_eval(args.mcp_url, key)))

    if args.json:
        print(json.dumps([asdict(r) for r in all_results], indent=2))
    else:
        rows = []
        case_budget = {c.id: c.max_latency_ms for c in [*QUALITY_CASES, *PRIVACY_CASES]}
        for pb in PERMISSION_CASES:
            case_budget[pb.id] = pb.max_latency_ms
        for r in all_results:
            rows.append(
                {
                    "case_id": r.case_id,
                    "path": r.path,
                    "quality": "PASS" if r.quality_pass else "FAIL",
                    "latency_ms": f"{r.latency_ms:.0f}",
                    "budget_ms": str(case_budget.get(r.case_id, "?")),
                    "outcome": r.turn_outcome,
                    "reason": r.quality_reason[:60],
                }
            )
        _print_table(rows)
        quality_pass = sum(1 for r in all_results if r.quality_pass)
        latency_pass = sum(1 for r in all_results if r.latency_pass)
        print(f"\nQuality: {quality_pass}/{len(all_results)}  Latency: {latency_pass}/{len(all_results)}")

    failed = [r for r in all_results if not r.pass_all]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
