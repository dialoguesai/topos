"""openCypher over the entity graph — OWNER-ONLY (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §4).

Every multi-hop traversal used to be new Python. This is the one query surface
that replaces them: `entities` + ACTIVE `entity_edges` are materialised into a
NetworkX graph and queried with openCypher via ``grandcypher``.

Why a full rebuild per query and no cache
-----------------------------------------
~3.2k nodes / ~3k edges. The rebuild is cheaper than the traversal that follows
it, and a cache would have to be invalidated by every enrichment pass that
touches an edge. Nothing here is stateful.

Graph shape
-----------
The same facts :func:`~topos.features.entities.edges.graph_snapshot` gives the
graph UI, under their storage names, so the two views cannot disagree about
what an edge is.

* Nodes are ``entity_id``, labelled with ``entity_type`` (``:person``, ``:org``,
  ...), carrying ``canonical_name``, ``entity_type``, ``mention_count``,
  ``is_self``, ``first_seen``, ``last_seen``.
* Edges are labelled with ``edge_type`` (``:co_occurrence``,
  ``:semantic_affinity``, ...), carrying ``edge_type``, ``weight``,
  ``evidence_count``, ``last_event_at``, ``valid_from``. Only ACTIVE edges
  (``valid_to IS NULL``) are materialised.

**Direction.** The graph is a ``MultiDiGraph``. Symmetric edge types
(``co_occurrence``, ``communicates_with``, ``semantic_affinity`` — see
``edges._SYMMETRIC_EDGE_TYPES``) are inserted in BOTH directions, because
storage keeps one canonically-ordered row per symmetric pair and a traversal
must not depend on which endpoint sorted first. Directed types (``part_of``,
``discusses``, ``located_at``) keep their direction. Always write patterns with
an arrow (``-[]->``); grandcypher's undirected ``-[]-`` form matches nothing.

Worked examples
---------------
Note: string literals use DOUBLE quotes and equality is ``=`` or ``==``.

1. Who does the owner co-occur with most?

   .. code-block:: cypher

      MATCH (me)-[r:co_occurrence]->(p:person)
      WHERE me.is_self == True
      RETURN p.canonical_name, r.weight, r.evidence_count

2. Two hops out from a named person, through any edge type:

   .. code-block:: cypher

      MATCH (a)-[]->(b)-[]->(c)
      WHERE a.canonical_name == "Sarah Chen"
      RETURN a.canonical_name, b.canonical_name, c.canonical_name

3. **Latent affinity — the queries that were unreachable without new Python.**
   ``semantic_affinity`` edges (§3.2) connect people who never co-occur but
   occupy the same role-shape. A plain listing:

   .. code-block:: cypher

      MATCH (a:person)-[r:semantic_affinity]->(b:person)
      WHERE r.weight > 0.5
      RETURN a.canonical_name, b.canonical_name, r.weight

4. Affinity as a bridge: someone I actually co-occur with, who is latently
   similar to someone I have never been in a room with.

   .. code-block:: cypher

      MATCH (me)-[:co_occurrence]->(known:person)-[aff:semantic_affinity]->(unknown:person)
      WHERE me.is_self == True
      RETURN known.canonical_name, unknown.canonical_name, aff.weight

5. Variable-length reachability (bounded — always bound it):

   .. code-block:: cypher

      MATCH (a:person)-[r*1..3]->(b:org)
      WHERE a.canonical_name == "Sarah Chen"
      RETURN a.canonical_name, b.canonical_name

Disclosure
----------
An arbitrary traversal punches straight through row-level redaction: a
``MATCH (a)-[*1..3]->(b)`` does not respect a scope unless the subgraph was
scoped BEFORE the query ran. Until a scoped-subgraph materialisation story
exists, the only handler for this is registered
``@handles("graph_cypher_query", owner_only=True)`` and the §4.1 leak gate in
``tests/test_protocol_handled_types.py`` keeps it off ``/mcp`` and off the
control plane's forwarding set.

Result rows still pass through the engine's existing vector gate
(``topos.query.retrieval._strip_vector_keys``) on the way out, so a
``RETURN a`` can never carry ``embedding_blob``/``centroid_blob`` out of the
engine even if the materialiser starts projecting them.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Tuple

from .edges import _SYMMETRIC_EDGE_TYPES

logger = logging.getLogger(__name__)

#: Rows returned when the caller does not ask for a specific number.
DEFAULT_ROW_LIMIT = 200
#: Ceiling on rows returned, whatever the caller asks for.
MAX_ROW_LIMIT = 2000
#: Longest accepted query text. A parse is Earley; unbounded input is a footgun.
MAX_QUERY_CHARS = 4000


class CypherQueryError(ValueError):
    """A query that could not be parsed or executed. Never a crash, always this."""


def build_entity_graph(conn: sqlite3.Connection, *, min_weight: float = 0.0):
    """Materialise entities + ACTIVE entity_edges into a NetworkX MultiDiGraph.

    Full rebuild, no cache — see the module docstring.

    Node and edge attributes carry the same facts
    :func:`~topos.features.entities.edges.graph_snapshot` exposes to the graph
    UI, under their storage names (``canonical_name`` rather than the UI's
    ``label``). Two differences from that function, both deliberate:

    * every entity becomes a node, including entities with no active edge —
      "who is in the graph but connected to nothing" is a legitimate query;
    * no ``LIMIT``, because a traversal over a paged subgraph answers a
      different question than the one asked.
    """
    import networkx as nx

    graph = nx.MultiDiGraph()
    for entity_id, name, entity_type, mentions, first_seen, last_seen, is_self in conn.execute(
        """
        SELECT entity_id, canonical_name, entity_type, mention_count,
               first_seen, last_seen, is_self
        FROM entities
        """
    ):
        etype = entity_type or "unknown"
        graph.add_node(
            entity_id,
            __labels__={etype},
            entity_id=entity_id,
            entity_type=etype,
            canonical_name=name,
            mention_count=int(mentions or 0),
            first_seen=first_seen,
            last_seen=last_seen,
            is_self=bool(is_self),
        )

    for src, dst, edge_type, weight, evidence, last_at, valid_from in conn.execute(
        """
        SELECT src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
               last_event_at, valid_from
        FROM entity_edges WHERE valid_to IS NULL AND weight >= ?
        """,
        (min_weight,),
    ):
        etype = edge_type or "unknown"
        attrs = {
            "__labels__": {etype},
            "edge_type": etype,
            "weight": float(weight or 0.0),
            "evidence_count": int(evidence or 0),
            "last_event_at": last_at,
            "valid_from": valid_from,
        }
        if src not in graph or dst not in graph:
            continue  # dangling edge; the entity row is gone
        graph.add_edge(src, dst, **attrs)
        # Symmetric facts are stored once, in canonical (sorted) order. Insert the
        # mirror so a traversal is not silently one-directional for half the pairs.
        if etype in _SYMMETRIC_EDGE_TYPES and src != dst:
            graph.add_edge(dst, src, **dict(attrs))

    return graph


def run_cypher(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    min_weight: float = 0.0,
) -> Dict[str, Any]:
    """Run an openCypher query over the freshly materialised entity graph.

    Returns ``{"columns", "rows", "row_count", "truncated", "graph"}``. Raises
    :class:`CypherQueryError` — and nothing else — on a malformed query.
    """
    text = (query or "").strip()
    if not text:
        raise CypherQueryError("query is required")
    if len(text) > MAX_QUERY_CHARS:
        raise CypherQueryError(f"query exceeds {MAX_QUERY_CHARS} characters")

    row_limit = max(1, min(int(limit or DEFAULT_ROW_LIMIT), MAX_ROW_LIMIT))
    graph = build_entity_graph(conn, min_weight=min_weight)

    try:
        from grandcypher import GrandCypher

        raw = GrandCypher(graph).run(text)
    except Exception as exc:  # noqa: BLE001 - parser/matcher errors are user errors here
        logger.debug("cypher query rejected: %s", exc)
        raise CypherQueryError(_error_text(exc)) from exc

    columns, rows = _rows_from_columnar(raw)
    truncated = len(rows) > row_limit
    return {
        "columns": columns,
        "rows": rows[:row_limit],
        "row_count": min(len(rows), row_limit),
        "truncated": truncated,
        "graph": {
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
        },
    }


# --- internals ---------------------------------------------------------------


def _error_text(exc: Exception) -> str:
    """First line of a parser error — lark echoes the whole query otherwise."""
    text = str(exc).strip().splitlines()
    head = text[0] if text else exc.__class__.__name__
    return f"{exc.__class__.__name__}: {head}"[:300]


def _vector_shaped(key: str) -> bool:
    """True when the engine's cross-user vector gate would drop this key.

    Delegates to ``_strip_vector_keys`` rather than restating its patterns, so
    a key added to that gate is covered here on the same commit.
    """
    from ...query.retrieval import _strip_vector_keys

    return not _strip_vector_keys({key: None})


def _json_safe(value: Any) -> Any:
    """Flatten grandcypher's return values into JSON-serialisable, redacted data.

    Edge property lookups come back keyed by ``(edge_index, label)`` because the
    graph is a multigraph; whole-entity returns come back as attribute dicts
    carrying a ``__labels__`` set. Neither is JSON.
    """
    if isinstance(value, dict):
        keys = list(value.keys())
        if keys and not all(isinstance(k, str) for k in keys):
            # Multi-edge bucket: one entry per parallel edge.
            flattened = [_json_safe(value[k]) for k in keys]
            return flattened[0] if len(flattened) == 1 else flattened
        cleaned = {
            str(k): _json_safe(v)
            for k, v in value.items()
            if not _vector_shaped(str(k))
        }
        labels = cleaned.pop("__labels__", None)
        if labels is not None:
            cleaned["labels"] = labels
        return cleaned
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _rows_from_columnar(raw: Any) -> Tuple[List[str], List[Dict[str, Any]]]:
    """grandcypher returns column -> [values]; the engine speaks rows."""
    if not isinstance(raw, dict) or not raw:
        return [], []

    columns: List[str] = []
    series: List[List[Any]] = []
    for key, values in raw.items():
        # RETURN a.embedding_blob must not become a column at all — the same gate
        # that redacts a whole-node dict applies to the projected property name.
        name = str(key)
        if _vector_shaped(name.rsplit(".", 1)[-1]):
            continue
        columns.append(name)
        series.append(list(values) if isinstance(values, (list, tuple)) else [values])

    height = max((len(col) for col in series), default=0)
    rows: List[Dict[str, Any]] = []
    for index in range(height):
        rows.append(
            {
                name: _json_safe(col[index]) if index < len(col) else None
                for name, col in zip(columns, series)
            }
        )
    return columns, rows


__all__ = [
    "CypherQueryError",
    "DEFAULT_ROW_LIMIT",
    "MAX_QUERY_CHARS",
    "MAX_ROW_LIMIT",
    "build_entity_graph",
    "run_cypher",
]
