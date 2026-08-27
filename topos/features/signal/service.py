"""Signal read service shared by HTTP and WS handlers."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ...storage.adapters.factory import AdapterBundle
from .data_health import DataHealthComputer
from .dimension_registry import SIGNAL_DIMENSIONS
from .schemas import strip_vector_fields

if TYPE_CHECKING:  # import cost only, never at runtime
    from ..lifecycle.blackhole_guard import BlackholeGuard

# Every string a cluster row hands a caller. `label_terms` are lifted verbatim
# from member text, so a filter reading only the label would pass the name on.
_CLUSTER_TEXT_KEYS = ("label", "centroid_preview", "label_terms")


def _safe_json_list(raw: Optional[str]) -> List[Any]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


_DATA_HEALTH_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_DATA_HEALTH_TTL_SEC = 30.0


class SignalService:
    def __init__(self, adapters: AdapterBundle, *, conn: Any = None) -> None:
        self._adapters = adapters
        self._conn = conn

    def list_vectors(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        page = self._adapters.vector.list_metadata(
            source_id=source_id,
            dimension=dimension,
            model=model,
            limit=limit,
            offset=offset,
        )
        items = [strip_vector_fields(item) for item in page.items]
        if created_after or created_before:
            filtered = []
            for item in items:
                ts = item.get("created_at") or ""
                if created_after and ts < created_after:
                    continue
                if created_before and ts > created_before:
                    continue
                filtered.append(item)
            items = filtered
        total = len(items) if (created_after or created_before) else page.total
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def search_vectors(
        self,
        *,
        query: str,
        limit: int = 20,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        mode: str = "hybrid",
        event_after: Optional[str] = None,
        event_before: Optional[str] = None,
        hydrate: bool = False,
    ) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from ...engine.backends.huggingface import HuggingFaceAdapter, active_embedding_model
        from .hybrid_search import fts_search, merge_hybrid_results
        from .query_embed_cache import get_cached_query_embedding, set_cached_query_embedding
        from .source_hydration import hydrate_record_text
        from .vector_settings import vector_hybrid_enabled, min_similarity_threshold

        q = str(query or "").strip()
        limit = max(1, min(int(limit), 100))
        embed_model = model or active_embedding_model()
        search_mode = (mode or "hybrid").strip().lower()
        if not q:
            return {"items": [], "total": 0, "query": "", "model": embed_model, "limit": limit, "mode": search_mode}

        query_vector = get_cached_query_embedding(q, model=embed_model)
        if query_vector is None:
            hf = HuggingFaceAdapter()
            emb = hf.run_inference(
                {"text": q},
                {"subtype": "embedding", "model": embed_model, "input_role": "query"},
            )
            vectors = emb.get("vectors") or []
            if not vectors:
                return {
                    "items": [],
                    "total": 0,
                    "query": q,
                    "model": embed_model,
                    "limit": limit,
                    "mode": search_mode,
                    "error": "embedding_failed",
                }
            query_vector = [float(x) for x in vectors[0]]
            set_cached_query_embedding(q, model=embed_model, vector=query_vector)

        # Over-fetch so the collapse below can still fill a page. Chunking
        # splits one record across rows; identical text stored once per sighting
        # collapses too — 37% of the first live corpus was byte-identical. Both
        # shrink the result set after the fetch, never before it.
        fetch_limit = limit * 3
        page = self._adapters.vector.search_similar(
            query_vector,
            source_id=source_id,
            dimension=dimension,
            model=embed_model,
            limit=limit,
            event_after=event_after,
            event_before=event_before,
            fetch_limit=fetch_limit,
        )
        items = [strip_vector_fields(item) for item in page.items]

        use_hybrid = vector_hybrid_enabled() and search_mode == "hybrid"
        conn = self._conn if self._conn is not None else get_db_connection()
        if use_hybrid and conn is not None:
            fts_ids = fts_search(conn, q, limit=fetch_limit, source_id=source_id)
            items = merge_hybrid_results(conn, items, fts_ids, limit=fetch_limit)

        # Rank by RRF score when hybrid ran (comparable across contributors);
        # cosine threshold applies only to items that actually have a cosine.
        min_sim = min_similarity_threshold()
        if min_sim > 0:
            items = [
                item
                for item in items
                if item.get("similarity") is None or float(item.get("similarity") or 0.0) >= min_sim
            ]

        rank_key = "hybrid_score" if use_hybrid else "similarity"

        def _rank(row: Dict[str, Any]) -> float:
            value = row.get(rank_key)
            if value is None:
                value = row.get("similarity") or row.get("hybrid_score") or 0.0
            return float(value)

        items = sorted(items, key=_rank, reverse=True)

        # One result per thing said, not per time it was stored. The same text
        # is embedded once per sighting — a page title revisited six times is
        # six rows sharing one content_hash — so a single phrase could take the
        # whole page ("GitHub" filled four of five slots). Items arrive ranked,
        # so the first survivor of a group is its best-scoring member, and the
        # rest are counted rather than discarded silently.
        #
        # Runs BEFORE reranking on purpose: the cross-encoder scores only its
        # top `rerank_candidate_limit` candidates, and spending that budget on
        # six copies of one string buys nothing.
        deduped: List[Dict[str, Any]] = []
        seen_records: set[str] = set()
        kept_by_content: Dict[str, Dict[str, Any]] = {}
        for item in items:
            record_key = str(item.get("record_id") or item.get("embedding_id") or "")
            if record_key and record_key in seen_records:
                continue
            content_key = str(item.get("content_hash") or "")
            twin = kept_by_content.get(content_key) if content_key else None
            if twin is not None:
                twin["occurrences"] = int(twin.get("occurrences") or 1) + 1
                if record_key:
                    seen_records.add(record_key)
                continue
            if record_key:
                seen_records.add(record_key)
            item["occurrences"] = 1
            if content_key:
                kept_by_content[content_key] = item
            deduped.append(item)

        items = self._maybe_rerank(q, deduped)[:limit]

        if hydrate and conn is not None:
            for item in items:
                hydrated = hydrate_record_text(
                    conn,
                    str(item.get("record_id") or ""),
                    source_id=item.get("source_id"),
                    record_type=item.get("record_type"),
                )
                if hydrated.found:
                    item["source_text"] = hydrated.content

        from ...storage.adapters.sqlite.vector_search import last_search_backend

        return {
            "items": items,
            "total": page.total,
            "query": q,
            "model": embed_model,
            "limit": limit,
            "mode": search_mode,
            "backend": last_search_backend(),
            "rerank": getattr(self, "last_rerank_state", "skipped"),
        }

    def _maybe_rerank(self, query: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Cross-encoder rerank of the top fused candidates (mode: off|auto|on).

        Sets ``self.last_rerank_state`` so callers can report whether reranking
        actually ran (auto mode degrades silently otherwise).
        """
        from .vector_settings import rerank_candidate_limit, rerank_mode

        mode = rerank_mode()
        self.last_rerank_state = "off" if mode == "off" else "skipped"
        if mode == "off" or len(items) < 2:
            return items
        head = items[: rerank_candidate_limit()]
        tail = items[len(head):]
        candidates = []
        for idx, item in enumerate(head):
            text = str(item.get("search_text") or item.get("text_preview") or "")
            if text:
                candidates.append({"id": idx, "text": text[:512]})
        if len(candidates) < 2:
            return items
        try:
            from ...engine.backends.huggingface import HuggingFaceAdapter

            result = HuggingFaceAdapter().run_inference(
                {"query": query, "candidates": candidates}, {"subtype": "rerank"}
            )
        except Exception:
            return items
        if result.get("status") == "unavailable" or not result.get("items"):
            return items
        order = [item["id"] for item in result["items"] if item.get("rerank_score") is not None]
        if not order:
            return items
        self.last_rerank_state = "reranked"
        score_by_idx = {
            item["id"]: item["rerank_score"] for item in result["items"]
        }
        reranked = [dict(head[i]) for i in order if i < len(head)]
        for i, row in zip(order, reranked):
            row["rerank_score"] = round(float(score_by_idx[i]), 6)
        missing = [head[i] for i in range(len(head)) if i not in set(order)]
        return reranked + missing + tail

    def get_vector_source_text(self, *, record_id: str, source_id: Optional[str] = None) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .source_hydration import hydrate_record_text

        rid = str(record_id or "").strip()
        if not rid:
            return {"record_id": "", "content": "", "found": False}

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            return {"record_id": rid, "content": "", "found": False, "error": "database_unavailable"}

        meta = conn.execute(
            "SELECT source_id, record_type FROM signal_embeddings WHERE record_id=? LIMIT 1",
            (rid,),
        ).fetchone()
        resolved_source = source_id or (meta[0] if meta else None)
        resolved_type = meta[1] if meta else None
        result = hydrate_record_text(
            conn,
            rid,
            source_id=resolved_source,
            record_type=resolved_type,
        )
        return {
            "record_id": result.record_id,
            "content": result.content,
            "found": result.found,
            "source_id": result.source_id,
            "table": result.table,
            "truncated": result.truncated,
            "error": result.error,
        }

    def list_graph(
        self,
        *,
        dimension: Optional[str] = None,
        limit_nodes: int = 200,
        limit_edges: int = 500,
        edge_type: Optional[str] = None,
        min_weight: Optional[float] = None,
        source_id: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        limit_nodes = max(1, min(int(limit_nodes), 1000))
        limit_edges = max(1, min(int(limit_edges), 2000))
        graph = self._adapters.graph.list_graph(
            dimension=dimension,
            limit_nodes=limit_nodes,
            limit_edges=limit_edges,
        )
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        if source_id:
            nodes = [n for n in nodes if n.get("source_id") == source_id]
            edges = [e for e in edges if e.get("source_id") == source_id]
        if edge_type:
            # W4.1: pack predicates are open-ended edge vocabulary — a trailing
            # ".*" filters the FAMILY ("rel.*" matches every rel. predicate),
            # exact strings keep exact semantics.
            if edge_type.endswith(".*"):
                fam = edge_type[:-1]  # "rel.*" -> "rel."
                edges = [e for e in edges if str(e.get("edge_type") or "").startswith(fam)]
            else:
                edges = [e for e in edges if e.get("edge_type") == edge_type]
        if min_weight is not None:
            edges = [e for e in edges if float(e.get("weight") or 0) >= min_weight]
        from .graph_sanitize import ensure_graph_endpoints

        nodes, edges = ensure_graph_endpoints(nodes, edges)
        return {"nodes": nodes, "edges": edges}

    def _health_conn(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            from ...core.state import get_db_connection

            return get_db_connection()
        except Exception:
            return None

    def _cached_profiles(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        cache_key = ",".join(sorted(deferred_jobs or []))
        now = time.monotonic()
        cached = _DATA_HEALTH_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _DATA_HEALTH_TTL_SEC:
            return cached[1]
        profiles = DataHealthComputer(self._adapters, self._health_conn()).compute(
            deferred_jobs=deferred_jobs
        )
        _DATA_HEALTH_CACHE[cache_key] = (now, profiles)
        return profiles

    def list_dimensions(self) -> Dict[str, Any]:
        profiles = self._cached_profiles()
        dimensions = []
        for dim in SIGNAL_DIMENSIONS:
            p = profiles.get(dim["id"], {})
            dimensions.append(
                {
                    "id": dim["id"],
                    "label": dim["label"],
                    "score": p.get("score", 0.0),
                    "coverage_score": p.get("coverage_score", 0.0),
                    "freshness_score": p.get("freshness_score", 0.0),
                    "measured": p.get("measured", False),
                    "canonical_sources": p.get("canonical_sources", []),
                    "updated_at": p.get("updated_at"),
                }
            )
        return {"dimensions": dimensions}

    def get_data_health(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Any]:
        from .data_health import check_provider_status
        from .model_recommendations import signal_model_recommendation

        profiles = self._cached_profiles(deferred_jobs=deferred_jobs)
        fit_readiness: Dict[str, float] = {}
        conn = self._health_conn()
        try:
            from ..fit.evaluator import compute_fit_readiness

            if conn is not None:
                fit_readiness = compute_fit_readiness(conn)
        except Exception:
            fit_readiness = {}
        try:
            model_recommendations = signal_model_recommendation(conn)
        except Exception:
            model_recommendations = {}
        return {
            "dimensions": list(profiles.values()),
            "fit_readiness": fit_readiness,
            "provider_status": check_provider_status(),
            "model_recommendations": model_recommendations,
            "updated_at": profiles.get("memory", {}).get("updated_at"),
        }

    def list_topic_clusters(
        self,
        *,
        guard: "BlackholeGuard",
        limit: int = 50,
        dimension: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List topic clusters, minus any the caller may not see.

        ``guard`` is required and keyword-only, the same discipline
        ``features/entities/reads.py`` uses: a cluster row carries no entity id
        to join on, so a call site that forgot to pass one would leak silently
        rather than fail. Making it required turns that into a TypeError.

        ``total`` counts what is returned, not what exists — a total that
        disagrees with the rows confirms something was withheld, which is the
        side channel D5 rules out.
        """
        from ...core.state import get_db_connection
        from .topic_clustering import load_topic_clusters_for_query

        limit = max(1, min(int(limit), 200))
        conn = get_db_connection()
        items = load_topic_clusters_for_query(conn, limit=limit, dimension=dimension) if conn else []
        items = guard.filter_name_string_artifacts(items, text_keys=_CLUSTER_TEXT_KEYS)
        return {"items": items, "total": len(items), "limit": limit}

    def list_topic_cluster_members(
        self,
        cluster_id: str,
        *,
        guard: "BlackholeGuard",
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Members of one cluster, minus any excerpt the caller may not see.

        A withheld cluster answers exactly as a missing one does (``LookupError``
        → 404): confirming that a cluster exists but is hidden would tell a
        caller the protected entity exists, which is the distinction D5 removes.
        """
        from ...core.state import get_db_connection
        from .topic_clustering import load_topic_cluster_members

        limit = max(1, min(int(limit), 500))
        conn = get_db_connection()
        if conn is None:
            return {"items": [], "total": 0, "cluster_id": cluster_id, "limit": limit}
        row = conn.execute(
            "SELECT cluster_id, label, centroid_preview, label_terms_json"
            " FROM topic_clusters WHERE cluster_id=? LIMIT 1",
            (cluster_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"topic cluster not found: {cluster_id}")
        cluster = {
            "cluster_id": row[0],
            "label": row[1],
            "centroid_preview": row[2],
            "label_terms": _safe_json_list(row[3]),
        }
        if not guard.filter_name_string_artifacts([cluster], text_keys=_CLUSTER_TEXT_KEYS):
            raise LookupError(f"topic cluster not found: {cluster_id}")
        items = load_topic_cluster_members(conn, cluster_id, limit=limit)
        items = guard.filter_name_string_artifacts(items, text_keys=("text_preview",))
        return {"items": items, "total": len(items), "cluster_id": cluster_id, "limit": limit}

    def list_briefs(self) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .dimension_briefs import DimensionBriefStore

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            return {"items": [], "total": 0}
        items = DimensionBriefStore(conn).list_briefs()
        return {"items": items, "total": len(items)}

    def get_brief(self, dimension: str) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .dimension_briefs import DimensionBriefStore

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            raise RuntimeError("database_unavailable")
        return DimensionBriefStore(conn).get_brief(dimension)

    def update_brief(self, dimension: str, markdown_body: str) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .dimension_briefs import DimensionBriefStore

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            raise RuntimeError("database_unavailable")
        return DimensionBriefStore(conn).save_user_edit(dimension, markdown_body)

    def list_brief_revisions(self, dimension: str, *, limit: int = 20) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .dimension_briefs import DimensionBriefStore

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            return {"items": [], "total": 0, "signal_dimension": dimension}
        items = DimensionBriefStore(conn).list_revisions(dimension, limit=limit)
        return {"items": items, "total": len(items), "signal_dimension": dimension, "limit": limit}

    async def refresh_brief(self, dimension: str, *, limit: int = 40) -> Dict[str, Any]:
        """Run LLM brief update for one dimension from recent canonical rows."""
        from ...core.state import get_db_connection
        from ...enrichment.jobs.canonical.dimension_summary_job import DimensionSummaryJob
        from .brief_canonical_loader import load_canonical_messages_for_dimension

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            return {"ok": False, "error": "database_unavailable"}

        messages = load_canonical_messages_for_dimension(conn, dimension, limit=limit)
        if not messages:
            return {"ok": False, "error": "no_canonical_messages"}

        job = DimensionSummaryJob()
        records = await job.enrich(messages, only_dimension=dimension)
        if records and records[0].get("_deferred"):
            return {"ok": False, "error": records[0].get("error") or "deferred"}
        brief = self.get_brief(dimension)
        return {"ok": True, "brief": brief, "job_result": records[0] if records else {}}

    def list_definitions(self) -> Dict[str, Any]:
        from .dimension_definition_loader import list_definition_ids, get_definition

        items = [get_definition(dim_id) for dim_id in list_definition_ids()]
        return {"items": items, "total": len(items)}

    def get_definition(self, dimension: str) -> Dict[str, Any]:
        from .dimension_definition_loader import DimensionDefinitionError, get_definition

        try:
            return get_definition(dimension)
        except DimensionDefinitionError as exc:
            raise ValueError(str(exc)) from exc

    def list_signal_objects(
        self,
        dimension: str,
        *,
        object_type: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .signal_object_store import SignalObjectStore

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            return {"items": [], "total": 0, "envelope": {"applied_limit": limit, "requested_limit": limit}}
        limit = max(1, min(int(limit), 200))
        try:
            store = SignalObjectStore(conn)
            items = store.list_objects(
                dimension, object_type=object_type, limit=limit
            )
            count_params: List[Any] = [str(dimension).strip().lower()]
            type_clause = ""
            if object_type:
                type_clause = " AND object_type=?"
                count_params.append(str(object_type).strip())
            total_row = conn.execute(
                f"""
                SELECT COUNT(*) FROM signal_objects
                WHERE signal_dimension=? AND valid_to IS NULL{type_clause}
                """,
                count_params,
            ).fetchone()
            total = int(total_row[0] if total_row else len(items))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return {
            "items": items,
            "total": total,
            "envelope": {"applied_limit": limit, "requested_limit": limit},
        }

    def get_signal_object(self, object_id: str) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .signal_object_store import SignalObjectStore

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            raise RuntimeError("database_unavailable")
        try:
            return SignalObjectStore(conn).get_object(object_id)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def owner_override_signal_object(
        self, object_id: str, payload_patch: Dict[str, Any]
    ) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .signal_object_store import SignalObjectStore

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            raise RuntimeError("database_unavailable")
        return SignalObjectStore(conn).owner_override(object_id, payload_patch)

    def evaluate_fit(
        self,
        opportunity_type: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from ..fit.evaluator import evaluate_opportunity

        conn = self._conn if self._conn is not None else get_db_connection()
        if conn is None:
            raise RuntimeError("database_unavailable")
        try:
            return evaluate_opportunity(conn, opportunity_type, context=context)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


def get_signal_service(conn=None) -> SignalService:
    from ...storage.adapters.factory import AdapterFactory

    if conn is None:
        from ...core.state import get_db_connection

        conn = get_db_connection()
    if conn is not None:
        bundle = AdapterFactory.create("local_database", conn=conn)
    else:
        bundle = AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
    return SignalService(bundle, conn=conn)
