"""QueryTarget via the in-process engine pipeline. REAL — Phase 1.

The engine-direct implementation: constructs a QueryPipelineOrchestrator over a DB and runs
requests through it, normalizing the result (including the DDR/evidence packet) into a
topos_eval.Response. This is the fast, in-process lane the composition cases already use —
here it's wrapped behind the QueryTarget protocol so topos-eval's metrics run on it.

The evidence packet is built from what the pipeline actually disclosed (public_result
items/rows/facts) plus the Disclosure Decision Record (enabled via TOPOS_QUERY_DDR=1 in
__init__) — the DDR carries what was assembled vs dropped and which stores were touched,
which is what faithfulness and disclosure grade against.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List

# topos_eval is the portable package; the engine imports are the coupling that keeps this
# file (not the package) on the engine side.
from topos_eval.protocols.target import EvidencePacket, Request, Response


def _items_from_public_result(pr: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ranked items: summary-mode items first, raw rows as the fallback — the same
    extraction order the existing rubric evals use (query_eval_cases._summary_items)."""
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    if items:
        return items
    rows = pr.get("rows") or pr.get("raw_rows") or pr.get("messages") or []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _answer_fields(pr: Dict[str, Any]) -> tuple[str | None, str | None, float | None]:
    """(answer_text, answer_type, confidence) from an inference-mode public_result."""
    answer = pr.get("answer")
    confidence = pr.get("confidence")
    if isinstance(answer, dict):
        confidence = confidence if confidence is not None else answer.get("confidence")
        answer_text = answer.get("text") or answer.get("answer") or str(answer)
    else:
        answer_text = answer if isinstance(answer, str) else None
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return answer_text, pr.get("answer_type"), confidence


def normalize_result(raw: Dict[str, Any], latency_ms: float | None = None) -> Response:
    """Map one pipeline result dict to a topos_eval.Response. Public seam so the runner
    can normalize responses it already collected without re-querying."""
    pr = raw.get("public_result") if isinstance(raw.get("public_result"), dict) else {}
    items = _items_from_public_result(pr)
    answer_text, answer_type, confidence = _answer_fields(pr)

    audit = raw.get("audit") if isinstance(raw.get("audit"), dict) else {}
    ddr = raw.get("disclosure_decision_record")
    evidence = EvidencePacket(
        summary_items=[i for i in items if i.get("retrieval_source")],
        rows=[r for r in (pr.get("rows") or pr.get("raw_rows") or []) if isinstance(r, dict)],
        facts=[f for f in (pr.get("facts") or []) if isinstance(f, dict)],
        stores_touched=list(audit.get("stores_touched") or []),
        ddr=ddr if isinstance(ddr, dict) else None,
    )

    return Response(
        turn_outcome=str(raw.get("turn_outcome") or "error"),
        items=items,
        answer=answer_text,
        answer_type=answer_type,
        confidence=confidence,
        deny_reason=raw.get("deny_reason"),
        evidence=evidence,
        latency_ms=latency_ms,
        raw=raw,
    )


class EngineTarget:
    """QueryTarget backed by the in-process pipeline over a SQLite DB."""

    def __init__(self, db_path: Path, *, warmup: bool = True) -> None:
        from topos.query.pipeline import QueryPipelineOrchestrator
        from topos.storage.adapters.factory import AdapterFactory

        self._db_path = Path(db_path)
        os.environ.setdefault("TOPOS_QUERY_DDR", "1")  # surface the DDR on results
        adapters = AdapterFactory.create("local_database", db_path=self._db_path)
        self._orchestrator = QueryPipelineOrchestrator(adapters=adapters)
        if warmup:
            self._warmup()

    def query(self, request: Request) -> Response:
        """Run one request through the pipeline and normalize. A denial is a
        Response(turn_outcome='denied'), never an exception."""
        from query_eval_cases import manifest_for_scope

        grant = request.grant or {}
        kwargs: Dict[str, Any] = dict(
            query_text=request.text,
            scope_id=request.scope,
            access_mode=request.access_mode,
            manifest=manifest_for_scope(request.scope),
            query_session_id=request.session_id or _session(request.scope),
        )
        if request.identity == "grantee":
            kwargs.update(
                is_grantee_request=True,
                requester_id=str(grant.get("requester_id") or "grantee"),
                filter_manifest=grant.get("filter_manifest"),
                field_transforms=grant.get("field_transforms"),
                disclosure_ceiling=grant.get("disclosure_ceiling"),
            )

        import time

        t0 = time.perf_counter()
        try:
            raw = self._run(self._orchestrator.execute(**kwargs))
        except Exception as exc:  # a crashing case is an error Response, honestly
            raw = {"turn_outcome": "error", "deny_reason": f"{type(exc).__name__}: {exc}"}
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return normalize_result(raw, latency_ms)

    @property
    def name(self) -> str:
        return f"engine-direct@{self._engine_sha()}"

    # -- internals ------------------------------------------------------------------

    @staticmethod
    def _run(coro):
        """Sync facade over the async pipeline: reuse the running loop if the caller is
        already inside one (pytest-asyncio), else asyncio.run."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def _warmup(self) -> None:
        """One throwaway multi-token, unique query so cold model-loading doesn't skew the
        first case's latency (same rationale as the runner's _warmup)."""
        self.query(
            Request(
                text=f"warmup pass over recent project notes {uuid.uuid4().hex[:8]}",
                scope="ai_conversations:read",
                access_mode="summary",
            )
        )

    @staticmethod
    def _engine_sha() -> str:
        try:
            root = Path(__file__).resolve().parents[5]
            return subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=root, capture_output=True, text=True, timeout=10,
            ).stdout.strip() or "unknown"
        except Exception:
            return "unknown"


def _session(case_id: str) -> str:
    return f"eval-{case_id}-{uuid.uuid4().hex[:8]}"
