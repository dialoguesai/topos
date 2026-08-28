"""Signal feature message handlers."""
from __future__ import annotations

import logging
import time

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    run_db_read,
    run_db_write,
)
from .registry import handles

logger = logging.getLogger("topos.core.handlers.signal_features")


@handles("signal_list_vectors")
async def handle_signal_list_vectors(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.list_vectors(
            limit=min(int(payload.get("limit") or 50), 500),
            offset=int(payload.get("offset") or 0),
            source_id=payload.get("source_id"),
            dimension=payload.get("dimension"),
            model=payload.get("model"),
            created_after=payload.get("created_after"),
            created_before=payload.get("created_before"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}

@handles("signal_search_vectors")
async def handle_signal_search_vectors(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.search_vectors(
            query=str(payload.get("q") or payload.get("query") or ""),
            limit=min(int(payload.get("limit") or 20), 100),
            source_id=payload.get("source_id"),
            dimension=payload.get("dimension"),
            model=payload.get("model"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}

@handles("signal_vector_source_text")
async def handle_signal_vector_source_text(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.get_vector_source_text(record_id=str(payload.get("record_id") or ""))
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}

@handles("signal_list_graph")
async def handle_signal_list_graph(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.list_graph(
            dimension=payload.get("dimension"),
            limit_nodes=min(int(payload.get("limit_nodes") or 200), 1000),
            limit_edges=min(int(payload.get("limit_edges") or 500), 2000),
            edge_type=payload.get("edge_type"),
            min_weight=payload.get("min_weight"),
            source_id=payload.get("source_id"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc), "code": 503}

@handles("signal_list_topic_clusters")
async def handle_signal_list_topic_clusters(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.lifecycle.blackhole_guard import guard_from_message
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.list_topic_clusters(
            guard=guard_from_message(conn, message),
            limit=min(int(payload.get("limit") or 50), 200),
            dimension=payload.get("dimension"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_list_topic_cluster_members")
async def handle_signal_list_topic_cluster_members(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    cluster_id = str(payload.get("cluster_id") or "").strip()
    if not cluster_id:
        return {"id": req_id, "status": "error", "error": "cluster_id required", "code": 400}
    try:
        from ...features.lifecycle.blackhole_guard import guard_from_message
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.list_topic_cluster_members(
            cluster_id,
            guard=guard_from_message(conn, message),
            limit=min(int(payload.get("limit") or 100), 500),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except LookupError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 404}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_list_dimensions")
async def handle_signal_list_dimensions(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.signal.service import get_signal_service

        return {"id": req_id, "status": "ok", "payload": get_signal_service(conn=hub.get_db_connection()).list_dimensions()}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_data_health")
async def handle_signal_data_health(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.signal.service import get_signal_service

        return {"id": req_id, "status": "ok", "payload": get_signal_service(conn=hub.get_db_connection()).get_data_health()}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_list_briefs")
async def handle_signal_list_briefs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).list_briefs(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_get_brief")
async def handle_signal_get_brief(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dimension = str(payload.get("dimension") or "").strip()
    if not dimension:
        return {"id": req_id, "status": "error", "error": "dimension required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).get_brief(dimension),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_list_brief_revisions")
async def handle_signal_list_brief_revisions(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dimension = str(payload.get("dimension") or "").strip()
    if not dimension:
        return {"id": req_id, "status": "error", "error": "dimension required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        limit = min(int(payload.get("limit") or 20), 100)
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).list_brief_revisions(dimension, limit=limit),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_update_brief")
async def handle_signal_update_brief(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dimension = str(payload.get("dimension") or "").strip()
    markdown_body = payload.get("markdown_body")
    if not dimension:
        return {"id": req_id, "status": "error", "error": "dimension required", "code": 400}
    if markdown_body is None:
        return {"id": req_id, "status": "error", "error": "markdown_body required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).update_brief(dimension, str(markdown_body)),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_refresh_brief")
async def handle_signal_refresh_brief(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dimension = str(payload.get("dimension") or "").strip()
    if not dimension:
        return {"id": req_id, "status": "error", "error": "dimension required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        limit = min(int(payload.get("limit") or 40), 100)
        service = get_signal_service(conn=conn)
        result = await service.refresh_brief(dimension, limit=limit)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_list_definitions")
async def handle_signal_list_definitions(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).list_definitions(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_get_definition")
async def handle_signal_get_definition(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dimension = str(payload.get("dimension") or "").strip()
    if not dimension:
        return {"id": req_id, "status": "error", "error": "dimension required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).get_definition(dimension),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 404}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_list_objects")
async def handle_signal_list_objects(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dimension = str(payload.get("dimension") or "").strip()
    if not dimension:
        return {"id": req_id, "status": "error", "error": "dimension required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        limit = min(int(payload.get("limit") or 50), 200)
        object_type = payload.get("object_type")
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).list_signal_objects(
                dimension,
                object_type=str(object_type).strip() if object_type else None,
                limit=limit,
            ),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_get_object")
async def handle_signal_get_object(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    object_id = str(payload.get("object_id") or "").strip()
    if not object_id:
        return {"id": req_id, "status": "error", "error": "object_id required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).get_signal_object(object_id),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 404}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_owner_override_object")
async def handle_signal_owner_override_object(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    object_id = str(payload.get("object_id") or "").strip()
    patch = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    if not object_id:
        return {"id": req_id, "status": "error", "error": "object_id required", "code": 400}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).owner_override_signal_object(object_id, patch),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 404}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("signal_evaluate_fit")
async def handle_signal_evaluate_fit(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    opportunity_type = str(payload.get("opportunity_type") or "").strip()
    if not opportunity_type:
        return {"id": req_id, "status": "error", "error": "opportunity_type required", "code": 400}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    try:
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": get_signal_service(conn=conn).evaluate_fit(opportunity_type, context=context),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


# --- dense-intelligence reads (P13: CP proxy parity with /v1/signal HTTP) ---


@handles("signal_list_entities")
async def handle_signal_list_entities(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.entities.reads import list_entities
        from ...features.lifecycle.blackhole_guard import guard_from_message

        # Argument coercion stays on the loop (pure, and a bad value should fail
        # the request before a worker is spent); the guard is built INSIDE the
        # worker because it holds the connection it was constructed with and
        # queries it lazily.
        q = payload.get("q")
        entity_type = payload.get("entity_type")
        contacts_only = bool(payload.get("contacts_only"))
        limit = min(int(payload.get("limit") or 50), 200)
        offset = max(0, int(payload.get("offset") or 0))

        def _read(conn):
            return list_entities(
                conn,
                guard=guard_from_message(conn, message),
                q=q,
                entity_type=entity_type,
                contacts_only=contacts_only,
                limit=limit,
                offset=offset,
            )

        result = await run_db_read(_read)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_get_entity")
async def handle_signal_get_entity(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    entity_id = str(payload.get("entity_id") or "").strip()
    if not entity_id:
        return {"id": req_id, "status": "error", "error": "entity_id required", "code": 400}
    try:
        from ...features.entities.reads import get_entity_detail
        from ...features.lifecycle.blackhole_guard import guard_from_message

        def _read(conn):
            # Guard built here, not on the loop: it retains this connection.
            return get_entity_detail(conn, entity_id, guard=guard_from_message(conn, message))

        detail = await run_db_read(_read)
        # Same 404 a never-stored id gets — the protected case must not be
        # distinguishable by status code or message (D5).
        if detail is None:
            return {"id": req_id, "status": "error", "error": "entity not found", "code": 404}
        return {"id": req_id, "status": "ok", "payload": detail}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_entity_graph")
async def handle_signal_entity_graph(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.entities.reads import entity_graph
        from ...features.lifecycle.blackhole_guard import guard_from_message

        limit_nodes = min(int(payload.get("limit_nodes") or 100), 5000)
        limit_edges = min(int(payload.get("limit_edges") or 300), 20000)
        min_weight = float(payload.get("min_weight") or 0.0)
        include_closed = bool(payload.get("include_closed"))
        as_of = payload.get("as_of") or None
        selection = str(payload.get("selection") or "weight")
        offset = max(0, int(payload.get("offset") or 0))

        def _read(conn):
            # Guard built here, not on the loop: it retains this connection.
            return entity_graph(
                conn,
                guard=guard_from_message(conn, message),
                limit_nodes=limit_nodes,
                limit_edges=limit_edges,
                min_weight=min_weight,
                include_closed=include_closed,
                as_of=as_of,
                selection=selection,
                offset=offset,
            )

        result = await run_db_read(_read)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_entity_graph_search")
async def handle_signal_entity_graph_search(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        import asyncio

        from ...features.entities.graph_search import graph_search
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service()

        def _run():
            def search_fn(*, query, limit, event_after=None, event_before=None):
                return service.search_vectors(
                    query=query,
                    limit=limit,
                    mode="hybrid",
                    event_after=event_after,
                    event_before=event_before,
                )

            return graph_search(
                conn,
                query=str(payload.get("q") or ""),
                search_fn=search_fn,
                limit_records=min(int(payload.get("limit_records") or 40), 100),
                limit_entities=min(int(payload.get("limit_entities") or 30), 100),
                event_after=payload.get("event_after") or None,
                event_before=payload.get("event_before") or None,
            )

        result = await asyncio.to_thread(_run)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_list_facts")
async def handle_signal_list_facts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.facts.reads import list_facts
        from ...features.lifecycle.blackhole_guard import guard_from_message

        conn = hub.get_db_connection()
        result = list_facts(
            conn,
            guard=guard_from_message(conn, message),
            predicate=payload.get("predicate"),
            dimension=payload.get("dimension"),
            pack=payload.get("pack"),
            include_closed=bool(payload.get("include_closed")),
            limit=min(int(payload.get("limit") or 100), 500),
            offset=max(0, int(payload.get("offset") or 0)),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("put_fact_verdict")
async def handle_put_fact_verdict(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Owner verdict on one fact: confirm / reject / edit (value or attribution)."""
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.facts.verdicts import apply_fact_verdict

        conn = hub.get_db_connection()
        result = apply_fact_verdict(
            conn,
            object_id=str(payload.get("object_id") or ""),
            action=str(payload.get("action") or ""),
            object_value=payload.get("object_value"),
            asserted_by=payload.get("asserted_by"),
            note=payload.get("note"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except (LookupError, ValueError) as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_list_insights")
async def handle_signal_list_insights(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.facts.reads import list_stat_insights
        from ...features.lifecycle.blackhole_guard import guard_from_message

        conn = hub.get_db_connection()
        result = list_stat_insights(
            conn,
            guard=guard_from_message(conn, message),
            dimension=payload.get("dimension"),
            limit=min(int(payload.get("limit") or 200), 500),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_list_timeline")
async def handle_signal_list_timeline(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.facts.reads import list_timeline

        conn = hub.get_db_connection()
        result = list_timeline(
            conn,
            canonical_table=payload.get("canonical_table"),
            source_id=payload.get("source_id"),
            date_from=payload.get("date_from"),
            date_to=payload.get("date_to"),
            limit=min(int(payload.get("limit") or 100), 500),
            offset=max(0, int(payload.get("offset") or 0)),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_list_entity_review")
async def handle_signal_list_entity_review(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.entities.consolidation import count_review, list_review

        conn = hub.get_db_connection()
        status = str(payload.get("status") or "pending")
        items = list_review(
            conn,
            status=status,
            limit=min(int(payload.get("limit") or 100), 500),
        )
        total = count_review(conn, status=status)
        return {"id": req_id, "status": "ok", "payload": {"items": items, "total": total}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_entity_review_sweep")
async def handle_signal_entity_review_sweep(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.entities.consolidation import propose_merges

        conn = hub.get_db_connection()
        return {"id": req_id, "status": "ok", "payload": propose_merges(conn)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_entity_review_action")
async def handle_signal_entity_review_action(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    review_id = str(payload.get("review_id") or "").strip()
    action = str(payload.get("action") or "").strip()
    if not review_id or action not in ("approve", "dismiss"):
        return {"id": req_id, "status": "error", "error": "review_id and action (approve|dismiss) required", "code": 400}
    try:
        from ...features.entities.consolidation import resolve_review

        started = time.monotonic()
        result = await run_db_write(resolve_review, review_id, action=action)
        logger.info(
            "signal_entity_review_action ok review_id=%s action=%s elapsed_ms=%.0f",
            review_id,
            action,
            (time.monotonic() - started) * 1000,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except LookupError as exc:
        logger.warning(
            "signal_entity_review_action not_found review_id=%s action=%s error=%s",
            review_id,
            action,
            exc,
        )
        return {"id": req_id, "status": "error", "error": str(exc), "code": 404}
    except ValueError as exc:
        logger.warning(
            "signal_entity_review_action rejected review_id=%s action=%s error=%s",
            review_id,
            action,
            exc,
        )
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "signal_entity_review_action failed review_id=%s action=%s", review_id, action
        )
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_affinity_status")
async def handle_signal_affinity_status(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.entities.affinity_owner import get_affinity_status

        conn = hub.get_db_connection()
        return {"id": req_id, "status": "ok", "payload": get_affinity_status(conn)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_affinity_config")
async def handle_signal_affinity_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    percentile = payload.get("percentile")
    nudge = payload.get("nudge")
    if percentile is None and nudge is None:
        return {
            "id": req_id,
            "status": "error",
            "error": "provide percentile or nudge (fewer|more|ok)",
            "code": 400,
        }
    try:
        from ...features.entities.affinity_owner import apply_affinity_config

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": apply_affinity_config(
                conn,
                percentile=float(percentile) if percentile is not None else None,
                nudge=str(nudge) if nudge is not None else None,  # type: ignore[arg-type]
            ),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_affinity_recompute")
async def handle_signal_affinity_recompute(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.entities.affinity_owner import recompute_affinity_now

        conn = hub.get_db_connection()
        return {"id": req_id, "status": "ok", "payload": recompute_affinity_now(conn)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_affinity_pairs")
async def handle_signal_affinity_pairs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.entities.affinity_owner import list_affinity_pairs_for_review

        conn = hub.get_db_connection()
        return {
            "id": req_id,
            "status": "ok",
            "payload": list_affinity_pairs_for_review(
                conn, limit=min(int(payload.get("limit") or 50), 200)
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_affinity_label")
async def handle_signal_affinity_label(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    a = str(payload.get("a") or "").strip()
    b = str(payload.get("b") or "").strip()
    label = str(payload.get("label") or "").strip()
    if not a or not b or not label:
        return {"id": req_id, "status": "error", "error": "a, b, and label required", "code": 400}
    try:
        from ...features.entities.affinity_owner import label_affinity_pair

        conn = hub.get_db_connection()
        cosine = payload.get("cosine")
        return {
            "id": req_id,
            "status": "ok",
            "payload": label_affinity_pair(
                conn,
                entity_a=a,
                entity_b=b,
                label=label,
                note=payload.get("note"),
                cosine=float(cosine) if cosine is not None else None,
            ),
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_entity_split")
async def handle_signal_entity_split(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    entity_id = str(payload.get("entity_id") or "").strip()
    surface = str(payload.get("surface") or "").strip()
    if not entity_id or not surface:
        return {"id": req_id, "status": "error", "error": "entity_id and surface required", "code": 400}
    try:
        from ...features.entities.consolidation import split_surface

        started = time.monotonic()
        result = await run_db_write(split_surface, entity_id, surface)
        logger.info(
            "signal_entity_split ok entity_id=%s mentions_moved=%s elapsed_ms=%.0f",
            entity_id,
            result.get("mentions_moved") if isinstance(result, dict) else None,
            (time.monotonic() - started) * 1000,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except LookupError as exc:
        logger.warning(
            "signal_entity_split not_found entity_id=%s error=%s", entity_id, exc
        )
        return {"id": req_id, "status": "error", "error": str(exc), "code": 404}
    except ValueError as exc:
        logger.warning(
            "signal_entity_split rejected entity_id=%s error=%s", entity_id, exc
        )
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        logger.exception("signal_entity_split failed entity_id=%s", entity_id)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_entity_merge")
async def handle_signal_entity_merge(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    entity_id = str(payload.get("entity_id") or "").strip()
    absorb_entity_id = str(payload.get("absorb_entity_id") or "").strip()
    if not entity_id or not absorb_entity_id:
        return {
            "id": req_id,
            "status": "error",
            "error": "entity_id and absorb_entity_id required",
            "code": 400,
        }
    try:
        from ...features.entities.consolidation import merge_entity_pair

        # Off the event loop: merge takes the write gate, and holding it here
        # stalls every coroutine including the control-plane keepalive. Observed
        # 2026-08-28 as a red engine indicator + browser "Failed to fetch".
        started = time.monotonic()
        result = await run_db_write(merge_entity_pair, entity_id, absorb_entity_id)
        logger.info(
            "signal_entity_merge ok keep=%s absorb=%s mentions_moved=%s already_merged=%s elapsed_ms=%.0f",
            entity_id,
            absorb_entity_id,
            result.get("mentions_moved") if isinstance(result, dict) else None,
            result.get("already_merged") if isinstance(result, dict) else None,
            (time.monotonic() - started) * 1000,
        )
        return {
            "id": req_id,
            "status": "ok",
            "payload": result,
        }
    except LookupError as exc:
        logger.warning(
            "signal_entity_merge not_found keep=%s absorb=%s error=%s",
            entity_id,
            absorb_entity_id,
            exc,
        )
        return {"id": req_id, "status": "error", "error": str(exc), "code": 404}
    except ValueError as exc:
        logger.warning(
            "signal_entity_merge rejected keep=%s absorb=%s error=%s",
            entity_id,
            absorb_entity_id,
            exc,
        )
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "signal_entity_merge failed keep=%s absorb=%s", entity_id, absorb_entity_id
        )
        return {"id": req_id, "status": "error", "error": str(exc)}


def _blackhole_logger():
    import logging

    return logging.getLogger("topos.core.handlers.signal_features")


@handles("signal_blackhole_entity")
async def handle_signal_blackhole_entity(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Owner marks an entity off-limits, and the rebuild that makes it true runs.

    The rebuild is inline rather than deferred: the owner has just been told a
    rebuild is needed (D4 raises that notification first), and the artifacts it
    withdraws are withheld from everyone else until it finishes. Finishing here
    keeps that window as short as the work allows.
    """
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    entity_id = str(payload.get("entity_id") or "").strip()
    if not entity_id:
        return {"id": req_id, "status": "error", "error": "entity_id required", "code": 400}
    try:
        from ...features.lifecycle.blackhole import BlackholeStore
        from ...features.lifecycle.blackhole_rebuild import rebuild_for_blackhole

        processing_tier = str(payload.get("processing_tier") or "secure")
        note = payload.get("note")

        # Both of these WRITE. They ran on the loop / on a caller-passed
        # connection respectively, which is the pairing that corrupts a shared
        # handle's statement cache; each now gets the worker's own connection.
        def _blackhole(conn):
            return BlackholeStore(conn).blackhole_entity(
                entity_ref=entity_id,
                processing_tier=processing_tier,
                note=note,
            )

        result = await run_db_write(_blackhole)
        if not result.get("already_blackholed"):
            result["rebuild"] = (await run_db_write(rebuild_for_blackhole, entity_id)).as_dict()
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_unblackhole_entity")
async def handle_signal_unblackhole_entity(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Lift a black hole. Existing grants are not restored — normal rules resume."""
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    entity_id = str(payload.get("entity_id") or "").strip()
    if not entity_id:
        return {"id": req_id, "status": "error", "error": "entity_id required", "code": 400}
    try:
        from ...features.lifecycle.blackhole import BlackholeStore

        result = BlackholeStore(hub.get_db_connection()).unblackhole_entity(entity_ref=entity_id)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_list_blackholes")
async def handle_signal_list_blackholes(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The owner's own off-limits list — names included.

    Unlike `blackhole_status`, this one does name entities, because it exists to
    show the owner what they have protected. It is reachable only through the
    owner-only proxy route, never as an agent-callable tool.
    """
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(hub.get_db_connection())
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "blackholes": store.list(),
                "notifications": store.notifications(state="open"),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_dismiss_blackhole_notification")
async def handle_signal_dismiss_blackhole_notification(
    message: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    notification_id = str(payload.get("notification_id") or "").strip()
    if not notification_id:
        return {"id": req_id, "status": "error", "error": "notification_id required", "code": 400}
    try:
        from ...features.lifecycle.blackhole import BlackholeStore

        dismissed = BlackholeStore(hub.get_db_connection()).dismiss_notification(notification_id)
        return {"id": req_id, "status": "ok", "payload": {"dismissed": dismissed}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("blackhole_check_text")
async def handle_blackhole_check_text(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Does *this text* mention a protected entity, and what may process it?

    The per-turn counterpart to `blackhole_status`. Home chat assembles its
    prompt client-side, so the control plane cannot answer this itself — but it
    can ask the node, which holds the names. That keeps the constraint on the
    turns that actually warrant it instead of on every turn the user ever sends.

    Returns whether the text is protected and which providers may handle it —
    never which entity matched. The answer is enough to route on and useless as
    a lookup.

    Errors report protected with the strictest provider set: a node that cannot
    check must not be read as "this text is fine".
    """
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    text = str(payload.get("text") or "")
    try:
        from ...features.lifecycle.blackhole_llm import evaluate

        verdict = evaluate(hub.get_db_connection(), {"text": text}, provider="")
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "protected": bool(verdict.tainted),
                "allowed_providers": sorted(verdict.allowed_providers),
            },
        }
    except Exception as exc:  # noqa: BLE001
        _blackhole_logger().warning("blackhole_check_text failed, reporting protected: %s", exc)
        from ...features.lifecycle.blackhole import TIER_PROVIDERS

        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "protected": True,
                "allowed_providers": sorted(TIER_PROVIDERS["local_only"]),
                "degraded": True,
            },
        }


@handles("blackhole_status")
async def handle_blackhole_status(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Whether this topos protects anything — a boolean, never a name.

    The control plane needs this to decide model routing for home chat, where
    it receives an already-assembled prompt and so has no stamped structure to
    inspect. Deliberately the least informative answer that supports the
    decision: the control plane learns that protection exists, never who is
    protected, so this handler cannot become a disclosure channel of its own.

    Errors report `has_blackholes: true`. A caller that cannot read the flags
    must route as though protection existed rather than assume it did not.
    """
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(hub.get_db_connection())
        records = store.list()
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "has_blackholes": bool(records),
                "pending_rebuild": store.has_pending_rebuild(),
            },
        }
    except Exception as exc:  # noqa: BLE001
        _blackhole_logger().warning("blackhole_status failed, reporting protected: %s", exc)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"has_blackholes": True, "pending_rebuild": True, "degraded": True},
        }


@handles("signal_exclude_entity")
async def handle_signal_exclude_entity(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    entity_id = str(payload.get("entity_id") or "").strip()
    if not entity_id:
        return {"id": req_id, "status": "error", "error": "entity_id required", "code": 400}
    try:
        from ...features.lifecycle.exclusions import ExclusionStore

        conn = hub.get_db_connection()
        return {"id": req_id, "status": "ok", "payload": ExclusionStore(conn).exclude_entity(entity_ref=entity_id)}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_attention_dashboard")
async def handle_signal_attention_dashboard(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.triage.dashboard import attention_dashboard_data

        conn = hub.get_db_connection()
        result = attention_dashboard_data(
            conn,
            days=min(int(payload.get("days") or 14), 90),
            include_titles=bool(payload.get("include_titles", True)),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_set_worn_badge")
async def handle_signal_set_worn_badge(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.triage.badges import set_worn_badge, worn_badge

        conn = hub.get_db_connection()
        set_worn_badge(conn, payload.get("badge_id") or None)
        return {"id": req_id, "status": "ok", "payload": {"worn": worn_badge(conn)}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_pin_intent")
async def handle_signal_pin_intent(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.triage.intents import active_intents, pin_intent

        intent_text = str(payload.get("intent_text") or "").strip()
        if not intent_text:
            raise ValueError("intent_text is required")
        conn = hub.get_db_connection()
        pin_intent(
            conn,
            intent_text,
            horizon=payload.get("horizon") or "quarter",
            links=payload.get("links") or [],
        )
        return {"id": req_id, "status": "ok", "payload": {"intents": active_intents(conn)}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("signal_retire_intent")
async def handle_signal_retire_intent(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...features.triage.intents import active_intents, retire_intent

        conn = hub.get_db_connection()
        retire_intent(conn, payload.get("object_key") or "")
        return {"id": req_id, "status": "ok", "payload": {"intents": active_intents(conn)}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("put_community_name")
async def handle_put_community_name(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Owner rename for a graph community (PLAN_COMMUNITY_NAMING S3): retires
    any derived name for the community's core and records the owner's, which
    wins ties forever after. Stamps the label onto members immediately."""
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    community_id = payload.get("community_id")
    new_name = str(payload.get("name") or "").strip()
    try:
        from ...features.entities.community_names import core_fingerprint, rename_community
        from ...features.entities.community_naming import valid_label

        if not isinstance(community_id, int) or not valid_label(new_name):
            return {"id": req_id, "status": "error",
                    "error": "community_id (int) and a short name (2-4 words, letters) required"}
        conn = hub.get_db_connection()
        rows = conn.execute(
            "SELECT entity_id, json_extract(metadata_json,'$.centrality.eigen')"
            " FROM entities WHERE json_extract(metadata_json,'$.community_id')=?",
            (community_id,),
        ).fetchall()
        if not rows:
            return {"id": req_id, "status": "error", "error": f"unknown community {community_id}"}
        ranked = [r[0] for r in sorted(rows, key=lambda r: -(r[1] or 0.0))]
        weights = {r[0]: float(r[1] or 0.0) for r in rows}
        fp = core_fingerprint(ranked, weights)
        from ...storage.db.write_gate import commit_connection, with_db_write
        with with_db_write():
            rename_community(conn, fp, new_name)
            conn.execute(
                "UPDATE entities SET metadata_json=json_patch(COALESCE(metadata_json,'{}'),"
                " json_object('community_label', ?))"
                " WHERE json_extract(metadata_json,'$.community_id')=?",
                (new_name, community_id),
            )
            commit_connection(conn)
        return {"id": req_id, "status": "ok",
                "payload": {"status": "ok", "community_id": community_id, "name": new_name,
                            "members": len(rows)}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
