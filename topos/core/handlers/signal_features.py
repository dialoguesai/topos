"""Signal feature message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
)
from .registry import handles


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
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.list_topic_clusters(
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
        from ...features.signal.service import get_signal_service

        conn = hub.get_db_connection()
        service = get_signal_service(conn=conn)
        result = service.list_topic_cluster_members(
            cluster_id,
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
