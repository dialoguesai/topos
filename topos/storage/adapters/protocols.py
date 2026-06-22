"""Wiki MVP storage adapter protocols (Phase 0). See PRD §7."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ListPage:
    items: List[Dict[str, Any]]
    total: int
    offset: int
    limit: int


@runtime_checkable
class CanonicalStore(Protocol):
    def upsert(self, table: str, record: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> str: ...
    def get(self, table: str, record_id: str) -> Optional[Dict[str, Any]]: ...
    def list(
        self,
        table: str,
        *,
        source_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        disclosure_tier: str = "owner_raw",
    ) -> ListPage: ...
    def delete(self, table: str, record_id: str) -> bool: ...
    def count(self, table: str, *, source_id: Optional[str] = None) -> int: ...


@runtime_checkable
class SignalFeatureStore(Protocol):
    def put_fact(self, fact: Dict[str, Any]) -> str: ...
    def put_score(self, score: Dict[str, Any]) -> str: ...
    def put_summary(self, summary: Dict[str, Any]) -> str: ...
    def get_by_dimension(self, dimension: str, *, limit: int = 100, offset: int = 0) -> ListPage: ...
    def list(self, *, dimension: Optional[str] = None, limit: int = 100, offset: int = 0) -> ListPage: ...


@runtime_checkable
class VectorIndex(Protocol):
    def upsert(self, metadata: Dict[str, Any], *, vector: Optional[List[float]] = None) -> str: ...
    def get_metadata(self, embedding_id: str) -> Optional[Dict[str, Any]]: ...
    def list_metadata(
        self,
        *,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage: ...
    def search_similar(
        self,
        query_vector: List[float],
        *,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        limit: int = 20,
        event_after: Optional[str] = None,
        event_before: Optional[str] = None,
        fetch_limit: Optional[int] = None,
    ) -> ListPage: ...
    def delete_by_record(self, record_id: str) -> int: ...


@runtime_checkable
class GraphEdgeStore(Protocol):
    def upsert_node(self, node: Dict[str, Any]) -> str: ...
    def upsert_edge(self, edge: Dict[str, Any]) -> str: ...
    def list_graph(
        self,
        *,
        dimension: Optional[str] = None,
        limit_nodes: int = 200,
        limit_edges: int = 500,
    ) -> Dict[str, List[Dict[str, Any]]]: ...
    def neighbors(self, node_id: str, *, limit: int = 50) -> List[Dict[str, Any]]: ...


@runtime_checkable
class AuditLogStore(Protocol):
    def append(self, event: Dict[str, Any]) -> str: ...
    def query(
        self,
        *,
        session_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage: ...


@runtime_checkable
class QuerySessionStore(Protocol):
    def get(self, session_id: str) -> Optional[Dict[str, Any]]: ...
    def put(self, session: Dict[str, Any]) -> str: ...
    def append_artifact(self, session_id: str, artifact: Dict[str, Any]) -> str: ...
    def invalidate(self, session_id: str, *, cache_key: Optional[str] = None) -> int: ...
    def purge_expired(self) -> int: ...
