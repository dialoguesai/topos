"""Signal read service shared by HTTP and WS handlers."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from ...storage.adapters.factory import AdapterBundle
from .data_health import DataHealthComputer
from .dimension_registry import MVP_DIMENSIONS
from .schemas import strip_vector_fields

_DATA_HEALTH_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_DATA_HEALTH_TTL_SEC = 30.0


class SignalService:
    def __init__(self, adapters: AdapterBundle) -> None:
        self._adapters = adapters

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
    ) -> Dict[str, Any]:
        from ...engine.backends.huggingface import DEFAULT_EMBEDDING_MODEL, HuggingFaceAdapter

        q = str(query or "").strip()
        limit = max(1, min(int(limit), 100))
        embed_model = model or DEFAULT_EMBEDDING_MODEL
        if not q:
            return {"items": [], "total": 0, "query": "", "model": embed_model, "limit": limit}

        hf = HuggingFaceAdapter()
        emb = hf.run_inference({"text": q}, {"subtype": "embedding", "model": embed_model})
        vectors = emb.get("vectors") or []
        if not vectors:
            return {
                "items": [],
                "total": 0,
                "query": q,
                "model": embed_model,
                "limit": limit,
                "error": "embedding_failed",
            }

        query_vector = [float(x) for x in vectors[0]]
        vector_index = self._adapters.vector
        search = getattr(vector_index, "search_similar", None)
        if search is None:
            return {
                "items": [],
                "total": 0,
                "query": q,
                "model": embed_model,
                "limit": limit,
                "error": "vector_search_unsupported",
            }

        page = search(
            query_vector,
            source_id=source_id,
            dimension=dimension,
            model=embed_model,
            limit=limit,
        )
        items = [strip_vector_fields(item) for item in page.items]
        return {"items": items, "total": page.total, "query": q, "model": embed_model, "limit": limit}

    def get_vector_source_text(self, *, record_id: str) -> Dict[str, Any]:
        from ...core.state import get_db_connection

        rid = str(record_id or "").strip()
        if not rid:
            return {"record_id": "", "content": "", "found": False}

        conn = get_db_connection()
        if conn is None:
            return {"record_id": rid, "content": "", "found": False, "error": "database_unavailable"}

        try:
            row = conn.execute(
                """
                SELECT message_id, content, content_rendered, source_id, conversation_id, sender_type
                FROM ai_chat_messages
                WHERE message_id = ?
                LIMIT 1
                """,
                (rid,),
            ).fetchone()
        except Exception as exc:
            return {"record_id": rid, "content": "", "found": False, "error": str(exc)}

        if not row:
            return {"record_id": rid, "content": "", "found": False}

        content = str(row[1] or row[2] or "").strip()
        return {
            "record_id": str(row[0]),
            "content": content,
            "source_id": row[3],
            "conversation_id": row[4],
            "sender_type": row[5],
            "found": True,
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
            edges = [e for e in edges if e.get("edge_type") == edge_type]
        if min_weight is not None:
            edges = [e for e in edges if float(e.get("weight") or 0) >= min_weight]
        from .graph_sanitize import ensure_graph_endpoints

        nodes, edges = ensure_graph_endpoints(nodes, edges)
        return {"nodes": nodes, "edges": edges}

    def _cached_profiles(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        cache_key = ",".join(sorted(deferred_jobs or []))
        now = time.monotonic()
        cached = _DATA_HEALTH_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _DATA_HEALTH_TTL_SEC:
            return cached[1]
        profiles = DataHealthComputer(self._adapters).compute(deferred_jobs=deferred_jobs)
        _DATA_HEALTH_CACHE[cache_key] = (now, profiles)
        return profiles

    def list_dimensions(self) -> Dict[str, Any]:
        profiles = self._cached_profiles()
        dimensions = []
        for dim in MVP_DIMENSIONS:
            p = profiles.get(dim["id"], {})
            dimensions.append(
                {
                    "id": dim["id"],
                    "label": dim["label"],
                    "coverage_score": p.get("coverage_score", 0.0),
                    "freshness_score": p.get("freshness_score", 0.0),
                    "canonical_sources": p.get("canonical_sources", []),
                    "updated_at": p.get("updated_at"),
                }
            )
        return {"dimensions": dimensions}

    def get_data_health(self, deferred_jobs: Optional[List[str]] = None) -> Dict[str, Any]:
        profiles = self._cached_profiles(deferred_jobs=deferred_jobs)
        ollama_up = "ollama_unreachable" not in {
            f for p in profiles.values() for f in p.get("provider_failures") or []
        }
        return {
            "dimensions": list(profiles.values()),
            "provider_status": {"ollama": "up" if ollama_up else "down", "huggingface": "up"},
            "updated_at": profiles.get("memory", {}).get("updated_at"),
        }

    def list_topic_clusters(
        self,
        *,
        limit: int = 50,
        dimension: Optional[str] = None,
    ) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .topic_clustering import load_topic_clusters_for_query

        limit = max(1, min(int(limit), 200))
        conn = get_db_connection()
        items = load_topic_clusters_for_query(conn, limit=limit, dimension=dimension) if conn else []
        return {"items": items, "total": len(items), "limit": limit}

    def list_topic_cluster_members(
        self,
        cluster_id: str,
        *,
        limit: int = 100,
    ) -> Dict[str, Any]:
        from ...core.state import get_db_connection
        from .topic_clustering import load_topic_cluster_members

        limit = max(1, min(int(limit), 500))
        conn = get_db_connection()
        items = (
            load_topic_cluster_members(conn, cluster_id, limit=limit) if conn else []
        )
        return {"items": items, "total": len(items), "cluster_id": cluster_id, "limit": limit}


def get_signal_service(conn=None) -> SignalService:
    from ...storage.adapters.factory import AdapterFactory

    if conn is None:
        from ...core.state import get_db_connection

        conn = get_db_connection()
    if conn is not None:
        bundle = AdapterFactory.create("local_database", conn=conn)
    else:
        bundle = AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
    return SignalService(bundle)
