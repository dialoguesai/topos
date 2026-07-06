"""Enrichment evaluation harness (WS-E).

Measures whether enrichments make the node *evaluatively better* along the four
value axes from the enrichments plan:

- coverage: rows enriched vs canonical totals, per (source, job)
- signal density: enrichment outputs per 1k canonical records
- retrieval precision: eval queries answered by semantic narrowing vs a
  chronological baseline (hit-rate@k with vs without enrichment)
- latency: wall-clock of narrowed vs chronological reads

All measurements are read-only. Run via ``python scripts/enrichment_eval.py``
or call the functions with any SQLite connection (tests use fixtures).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence


def _table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _count(conn, sql: str, params: Sequence[Any] = ()) -> int:
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def coverage_report(source_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Per-source, per-job coverage using the platform coverage core."""
    from ..api.enrichment import _enrichment_coverage_core
    from ..sources.registry import REGISTRY

    ids = source_ids or sorted(REGISTRY.keys())
    out: Dict[str, Any] = {}
    for sid in ids:
        try:
            out[sid] = _enrichment_coverage_core(sid)
        except Exception as exc:  # noqa: BLE001
            out[sid] = {"status": "error", "error": str(exc)}
    return out


# ---------------------------------------------------------------------------
# Signal density
# ---------------------------------------------------------------------------

_CANONICAL_TABLES = (
    "ai_chat_messages",
    "conversation_messages",
    "activity_events",
    "journal_entries",
    "calendar_events",
)

_DENSITY_TABLES = (
    "signal_embeddings",
    "signal_facts",
    "signal_objects",
    "entities",
    "message_entities",
    "topic_clusters",
    "message_topics",
    "message_emotions",
    "user_goals",
    "timeline",
)


def signal_density(conn) -> Dict[str, Any]:
    """Enrichment outputs per 1k canonical records — the 'intelligence density' number."""
    canonical_total = sum(
        _count(conn, f"SELECT COUNT(*) FROM {t}") for t in _CANONICAL_TABLES if _table_exists(conn, t)
    )
    outputs: Dict[str, int] = {}
    for table in _DENSITY_TABLES:
        if _table_exists(conn, table):
            outputs[table] = _count(conn, f"SELECT COUNT(*) FROM {table}")
    total_outputs = sum(outputs.values())
    per_1k = round(total_outputs / canonical_total * 1000, 1) if canonical_total else None
    return {
        "canonical_records": canonical_total,
        "enrichment_outputs": outputs,
        "enrichment_outputs_total": total_outputs,
        "density_per_1k_records": per_1k,
    }


# ---------------------------------------------------------------------------
# Retrieval precision + latency (with vs without semantic narrowing)
# ---------------------------------------------------------------------------


@dataclass
class RetrievalEvalCase:
    """One eval query: text plus the record ids (or content keywords) that count as hits."""

    query: str
    expected_record_ids: List[str] = field(default_factory=list)
    expected_keywords: List[str] = field(default_factory=list)


def _hits(rows: List[Dict[str, Any]], case: RetrievalEvalCase) -> int:
    expected_ids = set(case.expected_record_ids)
    keywords = [k.lower() for k in case.expected_keywords]
    n = 0
    for row in rows:
        rid = str(row.get("message_id") or row.get("record_id") or "")
        content = str(row.get("content") or "").lower()
        if (expected_ids and rid in expected_ids) or (
            keywords and any(k in content for k in keywords)
        ):
            n += 1
    return n


def _chronological_read(conn, table: str, limit: int) -> List[Dict[str, Any]]:
    cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    order_col = "event_at" if "event_at" in cols else ("ts" if "ts" in cols else "rowid")
    rows = conn.execute(
        f"SELECT message_id, content, {order_col} AS event_at FROM {table} "
        f"ORDER BY {order_col} DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"message_id": r[0], "content": r[1], "event_at": r[2]} for r in rows]


def _narrowed_read(
    conn,
    table: str,
    limit: int,
    query: str,
    semantic_ids_fn: Callable[[str], Optional[set]],
) -> List[Dict[str, Any]]:
    ids = semantic_ids_fn(query)
    if not ids:
        return []
    id_list = sorted(ids)[:900]
    placeholders = ",".join("?" for _ in id_list)
    rows = conn.execute(
        f"SELECT message_id, content FROM {table} "
        f"WHERE message_id IN ({placeholders}) LIMIT ?",
        (*id_list, limit),
    ).fetchall()
    return [{"message_id": r[0], "content": r[1]} for r in rows]


def _default_semantic_ids(query: str) -> Optional[set]:
    try:
        from ..features.signal.service import get_signal_service

        result = get_signal_service().search_vectors(query=query, limit=200, mode="hybrid")
        return {
            str(item.get("record_id"))
            for item in (result.get("items") or [])
            if item.get("record_id")
        }
    except Exception:  # noqa: BLE001
        return None


def retrieval_precision_eval(
    conn,
    cases: List[RetrievalEvalCase],
    *,
    table: str = "ai_chat_messages",
    limit: int = 25,
    semantic_ids_fn: Optional[Callable[[str], Optional[set]]] = None,
) -> Dict[str, Any]:
    """Hit-rate@limit and latency for chronological vs enrichment-narrowed reads."""
    ids_fn = semantic_ids_fn or _default_semantic_ids
    per_case: List[Dict[str, Any]] = []
    for case in cases:
        t0 = time.monotonic()
        baseline_rows = _chronological_read(conn, table, limit)
        baseline_ms = int((time.monotonic() - t0) * 1000)
        t0 = time.monotonic()
        narrowed_rows = _narrowed_read(conn, table, limit, case.query, ids_fn)
        narrowed_ms = int((time.monotonic() - t0) * 1000)
        expected_n = max(len(case.expected_record_ids), 1)
        baseline_hits = _hits(baseline_rows, case)
        narrowed_hits = _hits(narrowed_rows, case)
        per_case.append(
            {
                "query": case.query,
                "baseline": {
                    "rows": len(baseline_rows),
                    "hits": baseline_hits,
                    "hit_rate": round(baseline_hits / expected_n, 3),
                    "latency_ms": baseline_ms,
                },
                "narrowed": {
                    "rows": len(narrowed_rows),
                    "hits": narrowed_hits,
                    "hit_rate": round(narrowed_hits / expected_n, 3),
                    "latency_ms": narrowed_ms,
                    "rows_scanned_reduction": (
                        round(1 - len(narrowed_rows) / len(baseline_rows), 3)
                        if baseline_rows
                        else None
                    ),
                },
            }
        )
    def _avg(key_path: str) -> Optional[float]:
        vals = []
        for c in per_case:
            side, key = key_path.split(".")
            v = c[side][key]
            if isinstance(v, (int, float)):
                vals.append(float(v))
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "cases": per_case,
        "summary": {
            "case_count": len(per_case),
            "baseline_avg_hit_rate": _avg("baseline.hit_rate"),
            "narrowed_avg_hit_rate": _avg("narrowed.hit_rate"),
            "baseline_avg_latency_ms": _avg("baseline.latency_ms"),
            "narrowed_avg_latency_ms": _avg("narrowed.latency_ms"),
        },
    }


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------


def run_enrichment_eval(
    conn,
    *,
    source_ids: Optional[List[str]] = None,
    retrieval_cases: Optional[List[RetrievalEvalCase]] = None,
    table: str = "ai_chat_messages",
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "signal_density": signal_density(conn),
        "coverage": coverage_report(source_ids),
    }
    if retrieval_cases and _table_exists(conn, table):
        report["retrieval"] = retrieval_precision_eval(conn, retrieval_cases, table=table)
    return report


def format_report_summary(report: Dict[str, Any]) -> str:
    lines: List[str] = ["Enrichment eval summary"]
    density = report.get("signal_density") or {}
    lines.append(
        f"  signal density: {density.get('density_per_1k_records')} outputs/1k records "
        f"({density.get('enrichment_outputs_total')} outputs over {density.get('canonical_records')} records)"
    )
    coverage = report.get("coverage") or {}
    for sid, cov in coverage.items():
        if not isinstance(cov, dict) or cov.get("status") != "ok":
            continue
        jobs = cov.get("jobs") or []
        covered = [j for j in jobs if j.get("coverage_percent") is not None]
        if covered:
            avg = round(sum(j["coverage_percent"] for j in covered) / len(covered), 1)
            lines.append(f"  coverage {sid}: avg {avg}% across {len(covered)} measurable jobs")
    retrieval = (report.get("retrieval") or {}).get("summary")
    if retrieval:
        lines.append(
            f"  retrieval: hit-rate {retrieval.get('baseline_avg_hit_rate')} -> "
            f"{retrieval.get('narrowed_avg_hit_rate')} with narrowing; latency "
            f"{retrieval.get('baseline_avg_latency_ms')}ms -> {retrieval.get('narrowed_avg_latency_ms')}ms"
        )
    return "\n".join(lines)


def report_to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
