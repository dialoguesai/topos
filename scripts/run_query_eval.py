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
            # engine_only cases grade owner_raw-only surfaces (the graph lane):
            # a third-party-classed MCP harness is CORRECTLY silent there, and
            # running them here would grade the privacy invariant as a defect.
            for case in [c for c in QUALITY_CASES if not c.engine_only]:
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


async def run_aggregate_eval(db_path: Path, *, necessity: bool = True) -> List[Dict[str, Any]]:
    """SUITE-P: the aggregate verb vs today's inference lane, same corpus.

    The verb leg drives real dispatch; the necessity leg sends each case's
    natural phrasing through the retrieval+inference stack and grades it with
    the most charitable rubric (any exact expected number anywhere in the
    response). The old lane's failure rate is the verb's justification — if it
    passes at scale, S7's kill-switch says the verb was unnecessary.
    """
    import topos.core.handlers as hub
    from topos.core.handlers import handle_control_plane_request
    from topos.principal import OWNER_APP, Principal

    from query_eval_cases import (
        AGGREGATE_CASES,
        evaluate_aggregate_result,
        necessity_answer_contains,
    )

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    hub.get_db_connection = lambda: conn  # script-scoped; this process only evals
    owner = Principal(cls=OWNER_APP, channel="cp_relay")

    adapters = AdapterFactory.create("local_database", db_path=db_path)
    orch = QueryPipelineOrchestrator(adapters=adapters)

    rows: List[Dict[str, Any]] = []
    for case in AGGREGATE_CASES:
        t0 = time.perf_counter()
        resp = await handle_control_plane_request(
            {
                "id": f"suitep-{case.id}",
                "type": "aggregate",
                "payload": {**case.payload, "dataset_id": "u1:default"},
                "caller": {"mcp_source": "topos_home_chat"},
            },
            principal=owner,
        )
        verb_ms = (time.perf_counter() - t0) * 1000
        payload = (resp or {}).get("payload") or {}
        ok, reason = (
            evaluate_aggregate_result(case, payload.get("public_result") or {})
            if payload.get("turn_outcome") == "live_query"
            else (False, str(payload.get("deny_reason") or "no live_query"))
        )

        old_ok, old_reason, old_ms = None, "necessity leg skipped", None
        if necessity:
            manifest = manifest_for_scope(case.necessity_scope)
            t1 = time.perf_counter()
            try:
                out = await orch.execute(
                    query_text=case.necessity_query,
                    scope_id=case.necessity_scope,
                    access_mode="inference",
                    manifest=manifest,
                    query_session_id=f"suitep-old-{case.id}-{uuid.uuid4().hex[:8]}",
                )
                old_ok, old_reason = necessity_answer_contains(case, out)
            except Exception as exc:  # noqa: BLE001
                old_ok, old_reason = False, f"old lane errored: {exc}"
            old_ms = (time.perf_counter() - t1) * 1000

        rows.append(
            {
                "case_id": case.id,
                "verb_pass": ok,
                "verb_reason": reason,
                "verb_ms": round(verb_ms, 1),
                "verb_budget_ok": verb_ms <= case.max_latency_ms,
                "old_lane_pass": old_ok,
                "old_lane_reason": old_reason,
                "old_lane_ms": round(old_ms, 1) if old_ms is not None else None,
                "description": case.description,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Topos query quality eval runner")
    parser.add_argument("--db", default=str(LIVE_DB_PATH), help="SQLite database path")
    parser.add_argument("--mcp", action="store_true", help="Also run MCP path eval")
    parser.add_argument("--mcp-url", default=os.environ.get("MCP_BASE_URL", "https://cp.logu3s.com"))
    parser.add_argument("--seed", action="store_true", help="Apply minimal Q5/Q6 seed rows before eval")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="SUITE-P: seed a THROWAWAY corpus and run the aggregate verb vs "
        "the old inference lane (the necessity leg; needs the local model)",
    )
    parser.add_argument(
        "--no-necessity", action="store_true", help="Skip the old-lane leg of --aggregate"
    )
    args = parser.parse_args()

    if args.aggregate:
        import sqlite3
        import tempfile

        sys.path.insert(0, str(ROOT / "tests"))
        from fixtures.query_eval_seed.apply_aggregate_seed import apply_aggregate_seed
        from topos.storage.db.migrations import apply_all_migrations

        tmp = Path(tempfile.mkdtemp(prefix="suitep-")) / "suitep.db"
        conn = sqlite3.connect(str(tmp))
        apply_all_migrations(conn)
        apply_aggregate_seed(conn)
        conn.close()
        print(f"SUITE-P corpus seeded at {tmp}")
        rows = asyncio.run(run_aggregate_eval(tmp, necessity=not args.no_necessity))
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                verb = "PASS" if r["verb_pass"] else "FAIL"
                old = (
                    "-" if r["old_lane_pass"] is None
                    else "PASS" if r["old_lane_pass"] else "FAIL"
                )
                old_ms = f"{r['old_lane_ms']:.0f}ms" if r["old_lane_ms"] is not None else "-"
                print(
                    f"{r['case_id']:>5}  verb={verb} {r['verb_ms']:>7.0f}ms   "
                    f"old_lane={old} {old_ms:>9}   {r['description'][:52]}"
                )
                if not r["verb_pass"]:
                    print(f"       verb: {r['verb_reason'][:100]}")
                if r["old_lane_pass"] is False:
                    print(f"       old:  {r['old_lane_reason'][:100]}")
            verb_rate = sum(1 for r in rows if r["verb_pass"])
            measured = [r for r in rows if r["old_lane_pass"] is not None]
            old_rate = sum(1 for r in measured if r["old_lane_pass"])
            print(f"\nSUITE-P: verb {verb_rate}/{len(rows)} exact")
            if measured:
                print(
                    f"necessity leg: old lane {old_rate}/{len(measured)} "
                    f"(the gap is the verb's justification; old==verb => kill-switch)"
                )
        return 0 if all(r["verb_pass"] for r in rows) else 1

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
