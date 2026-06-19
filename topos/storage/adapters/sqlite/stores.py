"""SQLite-backed storage adapters (Phase 0 minimal implementations)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from ..protocols import ListPage


class SQLiteCanonicalStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, table: str, record: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> str:
        record_id = str(record.get("record_id") or record.get("id") or idempotency_key or uuid.uuid4())
        payload = json.dumps({**record, "record_id": record_id})
        self._conn.execute(
            """
            INSERT INTO wiki_canonical_records (record_id, table_name, source_id, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(record_id) DO UPDATE SET
                table_name=excluded.table_name,
                source_id=excluded.source_id,
                payload_json=excluded.payload_json
            """,
            (record_id, table, record.get("source_id"), payload),
        )
        self._conn.commit()
        return record_id

    def get(self, table: str, record_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT payload_json FROM wiki_canonical_records WHERE record_id=? AND table_name=?",
            (record_id, table),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def list(
        self,
        table: str,
        *,
        source_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage:
        query = "SELECT payload_json FROM wiki_canonical_records WHERE table_name=?"
        params: List[Any] = [table]
        if source_id is not None:
            query += " AND source_id=?"
            params.append(source_id)
        count_row = self._conn.execute(
            f"SELECT COUNT(*) FROM ({query})",
            params,
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
        query += " ORDER BY record_id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(query, params).fetchall()
        items = [json.loads(r[0]) for r in rows]
        return ListPage(items=items, total=total, offset=offset, limit=limit)

    def delete(self, table: str, record_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM wiki_canonical_records WHERE record_id=? AND table_name=?",
            (record_id, table),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def count(self, table: str, *, source_id: Optional[str] = None) -> int:
        return self.list(table, source_id=source_id, limit=1, offset=0).total


class SQLiteSignalFeatureStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def put_fact(self, fact: Dict[str, Any]) -> str:
        fact_id = str(fact.get("fact_id") or uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO signal_facts (fact_id, dimension, source_id, record_id, payload_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fact_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (
                fact_id,
                fact.get("dimension"),
                fact.get("source_id"),
                fact.get("record_id"),
                json.dumps({**fact, "fact_id": fact_id}),
            ),
        )
        self._conn.commit()
        return fact_id

    def put_score(self, score: Dict[str, Any]) -> str:
        score_id = str(score.get("score_id") or uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO signal_scores (score_id, dimension, source_id, record_id, payload_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(score_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (
                score_id,
                score.get("dimension"),
                score.get("source_id"),
                score.get("record_id"),
                json.dumps({**score, "score_id": score_id}),
            ),
        )
        self._conn.commit()
        return score_id

    def put_summary(self, summary: Dict[str, Any]) -> str:
        summary_id = str(summary.get("summary_id") or uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO signal_summaries (summary_id, dimension, source_id, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(summary_id) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (
                summary_id,
                summary.get("dimension"),
                summary.get("source_id"),
                json.dumps({**summary, "summary_id": summary_id}),
            ),
        )
        self._conn.commit()
        return summary_id

    def _list_rows(self, table: str, *, dimension: Optional[str], limit: int, offset: int) -> ListPage:
        query = f"SELECT payload_json FROM {table}"
        params: List[Any] = []
        if dimension is not None:
            query += " WHERE dimension=?"
            params.append(dimension)
        count = self._conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()
        total = int(count[0]) if count else 0
        query += " ORDER BY rowid LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        items = [json.loads(r[0]) for r in self._conn.execute(query, params).fetchall()]
        return ListPage(items=items, total=total, offset=offset, limit=limit)

    def get_by_dimension(self, dimension: str, *, limit: int = 100, offset: int = 0) -> ListPage:
        facts = self._list_rows("signal_facts", dimension=dimension, limit=limit, offset=offset).items
        scores = self._list_rows("signal_scores", dimension=dimension, limit=limit, offset=offset).items
        summaries = self._list_rows("signal_summaries", dimension=dimension, limit=limit, offset=offset).items
        items = facts + scores + summaries
        return ListPage(items=items[:limit], total=len(items), offset=offset, limit=limit)

    def list(self, *, dimension: Optional[str] = None, limit: int = 100, offset: int = 0) -> ListPage:
        if dimension:
            return self.get_by_dimension(dimension, limit=limit, offset=offset)
        facts = self._list_rows("signal_facts", dimension=None, limit=limit, offset=offset)
        return facts


class SQLiteVectorIndex:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, metadata: Dict[str, Any], *, vector: Optional[List[float]] = None) -> str:
        embedding_id = str(metadata.get("embedding_id") or uuid.uuid4())
        vector_blob = json.dumps(vector).encode("utf-8") if vector is not None else None
        self._conn.execute(
            """
            INSERT INTO signal_embeddings (
                embedding_id, record_id, source_id, signal_dimension, model, provider,
                dims, text_preview, provenance_json, vector_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(embedding_id) DO UPDATE SET
                provenance_json=excluded.provenance_json,
                vector_blob=excluded.vector_blob
            """,
            (
                embedding_id,
                metadata.get("record_id"),
                metadata.get("source_id"),
                metadata.get("signal_dimension"),
                metadata.get("model"),
                metadata.get("provider"),
                metadata.get("dims"),
                metadata.get("text_preview"),
                json.dumps({**metadata, "embedding_id": embedding_id}),
                vector_blob,
            ),
        )
        self._conn.commit()
        return embedding_id

    def get_metadata(self, embedding_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT provenance_json FROM signal_embeddings WHERE embedding_id=?",
            (embedding_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list_metadata(
        self,
        *,
        source_id: Optional[str] = None,
        dimension: Optional[str] = None,
        model: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage:
        query = "SELECT provenance_json FROM signal_embeddings WHERE 1=1"
        params: List[Any] = []
        if source_id is not None:
            query += " AND source_id=?"
            params.append(source_id)
        if dimension is not None:
            query += " AND signal_dimension=?"
            params.append(dimension)
        if model is not None:
            query += " AND model=?"
            params.append(model)
        total_row = self._conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()
        total = int(total_row[0]) if total_row else 0
        query += " ORDER BY embedding_id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        items = [json.loads(r[0]) for r in self._conn.execute(query, params).fetchall()]
        return ListPage(items=items, total=total, offset=offset, limit=limit)

    def delete_by_record(self, record_id: str) -> int:
        cur = self._conn.execute("DELETE FROM signal_embeddings WHERE record_id=?", (record_id,))
        self._conn.commit()
        return cur.rowcount


class SQLiteGraphEdgeStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_node(self, node: Dict[str, Any]) -> str:
        node_id = str(node.get("node_id") or uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO graph_nodes (node_id, node_type, label, metadata_json, source_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET metadata_json=excluded.metadata_json
            """,
            (
                node_id,
                node.get("node_type"),
                node.get("label"),
                json.dumps({**node, "node_id": node_id}),
                node.get("source_id"),
            ),
        )
        self._conn.commit()
        return node_id

    def upsert_edge(self, edge: Dict[str, Any]) -> str:
        edge_id = str(edge.get("edge_id") or uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO graph_edges (
                edge_id, src_node_id, dst_node_id, edge_type, weight, metadata_json, source_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_id) DO UPDATE SET metadata_json=excluded.metadata_json
            """,
            (
                edge_id,
                edge.get("src_node_id"),
                edge.get("dst_node_id"),
                edge.get("edge_type"),
                edge.get("weight"),
                json.dumps({**edge, "edge_id": edge_id}),
                edge.get("source_id"),
            ),
        )
        self._conn.commit()
        return edge_id

    def list_graph(
        self,
        *,
        dimension: Optional[str] = None,
        limit_nodes: int = 200,
        limit_edges: int = 500,
    ) -> Dict[str, List[Dict[str, Any]]]:
        node_query = "SELECT metadata_json FROM graph_nodes"
        edge_query = "SELECT metadata_json FROM graph_edges"
        params: List[Any] = []
        if dimension is not None:
            node_query += " WHERE json_extract(metadata_json, '$.dimension')=?"
            edge_query += " WHERE json_extract(metadata_json, '$.dimension')=?"
            params = [dimension]
        nodes = [
            json.loads(r[0])
            for r in self._conn.execute(f"{node_query} LIMIT ?", (*params, limit_nodes)).fetchall()
        ]
        edges = [
            json.loads(r[0])
            for r in self._conn.execute(f"{edge_query} LIMIT ?", (*params, limit_edges)).fetchall()
        ]
        return {"nodes": nodes, "edges": edges}

    def neighbors(self, node_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT metadata_json FROM graph_edges
            WHERE src_node_id=? OR dst_node_id=?
            LIMIT ?
            """,
            (node_id, node_id, limit),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]


class SQLiteAuditLogStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, event: Dict[str, Any]) -> str:
        event_id = str(event.get("event_id") or uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO query_audit_events (event_id, session_id, event_type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (event_id, event.get("session_id"), event.get("event_type"), json.dumps({**event, "event_id": event_id})),
        )
        self._conn.commit()
        return event_id

    def query(
        self,
        *,
        session_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ListPage:
        query = "SELECT payload_json FROM query_audit_events WHERE 1=1"
        params: List[Any] = []
        if session_id is not None:
            query += " AND session_id=?"
            params.append(session_id)
        total_row = self._conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()
        total = int(total_row[0]) if total_row else 0
        query += " ORDER BY rowid LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        items = [json.loads(r[0]) for r in self._conn.execute(query, params).fetchall()]
        return ListPage(items=items, total=total, offset=offset, limit=limit)


class SQLiteQuerySessionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT envelope_json, requester_id, intent_hash, ttl_expires_at FROM query_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        raw_envelope = row[0]
        if raw_envelope:
            try:
                parsed = json.loads(raw_envelope)
                envelope = parsed.get("envelope_json") if isinstance(parsed, dict) and "envelope_json" in parsed else parsed
            except json.JSONDecodeError:
                envelope = {}
        else:
            envelope = {}
        if not isinstance(envelope, dict):
            envelope = {}
        session = {
            "session_id": session_id,
            "requester_id": row[1],
            "intent_hash": row[2],
            "ttl_expires_at": row[3],
            "envelope_json": envelope,
        }
        artifacts = [
            {
                "artifact_id": r[0],
                "cache_key": r[1],
                "public_result_json": json.loads(r[2]) if r[2] else None,
                "retrieval_fingerprint": r[3],
                "game_layer_strategy": r[4],
            }
            for r in self._conn.execute(
                """
                SELECT artifact_id, cache_key, public_result_json, retrieval_fingerprint, game_layer_strategy
                FROM query_artifacts WHERE session_id=? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
        ]
        return {**session, "artifacts": artifacts}

    def put(self, session: Dict[str, Any]) -> str:
        session_id = str(session["session_id"])
        envelope = session.get("envelope_json") or {}
        if not isinstance(envelope, dict):
            envelope = {}
        self._conn.execute(
            """
            INSERT INTO query_sessions (session_id, requester_id, intent_hash, envelope_json, ttl_expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                requester_id=excluded.requester_id,
                intent_hash=excluded.intent_hash,
                envelope_json=excluded.envelope_json,
                ttl_expires_at=excluded.ttl_expires_at,
                updated_at=datetime('now')
            """,
            (
                session_id,
                session.get("requester_id"),
                session.get("intent_hash"),
                json.dumps(envelope),
                session.get("ttl_expires_at") or session.get("expires_at"),
            ),
        )
        self._conn.commit()
        return session_id

    def append_artifact(self, session_id: str, artifact: Dict[str, Any]) -> str:
        public_result = artifact.get("public_result_json")
        if public_result is not None:
            from topos.query.session_utils import validate_public_result

            payload = public_result if isinstance(public_result, dict) else {}
            validate_public_result(payload)
        artifact_id = str(artifact.get("artifact_id") or uuid.uuid4())
        if public_result is not None and not isinstance(public_result, str):
            public_result = json.dumps(public_result)
        self._conn.execute(
            """
            INSERT INTO query_artifacts (
                artifact_id, session_id, cache_key, public_result_json,
                retrieval_fingerprint, game_layer_strategy
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                session_id,
                artifact.get("cache_key"),
                public_result,
                artifact.get("retrieval_fingerprint"),
                artifact.get("game_layer_strategy"),
            ),
        )
        self._conn.commit()
        return artifact_id

    def invalidate(self, session_id: str, *, cache_key: Optional[str] = None) -> int:
        if cache_key is None:
            cur = self._conn.execute("DELETE FROM query_artifacts WHERE session_id=?", (session_id,))
        else:
            cur = self._conn.execute(
                "DELETE FROM query_artifacts WHERE session_id=? AND cache_key=?",
                (session_id, cache_key),
            )
        self._conn.commit()
        return cur.rowcount

    def purge_expired(self) -> int:
        expired = self._conn.execute(
            "SELECT session_id FROM query_sessions WHERE ttl_expires_at IS NOT NULL AND ttl_expires_at < datetime('now')"
        ).fetchall()
        count = 0
        for (session_id,) in expired:
            self._conn.execute("DELETE FROM query_artifacts WHERE session_id=?", (session_id,))
            count += 1
        cur = self._conn.execute(
            "DELETE FROM query_sessions WHERE ttl_expires_at IS NOT NULL AND ttl_expires_at < datetime('now')"
        )
        self._conn.commit()
        return count + cur.rowcount
