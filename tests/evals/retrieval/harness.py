"""Retrieval eval harness: builds a persona DB, embeds, scores search quality.

Usage (explicit, loads the local embedding model):
    pytest -m retrieval_eval tests/evals/retrieval -q

Produces a scorecard dict; the test writes it to scorecard.json and compares
against baseline.json when present.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations

from . import persona

EMBED_BATCH = 32
TOP_K = 10


def build_persona_db(db_path: str, embed_fn: Callable[[List[str]], List[List[float]]], *, model_name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    apply_all_migrations(conn)
    bundle = AdapterFactory.create("local_database", conn=conn)

    from topos.features.signal.embed_context import build_embed_text, embeddable_content

    records: List[Dict[str, Any]] = []
    texts: List[str] = []
    metas: List[Dict[str, Any]] = []
    for table, rows in persona.canonical_rows().items():
        dimension = persona.DIMENSION_BY_TABLE[table]
        for row in rows:
            row = {**row, "canonical_table": table}
            raw_text = embeddable_content(row)
            if not raw_text:
                continue
            # Embed with the production context header; store raw text for FTS.
            texts.append(build_embed_text(row, raw_text, record_type=table.rstrip("s")))
            metas.append(
                {
                    "record_id": row["record_id"],
                    "message_id": row["record_id"],
                    "source_id": persona.PERSONA_SOURCE_ID,
                    "signal_dimension": dimension,
                    "text_preview": raw_text[:200],
                    "search_text": raw_text[:2000],
                    "record_type": table.rstrip("s"),
                    "event_at": row.get("entry_at")
                    or row.get("ts")
                    or row.get("starts_at")
                    or row.get("occurred_at"),
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "content_hash": hashlib.sha256(raw_text.encode()).hexdigest(),
                    "embedding_id": f"emb_{uuid.uuid4().hex[:12]}",
                }
            )

    for start in range(0, len(texts), EMBED_BATCH):
        batch_texts = texts[start : start + EMBED_BATCH]
        vectors = embed_fn(batch_texts)
        for meta, vector in zip(metas[start : start + EMBED_BATCH], vectors):
            records.append(
                {
                    **meta,
                    "vector": vector,
                    "dims": len(vector),
                    "model": model_name,
                    "provider": "huggingface",
                    "normalized": True,
                }
            )

    written = write_signal_records(
        "embeddings",
        records,
        adapters=bundle,
        provenance={"job_id": "embeddings", "model": model_name},
        conn=conn,
    )
    assert written == len(records), f"embedding write shortfall: {written}/{len(records)}"
    conn.commit()
    return conn


def _dcg(relevances: List[int]) -> float:
    return sum(rel / math.log2(idx + 2) for idx, rel in enumerate(relevances))


def score_case(
    case: Dict[str, Any],
    items: List[Dict[str, Any]],
    *,
    top_k: int = TOP_K,
) -> Dict[str, Any]:
    expected = set(case.get("expected_any") or [])
    got_ids = [str(item.get("record_id") or "") for item in items[:top_k]]

    if case.get("class") == "negative":
        top1_sim = float(items[0].get("similarity") or 0.0) if items else 0.0
        ceiling = float(case.get("max_top1_similarity") or 0.55)
        passed = top1_sim <= ceiling
        return {
            "id": case["id"],
            "class": "negative",
            "passed": passed,
            "top1_similarity": round(top1_sim, 4),
            "recall": None,
            "mrr": None,
            "ndcg": None,
        }

    hits = [rid for rid in got_ids if rid in expected]
    recall = 1.0 if hits else 0.0
    mrr = 0.0
    for rank, rid in enumerate(got_ids, start=1):
        if rid in expected:
            mrr = 1.0 / rank
            break
    relevances = [1 if rid in expected else 0 for rid in got_ids]
    ideal = sorted(relevances, reverse=True)
    ndcg = _dcg(relevances) / _dcg(ideal) if any(ideal) else 0.0

    result = {
        "id": case["id"],
        "class": case["class"],
        "passed": recall > 0,
        "recall": recall,
        "mrr": round(mrr, 4),
        "ndcg": round(ndcg, 4),
        "top_ids": got_ids[:5],
    }
    min_tables = case.get("min_distinct_tables")
    if min_tables:
        hit_tables = {
            str(item.get("record_type") or "")
            for item in items[:top_k]
            if str(item.get("record_id") or "") in expected
        }
        result["distinct_tables"] = len(hit_tables)
        result["passed"] = result["passed"] and len(hit_tables) >= int(min_tables)
    return result


def run_eval(
    conn: sqlite3.Connection,
    search_fn: Callable[[str], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    case_results = []
    for case in persona.eval_cases():
        items = search_fn(case["query"])
        case_results.append(score_case(case, items))

    scored = [r for r in case_results if r["recall"] is not None]
    by_class: Dict[str, List[Dict[str, Any]]] = {}
    for r in case_results:
        by_class.setdefault(r["class"], []).append(r)

    def _mean(values: List[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    return {
        "cases": case_results,
        "summary": {
            "recall_at_10": _mean([r["recall"] for r in scored]),
            "mrr_at_10": _mean([r["mrr"] for r in scored]),
            "ndcg_at_10": _mean([r["ndcg"] for r in scored]),
            "negative_pass_rate": _mean(
                [1.0 if r["passed"] else 0.0 for r in by_class.get("negative", [])]
            ),
            "per_class_recall": {
                cls: _mean([r["recall"] for r in rs if r["recall"] is not None])
                for cls, rs in by_class.items()
                if cls != "negative"
            },
            "cases_passed": sum(1 for r in case_results if r["passed"]),
            "cases_total": len(case_results),
        },
    }


def compare_to_baseline(scorecard: Dict[str, Any], baseline_path: Path, *, tolerance: float = 0.05) -> List[str]:
    """Return list of regression messages (empty = OK)."""
    if not baseline_path.exists():
        return []
    baseline = json.loads(baseline_path.read_text())
    problems: List[str] = []
    for metric in ("recall_at_10", "mrr_at_10", "ndcg_at_10", "negative_pass_rate"):
        base = float(baseline.get("summary", {}).get(metric) or 0.0)
        now = float(scorecard["summary"].get(metric) or 0.0)
        if now < base - tolerance:
            problems.append(f"{metric} regressed: {base:.3f} -> {now:.3f}")
    return problems
