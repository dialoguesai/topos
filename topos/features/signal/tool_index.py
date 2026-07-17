"""Tool-RAG substrate: embed MCP tool specs, retrieve top-K for a query.

PLAN_AGENT_QUALITY WS-3 (M2). The gateway's tool count grows per granted remote
connector (GitHub alone adds 27); a flat tool doc overflows the orchestration
budget and makes models hallucinate tool names. This module keeps a small
embedded index of tool specs on the node so the chat orchestration can show the
model only the ~K relevant tools for a query, regardless of global tool count.

Design notes
- Tool counts are tiny (tens to low hundreds), so retrieval is a brute-force
  cosine scan over a dedicated ``tool_index`` table. We deliberately do NOT
  reuse ``signal_embeddings``/``signal_embeddings_vec``: mixing tool rows into
  the personal-signal ANN index would pollute its top-N windows, and tool specs
  are config-like metadata, not personal signal (GC/decay must never prune them).
- Embeddings ride the same stack as signal search: ``HuggingFaceAdapter`` with
  ``active_embedding_model()``, ``input_role="passage"`` for tool docs and
  ``"query"`` for queries (arctic-embed convention), and the request/session
  query-embed cache.
- Indexing is hash-deduped (sha256 of model + embed text), so re-syncing the
  same tool list is a cheap no-op — callers can push the full list after every
  ``tools/list`` / connector discover without bookkeeping.
- Retrieval NEVER hard-fails on embedding availability: when the embedder is
  missing (slim installs) it degrades to keyword token-overlap scoring, and the
  response always carries CORE_TOOL_NAMES so a retrieval miss can't strand the
  model (safety rail from the plan).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from .vector_codec import decode_vector, encode_f32, similarity

logger = logging.getLogger("topos.features.signal.tool_index")

_TABLE = "tool_index"

# Always returned by retrieve_tools regardless of similarity: query_scope covers
# synced-history questions, the introspection pair covers "what is this
# connector / what can I do" — so even a total retrieval miss leaves the model
# with a workable move.
CORE_TOOL_NAMES: tuple[str, ...] = ("query_scope", "describe_connector", "list_connectors")

# Identity-shaped bare tool names that ride along in scoped retrievals
# (PLAN_HELP_NUDGE A2). Identity tools never rank for task-verb queries ("what
# were my commits" embeds nothing profile-shaped — get_me sat 24/27 by cosine
# on the live GitHub index) yet they are the prerequisite for any first-person
# question: without the login the model fills in a placeholder username and
# confidently reports empty results.
IDENTITY_TOOL_NAMES: frozenset[str] = frozenset(
    {"get_me", "whoami", "me", "get_current_user", "get_authenticated_user", "about_me"}
)

# Wire-name shape for remote connector tools: remote__{connector}__{tool}.
_REMOTE_WIRE_RE = re.compile(r"^remote__(?P<connector>[^_].*?)__(?P<tool>.+)$")

# texts, input_role ("passage" | "query") -> vectors. Injectable for tests.
EmbedFn = Callable[[List[str], str], List[List[float]]]


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            tool_name TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            text_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            dims INTEGER NOT NULL,
            vector_blob BLOB NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def connector_of(tool_name: str, explicit: Optional[str] = None) -> str:
    """Connector id for a wire tool name ('' for base tools)."""
    if explicit:
        return str(explicit).strip()
    match = _REMOTE_WIRE_RE.match(str(tool_name or ""))
    return match.group("connector") if match else ""


def _bare_name(tool_name: str) -> str:
    match = _REMOTE_WIRE_RE.match(str(tool_name or ""))
    return match.group("tool") if match else str(tool_name or "")


def embed_text_for_tool(name: str, description: str, connector_id: str) -> str:
    """Compact embed text: connector + bare name + trimmed description."""
    desc = " ".join(str(description or "").split())[:400]
    parts = [p for p in (connector_id, _bare_name(name)) if p]
    head = " ".join(parts) or str(name or "")
    return f"{head}: {desc}" if desc else head


def _default_embed_fn(texts: List[str], input_role: str) -> List[List[float]]:
    from ...engine.backends.huggingface import HuggingFaceAdapter, active_embedding_model

    model = active_embedding_model()
    adapter = HuggingFaceAdapter()
    out: List[List[float]] = []
    # The adapter batches internally per call; tool lists are small enough that
    # one call per sync is fine.
    result = adapter.run_inference(
        {"texts": texts},
        {"subtype": "embedding", "model": model, "input_role": input_role, "batch_size": 32},
    )
    vectors = result.get("vectors") or []
    for vec in vectors:
        out.append([float(x) for x in vec])
    return out


def active_model_label() -> str:
    try:
        from ...engine.backends.huggingface import active_embedding_model

        return str(active_embedding_model())
    except Exception:  # noqa: BLE001 - slim installs have no HF backend
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}|{text}".encode("utf-8")).hexdigest()


def index_tools(
    conn: sqlite3.Connection,
    tools: Sequence[Dict[str, Any]],
    *,
    embed_fn: Optional[EmbedFn] = None,
    prune_missing: bool = False,
) -> Dict[str, Any]:
    """Upsert embeddings for the given tool specs; hash-deduped per (model, text).

    ``tools``: [{"name": str, "description": str?, "connector_id": str?}, ...]
    Returns counts: {"indexed", "unchanged", "pruned", "total", "model"}.
    """
    ensure_table(conn)
    model = active_model_label() if embed_fn is None else "injected"
    embed = embed_fn or _default_embed_fn

    seen_names: List[str] = []
    pending: List[tuple[str, str, str, str, str]] = []  # (name, connector, desc, text, hash)
    unchanged = 0
    existing_hashes: Dict[str, str] = {
        str(row[0]): str(row[1])
        for row in conn.execute(f"SELECT tool_name, text_hash FROM {_TABLE}").fetchall()
    }

    for spec in tools:
        name = str((spec or {}).get("name") or "").strip()
        if not name:
            continue
        connector = connector_of(name, (spec or {}).get("connector_id"))
        description = str((spec or {}).get("description") or "")
        text = embed_text_for_tool(name, description, connector)
        digest = _hash(model, text)
        seen_names.append(name)
        if existing_hashes.get(name) == digest:
            unchanged += 1
            continue
        pending.append((name, connector, description, text, digest))

    indexed = 0
    if pending:
        vectors = embed([text for _, _, _, text, _ in pending], "passage")
        now = _now_iso()
        for (name, connector, description, _text, digest), vector in zip(pending, vectors):
            if not vector:
                continue
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {_TABLE}
                    (tool_name, connector_id, description, text_hash, model, dims, vector_blob, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    connector,
                    description[:2000],
                    digest,
                    model,
                    len(vector),
                    encode_f32([float(x) for x in vector]),
                    now,
                ),
            )
            indexed += 1

    pruned = 0
    if prune_missing and seen_names:
        placeholders = ",".join("?" for _ in seen_names)
        cur = conn.execute(
            f"DELETE FROM {_TABLE} WHERE tool_name NOT IN ({placeholders})",
            seen_names,
        )
        pruned = cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

    conn.commit()
    total = int(conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0])
    return {
        "indexed": indexed,
        "unchanged": unchanged,
        "pruned": pruned,
        "total": total,
        "model": model,
    }


def _query_vector(query: str, *, embed_fn: Optional[EmbedFn]) -> Optional[List[float]]:
    q = str(query or "").strip()
    if not q:
        return None
    if embed_fn is not None:
        vectors = embed_fn([q], "query")
        return [float(x) for x in vectors[0]] if vectors else None
    try:
        from ...engine.backends.huggingface import HuggingFaceAdapter, active_embedding_model
        from .query_embed_cache import get_cached_query_embedding, set_cached_query_embedding

        model = active_embedding_model()
        cached = get_cached_query_embedding(q, model=model)
        if cached is not None:
            return cached
        adapter = HuggingFaceAdapter()
        result = adapter.run_inference(
            {"text": q}, {"subtype": "embedding", "model": model, "input_role": "query"}
        )
        vectors = result.get("vectors") or []
        if not vectors:
            return None
        vector = [float(x) for x in vectors[0]]
        set_cached_query_embedding(q, model=model, vector=vector)
        return vector
    except Exception as exc:  # noqa: BLE001 - degrade to keyword scoring, never 500
        logger.debug("tool_index query embedding unavailable: %s", exc)
        return None


_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _keyword_score(query: str, name: str, description: str) -> float:
    """Token-overlap fallback score in [0, 1] when no embedder is available."""
    q_tokens = set(_TOKEN_RE.findall(query.lower()))
    if not q_tokens:
        return 0.0
    doc_tokens = set(_TOKEN_RE.findall(f"{name.replace('_', ' ')} {description}".lower()))
    if not doc_tokens:
        return 0.0
    return len(q_tokens & doc_tokens) / len(q_tokens)


def retrieve_tools(
    conn: sqlite3.Connection,
    query: str,
    *,
    connector_scope: Optional[str] = None,
    k: int = 8,
    embed_fn: Optional[EmbedFn] = None,
    extra_identity_names: Sequence[str] = (),
) -> Dict[str, Any]:
    """Rank indexed tools for a query; always carries CORE_TOOL_NAMES.

    Returns {"tools": [{name, connector_id, description, similarity}], "core",
    "identity", "backend": "cosine" | "keyword_fallback", "model",
    "indexed_count", "k"}.

    When ``connector_scope`` is set, the scoped connector's identity-shaped
    tools (IDENTITY_TOOL_NAMES, plus ``extra_identity_names`` for connectors
    whose identity tool has a nonstandard bare name, e.g. GraphQL ``viewer``)
    always ride along regardless of similarity. Matched names are echoed in the
    ``identity`` field (empty when unscoped or nothing matched). Ride-alongs
    outside the top-k carry ``injected: True`` (``similarity: None`` when no
    score was computable, e.g. a dims-stale row) — so ``tools`` may exceed
    ``k`` and isn't strictly similarity-sorted at the tail.
    """
    ensure_table(conn)
    k = max(1, min(int(k or 8), 50))
    scope = str(connector_scope or "").strip()

    sql = f"SELECT tool_name, connector_id, description, model, vector_blob FROM {_TABLE}"
    params: List[Any] = []
    if scope:
        sql += " WHERE connector_id = ?"
        params.append(scope)
    rows = conn.execute(sql, params).fetchall()

    query_vector = _query_vector(query, embed_fn=embed_fn)
    scored: List[tuple[float, Dict[str, Any]]] = []
    backend = "cosine"
    if query_vector is not None:
        dims = len(query_vector)
        for name, connector, description, _model, blob in rows:
            try:
                stored = decode_vector(blob, "f32")
            except Exception:  # noqa: BLE001 - a bad row must not break retrieval
                continue
            if len(stored) != dims:
                continue  # stale rows from an older embedding model
            sim = similarity(query_vector, stored, normalized=True)
            scored.append(
                (sim, {"name": str(name), "connector_id": str(connector), "description": str(description)})
            )
    if query_vector is None or not scored:
        backend = "keyword_fallback"
        scored = [
            (
                _keyword_score(query, str(name), str(description)),
                {"name": str(name), "connector_id": str(connector), "description": str(description)},
            )
            for name, connector, description, _model, blob in rows
        ]

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = [{**meta, "similarity": round(score, 6)} for score, meta in scored[:k]]

    # Identity ride-along (PLAN_HELP_NUDGE A2): rows is already scope-filtered
    # (WHERE connector_id = ?), so matching here can never pull in another
    # connector's identity tool.
    identity: List[str] = []
    if scope:
        wanted = IDENTITY_TOOL_NAMES | {
            str(n).strip() for n in extra_identity_names if str(n).strip()
        }
        top_names = {t["name"] for t in top}
        scored_by_name = {meta["name"]: score for score, meta in scored}
        for name, connector, description, _model, _blob in rows:
            name = str(name)
            if _bare_name(name) not in wanted or name in identity:
                continue
            identity.append(name)
            if name in top_names:
                continue  # already ranked into the top-k on its own; no flag
            entry: Dict[str, Any] = {
                "name": name,
                "connector_id": str(connector),
                "description": str(description),
                # Keep the computed (low) score when the scan produced one; a
                # dims-stale/decode-skipped row gets None — don't invent one.
                "similarity": round(scored_by_name[name], 6) if name in scored_by_name else None,
                "injected": True,
            }
            top.append(entry)
        if not identity:
            logger.debug(
                "tool_index scoped retrieval found no identity tool to inject (scope=%s)",
                scope,
            )

    return {
        "tools": top,
        "core": list(CORE_TOOL_NAMES),
        "identity": identity,
        "backend": backend,
        "model": active_model_label() if embed_fn is None else "injected",
        "indexed_count": len(rows),
        "k": k,
    }


def index_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Cheap introspection for health checks / the eval harness."""
    ensure_table(conn)
    total = int(conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0])
    by_connector = {
        str(row[0] or ""): int(row[1])
        for row in conn.execute(
            f"SELECT connector_id, COUNT(*) FROM {_TABLE} GROUP BY connector_id"
        ).fetchall()
    }
    newest = conn.execute(f"SELECT MAX(updated_at) FROM {_TABLE}").fetchone()[0]
    return {"total": total, "by_connector": by_connector, "updated_at": newest, "core": list(CORE_TOOL_NAMES)}
