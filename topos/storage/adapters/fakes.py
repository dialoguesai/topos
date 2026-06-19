"""In-memory adapter fakes for contract tests (Phase 0)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .protocols import ListPage


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryCanonicalStore:
    def __init__(self) -> None:
        self._tables: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def upsert(self, table: str, record: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> str:
        record_id = str(record.get("record_id") or record.get("id") or idempotency_key or uuid.uuid4())
        bucket = self._tables.setdefault(table, {})
        bucket[record_id] = {**record, "record_id": record_id}
        return record_id

    def get(self, table: str, record_id: str) -> Optional[Dict[str, Any]]:
        return self._tables.get(table, {}).get(record_id)

    def list(
        self,
        table: str,
        *,
        source_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage:
        rows = list(self._tables.get(table, {}).values())
        if source_id is not None:
            rows = [r for r in rows if r.get("source_id") == source_id]
        total = len(rows)
        page = rows[offset : offset + limit]
        return ListPage(items=page, total=total, offset=offset, limit=limit)

    def delete(self, table: str, record_id: str) -> bool:
        bucket = self._tables.get(table, {})
        if record_id in bucket:
            del bucket[record_id]
            return True
        return False

    def count(self, table: str, *, source_id: Optional[str] = None) -> int:
        return self.list(table, source_id=source_id, limit=10_000, offset=0).total


class InMemorySignalFeatureStore:
    def __init__(self) -> None:
        self._facts: List[Dict[str, Any]] = []
        self._scores: List[Dict[str, Any]] = []
        self._summaries: List[Dict[str, Any]] = []

    def put_fact(self, fact: Dict[str, Any]) -> str:
        fact_id = str(fact.get("fact_id") or uuid.uuid4())
        self._facts.append({**fact, "fact_id": fact_id})
        return fact_id

    def put_score(self, score: Dict[str, Any]) -> str:
        score_id = str(score.get("score_id") or uuid.uuid4())
        self._scores.append({**score, "score_id": score_id})
        return score_id

    def put_summary(self, summary: Dict[str, Any]) -> str:
        summary_id = str(summary.get("summary_id") or uuid.uuid4())
        self._summaries.append({**summary, "summary_id": summary_id})
        return summary_id

    def _page(self, rows: List[Dict[str, Any]], *, limit: int, offset: int) -> ListPage:
        total = len(rows)
        return ListPage(items=rows[offset : offset + limit], total=total, offset=offset, limit=limit)

    def get_by_dimension(self, dimension: str, *, limit: int = 100, offset: int = 0) -> ListPage:
        rows = [r for r in self._facts + self._scores + self._summaries if r.get("dimension") == dimension]
        return self._page(rows, limit=limit, offset=offset)

    def list(self, *, dimension: Optional[str] = None, limit: int = 100, offset: int = 0) -> ListPage:
        if dimension:
            return self.get_by_dimension(dimension, limit=limit, offset=offset)
        rows = self._facts + self._scores + self._summaries
        return self._page(rows, limit=limit, offset=offset)


class InMemoryVectorIndex:
    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}

    def upsert(self, metadata: Dict[str, Any], *, vector: Optional[List[float]] = None) -> str:
        embedding_id = str(metadata.get("embedding_id") or uuid.uuid4())
        self._items[embedding_id] = {**metadata, "embedding_id": embedding_id, "vector": vector}
        return embedding_id

    def get_metadata(self, embedding_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(embedding_id)
        if item is None:
            return None
        return {k: v for k, v in item.items() if k != "vector"}

    def list_metadata(
        self,
        *,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage:
        rows = list(self._items.values())
        if source_id is not None:
            rows = [r for r in rows if r.get("source_id") == source_id]
        if dimension is not None:
            rows = [r for r in rows if r.get("signal_dimension") == dimension]
        if model is not None:
            rows = [r for r in rows if r.get("model") == model]
        total = len(rows)
        page = [{k: v for k, v in r.items() if k != "vector"} for r in rows[offset : offset + limit]]
        return ListPage(items=page, total=total, offset=offset, limit=limit)

    def search_similar(
        self,
        query_vector: List[float],
        *,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        limit: int = 20,
    ) -> ListPage:
        from ...features.signal.vector_math import cosine_similarity

        limit = max(1, min(int(limit), 100))
        rows = list(self._items.values())
        if source_id is not None:
            rows = [r for r in rows if r.get("source_id") == source_id]
        if dimension is not None:
            rows = [r for r in rows if r.get("signal_dimension") == dimension]
        if model is not None:
            rows = [r for r in rows if r.get("model") == model]
        query_dims = len(query_vector)
        scored: List[tuple[float, Dict[str, Any]]] = []
        for row in rows:
            vector = row.get("vector")
            if not isinstance(vector, list) or len(vector) != query_dims:
                continue
            sim = cosine_similarity(query_vector, [float(x) for x in vector])
            meta = {k: v for k, v in row.items() if k != "vector"}
            meta["similarity"] = round(sim, 6)
            scored.append((sim, meta))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = scored[:limit]
        items = [meta for _, meta in top]
        return ListPage(items=items, total=len(scored), offset=0, limit=limit)

    def delete_by_record(self, record_id: str) -> int:
        to_delete = [k for k, v in self._items.items() if v.get("record_id") == record_id]
        for key in to_delete:
            del self._items[key]
        return len(to_delete)


class InMemoryGraphEdgeStore:
    def __init__(self) -> None:
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._edges: Dict[str, Dict[str, Any]] = {}

    def upsert_node(self, node: Dict[str, Any]) -> str:
        node_id = str(node.get("node_id") or uuid.uuid4())
        self._nodes[node_id] = {**node, "node_id": node_id}
        return node_id

    def upsert_edge(self, edge: Dict[str, Any]) -> str:
        edge_id = str(edge.get("edge_id") or uuid.uuid4())
        self._edges[edge_id] = {**edge, "edge_id": edge_id}
        return edge_id

    def list_graph(
        self,
        *,
        dimension: Optional[str] = None,
        limit_nodes: int = 200,
        limit_edges: int = 500,
    ) -> Dict[str, List[Dict[str, Any]]]:
        nodes = list(self._nodes.values())
        edges = list(self._edges.values())
        if dimension is not None:
            nodes = [n for n in nodes if n.get("dimension") == dimension]
            edges = [e for e in edges if e.get("dimension") == dimension]
        return {"nodes": nodes[:limit_nodes], "edges": edges[:limit_edges]}

    def neighbors(self, node_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        matching = [
            edge
            for edge in self._edges.values()
            if edge.get("src_node_id") == node_id or edge.get("dst_node_id") == node_id
        ]
        return matching[:limit]


class InMemoryAuditLogStore:
    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []

    def append(self, event: Dict[str, Any]) -> str:
        event_id = str(event.get("event_id") or uuid.uuid4())
        self._events.append({**event, "event_id": event_id, "created_at": _now_iso()})
        return event_id

    def query(
        self,
        *,
        session_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage:
        rows = self._events
        if session_id is not None:
            rows = [e for e in rows if e.get("session_id") == session_id]
        total = len(rows)
        return ListPage(items=rows[offset : offset + limit], total=total, offset=offset, limit=limit)


class InMemoryQuerySessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._artifacts: Dict[str, List[Dict[str, Any]]] = {}

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return {**session, "artifacts": list(self._artifacts.get(session_id, []))}

    def put(self, session: Dict[str, Any]) -> str:
        session_id = str(session["session_id"])
        self._sessions[session_id] = {k: v for k, v in session.items() if k != "artifacts"}
        self._artifacts.setdefault(session_id, [])
        return session_id

    def append_artifact(self, session_id: str, artifact: Dict[str, Any]) -> str:
        public_result = artifact.get("public_result_json")
        if public_result is not None:
            from topos.query.session_utils import validate_public_result

            payload = public_result if isinstance(public_result, dict) else {}
            validate_public_result(payload)
        artifact_id = str(artifact.get("artifact_id") or uuid.uuid4())
        bucket = self._artifacts.setdefault(session_id, [])
        bucket.append({**artifact, "artifact_id": artifact_id})
        return artifact_id

    def invalidate(self, session_id: str, *, cache_key: Optional[str] = None) -> int:
        if session_id not in self._artifacts:
            return 0
        if cache_key is None:
            count = len(self._artifacts[session_id])
            self._artifacts[session_id] = []
            return count
        before = len(self._artifacts[session_id])
        self._artifacts[session_id] = [a for a in self._artifacts[session_id] if a.get("cache_key") != cache_key]
        return before - len(self._artifacts[session_id])

    def purge_expired(self) -> int:
        return 0
