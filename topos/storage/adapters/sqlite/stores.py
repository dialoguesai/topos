"""SQLite-backed storage adapters (Phase 0 minimal implementations)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from ..protocols import ListPage
from ...canonical.canonical_store import SQLiteCanonicalStore as TypedSQLiteCanonicalStore

_NATIVE_TABLES = frozenset(
    {
        "ai_chat_messages",
        "conversation_messages",
        "activity_events",
        "financial_transactions",
        "location_events",
        "profile_records",
        "calendar_events",
        "journal_entries",
        "contacts",
        "contact_identifiers",
    }
)
_NATIVE_ID_COL = {
    "ai_chat_messages": "message_id",
    "conversation_messages": "message_id",
    "activity_events": "event_id",
    "financial_transactions": "transaction_id",
    "location_events": "event_id",
    "profile_records": "record_id",
    "calendar_events": "event_id",
    "journal_entries": "entry_id",
    "contacts": "contact_id",
    "contact_identifiers": "contact_id",
}
_NATIVE_LIST_SPECS: Dict[str, tuple[str, List[str]]] = {
    "ai_chat_messages": (
        """
        SELECT message_id AS record_id, conversation_id, sender_type, source_id,
               substr(coalesce(content_rendered, content), 1, 500) AS content_preview,
               content, event_at
        FROM ai_chat_messages
        """,
        [
            "record_id",
            "conversation_id",
            "sender_type",
            "source_id",
            "content_preview",
            "content",
            "event_at",
        ],
    ),
    "ai_chat_messages_disclosure": (
        """
        SELECT message_id AS record_id, conversation_id, sender_type, source_id,
               substr(coalesce(content_rendered_disclosure, content_rendered, content_disclosure, content), 1, 500) AS content_preview,
               coalesce(content_disclosure, content) AS content,
               event_at
        FROM ai_chat_messages
        """,
        [
            "record_id",
            "conversation_id",
            "sender_type",
            "source_id",
            "content_preview",
            "content",
            "event_at",
        ],
    ),
    "conversation_messages": (
        """
        SELECT message_id AS record_id, conversation_id, sender_id, source_id,
               substr(coalesce(content, ''), 1, 500) AS content_preview,
               content, created_at AS event_at
        FROM conversation_messages
        """,
        [
            "record_id",
            "conversation_id",
            "sender_id",
            "source_id",
            "content_preview",
            "content",
            "event_at",
        ],
    ),
    "conversation_messages_disclosure": (
        """
        SELECT message_id AS record_id, conversation_id, sender_id, source_id,
               substr(coalesce(content_disclosure, content, ''), 1, 500) AS content_preview,
               coalesce(content_disclosure, content) AS content,
               created_at AS event_at
        FROM conversation_messages
        """,
        [
            "record_id",
            "conversation_id",
            "sender_id",
            "source_id",
            "content_preview",
            "content",
            "event_at",
        ],
    ),
    "activity_events": (
        """
        SELECT event_id AS record_id, source_id,
               substr(coalesce(title, ''), 1, 500) AS content_preview,
               coalesce(title, '') AS content, occurred_at AS event_at
        FROM activity_events
        """,
        ["record_id", "source_id", "content_preview", "content", "event_at"],
    ),
    "financial_transactions": (
        """
        SELECT transaction_id AS record_id, source_id, account_type, amount, category, currency,
               substr(coalesce(description, ''), 1, 500) AS content_preview,
               coalesce(description, '') AS content, posted_at AS event_at
        FROM financial_transactions
        """,
        [
            "record_id",
            "source_id",
            "account_type",
            "amount",
            "category",
            "currency",
            "content_preview",
            "content",
            "event_at",
        ],
    ),
    "location_events": (
        """
        SELECT event_id AS record_id, source_id, place_name, city, region, country, event_type,
               substr(coalesce(place_name, city, ''), 1, 500) AS content_preview,
               coalesce(place_name, city, '') AS content, event_at
        FROM location_events
        """,
        [
            "record_id",
            "source_id",
            "place_name",
            "city",
            "region",
            "country",
            "event_type",
            "content_preview",
            "content",
            "event_at",
        ],
    ),
    "profile_records": (
        """
        SELECT record_id, record_type, title, organization, description, source_id,
               substr(coalesce(description, title, ''), 1, 500) AS content_preview,
               coalesce(description, title, '') AS content
        FROM profile_records
        """,
        [
            "record_id",
            "record_type",
            "title",
            "organization",
            "description",
            "source_id",
            "content_preview",
            "content",
        ],
    ),
    "calendar_events": (
        """
        SELECT event_id AS record_id, title, starts_at, ends_at, source_id, metadata_json,
               substr(coalesce(title, ''), 1, 500) AS content_preview,
               coalesce(title, '') AS content
        FROM calendar_events
        """,
        [
            "record_id",
            "title",
            "starts_at",
            "ends_at",
            "source_id",
            "metadata_json",
            "content_preview",
            "content",
        ],
    ),
    "journal_entries": (
        """
        SELECT entry_id AS record_id, entry_at, starts_at, ends_at, mood_tag, category, content, people, place_name, source_id,
               substr(coalesce(content, ''), 1, 500) AS content_preview,
               entry_at AS event_at
        FROM journal_entries
        """,
        [
            "record_id",
            "entry_at",
            "starts_at",
            "ends_at",
            "mood_tag",
            "category",
            "content",
            "people",
            "place_name",
            "source_id",
            "content_preview",
            "event_at",
        ],
    ),
    "journal_entries_disclosure": (
        """
        SELECT entry_id AS record_id, entry_at, mood_tag, category,
               coalesce(content_disclosure, content) AS content,
               source_id,
               substr(coalesce(content_disclosure, content, ''), 1, 500) AS content_preview,
               entry_at AS event_at
        FROM journal_entries
        """,
        [
            "record_id",
            "entry_at",
            "mood_tag",
            "category",
            "content",
            "source_id",
            "content_preview",
            "event_at",
        ],
    ),
    "contacts": (
        """
        SELECT contact_id AS record_id, display_name, source_id, is_self
        FROM contacts
        """,
        ["record_id", "display_name", "source_id", "is_self"],
    ),
    "contact_identifiers": (
        """
        SELECT contact_id AS record_id, identifier, identifier_type, source_id
        FROM contact_identifiers
        """,
        ["record_id", "identifier", "identifier_type", "source_id"],
    ),
}


_DISCLOSURE_LIST_TABLES = frozenset(
    {"ai_chat_messages", "conversation_messages", "journal_entries"}
)


def _native_list_spec_key(table: str, disclosure_tier: str) -> str:
    if disclosure_tier == "default_disclosure" and table in _DISCLOSURE_LIST_TABLES:
        disclosure_key = f"{table}_disclosure"
        if disclosure_key in _NATIVE_LIST_SPECS:
            return disclosure_key
    return table


class SQLiteCanonicalStore:
    """Adapter-facing canonical store — delegates MVP tables to typed canonical_store."""

    _NATIVE_TABLES = _NATIVE_TABLES

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._typed = TypedSQLiteCanonicalStore(conn)

    def _fetch_native_row(self, table: str, record_id: str) -> Optional[Dict[str, Any]]:
        id_col = _NATIVE_ID_COL.get(table)
        if not id_col:
            return None
        row = self._conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (record_id,)).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._conn.execute(f"PRAGMA table_info({table})").fetchall()]
        return dict(zip(cols, row))

    def _list_native(
        self,
        table: str,
        *,
        source_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        disclosure_tier: str = "owner_raw",
    ) -> ListPage:
        if table not in self._NATIVE_TABLES:
            return ListPage(items=[], total=0, offset=offset, limit=limit)

        spec_key = _native_list_spec_key(table, disclosure_tier)
        base, col_names = _NATIVE_LIST_SPECS[spec_key]
        params: List[Any] = []
        query = base
        if source_id is not None:
            query += " WHERE source_id=?"
            params.append(source_id)
        count_row = self._conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()
        total = int(count_row[0]) if count_row else 0
        query += " ORDER BY record_id LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(query, params).fetchall()
        items = [dict(zip(col_names, row)) for row in rows]
        return ListPage(items=items, total=total, offset=offset, limit=limit)

    def upsert(self, table: str, record: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> str:
        if table in self._NATIVE_TABLES:
            ref = self._typed.upsert(
                table,
                record,
                sync_batch_id=idempotency_key or record.get("sync_batch_id"),
            )
            return ref.record_id
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
        if table in self._NATIVE_TABLES:
            return self._fetch_native_row(table, record_id)
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
        disclosure_tier: str = "owner_raw",
    ) -> ListPage:
        if table in self._NATIVE_TABLES:
            return self._list_native(
                table,
                source_id=source_id,
                limit=limit,
                offset=offset,
                disclosure_tier=disclosure_tier,
            )

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
        id_col = _NATIVE_ID_COL.get(table)
        if id_col:
            cur = self._conn.execute(f"DELETE FROM {table} WHERE {id_col}=?", (record_id,))
        else:
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

    def _existing_embedding_id(
        self,
        record_id: Optional[str],
        model: Optional[str],
        chunk_index: int,
    ) -> Optional[str]:
        if not record_id or not model:
            return None
        row = self._conn.execute(
            """
            SELECT embedding_id FROM signal_embeddings
            WHERE record_id=? AND model=? AND chunk_index=?
            """,
            (record_id, model, chunk_index),
        ).fetchone()
        return str(row[0]) if row else None

    def get_embedding_hashes(
        self,
        record_id: str,
        model: str,
    ) -> Dict[int, str]:
        rows = self._conn.execute(
            """
            SELECT chunk_index, content_hash FROM signal_embeddings
            WHERE record_id=? AND model=? AND content_hash IS NOT NULL
            """,
            (record_id, model),
        ).fetchall()
        return {int(row[0]): str(row[1]) for row in rows if row[1]}

    def delete_chunks_for_record(
        self,
        record_id: str,
        model: str,
        *,
        keep_indices: Optional[List[int]] = None,
    ) -> int:
        from .vector_search import delete_vec_rows

        if keep_indices is not None:
            placeholders = ",".join("?" for _ in keep_indices)
            ids = [
                str(row[0])
                for row in self._conn.execute(
                    f"""
                    SELECT embedding_id FROM signal_embeddings
                    WHERE record_id=? AND model=? AND chunk_index NOT IN ({placeholders})
                    """,
                    (record_id, model, *keep_indices),
                ).fetchall()
            ]
            cur = self._conn.execute(
                f"""
                DELETE FROM signal_embeddings
                WHERE record_id=? AND model=? AND chunk_index NOT IN ({placeholders})
                """,
                (record_id, model, *keep_indices),
            )
        else:
            ids = [
                str(row[0])
                for row in self._conn.execute(
                    "SELECT embedding_id FROM signal_embeddings WHERE record_id=? AND model=?",
                    (record_id, model),
                ).fetchall()
            ]
            cur = self._conn.execute(
                "DELETE FROM signal_embeddings WHERE record_id=? AND model=?",
                (record_id, model),
            )
        delete_vec_rows(self._conn, ids)
        self._conn.commit()
        return cur.rowcount

    def upsert(self, metadata: Dict[str, Any], *, vector: Optional[List[float]] = None) -> str:
        from ....features.signal.vector_codec import encode_vector
        from ....features.signal.vector_settings import vector_format
        from .vector_search import sync_vec_row

        record_id = metadata.get("record_id")
        model = metadata.get("model")
        chunk_index = int(metadata.get("chunk_index") or 0)
        existing_id = self._existing_embedding_id(record_id, model, chunk_index)
        embedding_id = str(metadata.get("embedding_id") or existing_id or uuid.uuid4())
        fmt = vector_format()
        vector_blob = encode_vector(vector, fmt) if vector is not None else None
        search_text = metadata.get("search_text") or metadata.get("text_preview")
        provenance = {**metadata, "embedding_id": embedding_id}

        self._conn.execute(
            """
            INSERT INTO signal_embeddings (
                embedding_id, record_id, source_id, signal_dimension, model, provider,
                dims, text_preview, provenance_json, vector_blob, vector_format,
                content_hash, chunk_index, event_at, conversation_id, record_type, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id, model, chunk_index) DO UPDATE SET
                embedding_id=excluded.embedding_id,
                source_id=excluded.source_id,
                signal_dimension=excluded.signal_dimension,
                provider=excluded.provider,
                dims=excluded.dims,
                text_preview=excluded.text_preview,
                provenance_json=excluded.provenance_json,
                vector_blob=excluded.vector_blob,
                vector_format=excluded.vector_format,
                content_hash=excluded.content_hash,
                event_at=excluded.event_at,
                conversation_id=excluded.conversation_id,
                record_type=excluded.record_type,
                search_text=excluded.search_text
            """,
            (
                embedding_id,
                record_id,
                metadata.get("source_id"),
                metadata.get("signal_dimension"),
                model,
                metadata.get("provider"),
                metadata.get("dims"),
                metadata.get("text_preview"),
                json.dumps(provenance),
                vector_blob,
                fmt if vector is not None else metadata.get("vector_format", "json"),
                metadata.get("content_hash"),
                chunk_index,
                metadata.get("event_at"),
                metadata.get("conversation_id"),
                metadata.get("record_type"),
                search_text,
            ),
        )
        if vector is not None:
            sync_vec_row(self._conn, embedding_id=embedding_id, vector=[float(x) for x in vector])
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
    ) -> ListPage:
        from .vector_search import search_similar as run_search

        limit = max(1, min(int(limit), 100))
        items, total = run_search(
            self._conn,
            query_vector,
            source_id=source_id,
            dimension=dimension,
            model=model,
            event_after=event_after,
            event_before=event_before,
            limit=limit,
            fetch_limit=fetch_limit,
        )
        return ListPage(items=items, total=total, offset=0, limit=limit)

    def delete_by_record(self, record_id: str) -> int:
        from .vector_search import delete_vec_rows

        ids = [
            str(row[0])
            for row in self._conn.execute(
                "SELECT embedding_id FROM signal_embeddings WHERE record_id=?",
                (record_id,),
            ).fetchall()
        ]
        cur = self._conn.execute("DELETE FROM signal_embeddings WHERE record_id=?", (record_id,))
        delete_vec_rows(self._conn, ids)
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
