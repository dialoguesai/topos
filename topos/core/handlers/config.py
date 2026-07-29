"""Engine config, UI config, table prefs, and identity message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    List,
    Optional,
    get_engine_config_value,
    get_mcp_request_counts,
    get_uma_request_counts,
    get_user_id,
    json,
    set_engine_config_value,
    settings,
)
from .registry import handles


UI_CONFIG_KEY = "ui_config"

# SINGLE SOURCE: this module used to keep its own copy of the ui-config
# normalizer, which silently drifted — the HTTP endpoint accepted new fields
# (graph.timeWindowDays) that this WS path (the CP proxy route) stripped.
from ...api.ui_config import (  # noqa: E402
    ALLOWED_WIDGETS as ALLOWED_PINNED_WIDGETS,
    MAX_PINNED as MAX_PINNED_WIDGETS,
    _default_ui_config,
    _normalize_ui_config,
)

@handles("migrate_browser_plugin_app_id")
async def handle_migrate_browser_plugin_app_id(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ..state import _migrate_legacy_browser_plugin_app_ids

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    updated = _migrate_legacy_browser_plugin_app_ids(conn)
    return {"id": req_id, "status": "ok", "payload": {"updated_rows": updated}}

@handles("get_request_counts")
async def handle_get_request_counts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    """Return UMA + MCP request counts from engine DB (for CP proxy or direct frontend)."""
    payload = message.get("payload") or {}
    owner_user_id = (payload.get("owner_user_id") or "").strip()
    since_days = min(max(int(payload.get("since_days") or 90), 1), 365)
    conn = hub.get_db_connection()
    if not owner_user_id and conn:
        owner_user_id = get_user_id(conn) or ""
    uma = get_uma_request_counts(conn, owner_user_id, since_days) if conn else {"total_read_requests": 0, "total_write_requests": 0, "by_app": []}
    mcp = get_mcp_request_counts(conn, since_days) if conn else {"by_source": [], "by_tool": [], "total": 0}
    return {"id": req_id, "status": "ok", "payload": {"uma": uma, "mcp": mcp}}

@handles("get_ui_config")
async def handle_get_ui_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    raw = get_engine_config_value(conn, UI_CONFIG_KEY)
    if not raw:
        return {"id": req_id, "status": "ok", "payload": _default_ui_config()}
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {}
    return {"id": req_id, "status": "ok", "payload": _normalize_ui_config(parsed)}

@handles("put_ui_config")
async def handle_put_ui_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    normalized = _normalize_ui_config(payload.get("ui_config"))
    try:
        set_engine_config_value(conn, UI_CONFIG_KEY, json.dumps(normalized))
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
    return {"id": req_id, "status": "ok", "payload": normalized}

@handles("get_data_explorer_table_prefs")
async def handle_get_data_explorer_table_prefs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...data_explorer_table_prefs import get_table_prefs as load_table_prefs

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    user_id = str(payload.get("user_id") or "").strip()
    table_name = str(payload.get("table_name") or "").strip()
    if not user_id or not table_name:
        return {"id": req_id, "status": "error", "error": "user_id and table_name required"}
    try:
        prefs = load_table_prefs(conn, user_id=user_id, table_name=table_name)
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    return {"id": req_id, "status": "ok", "payload": {"prefs": prefs}}

@handles("put_data_explorer_table_prefs")
async def handle_put_data_explorer_table_prefs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...data_explorer_table_prefs import put_table_prefs as save_table_prefs

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    user_id = str(payload.get("user_id") or "").strip()
    table_name = str(payload.get("table_name") or "").strip()
    prefs = payload.get("prefs") or {}
    if not user_id or not table_name:
        return {"id": req_id, "status": "error", "error": "user_id and table_name required"}
    try:
        saved = save_table_prefs(conn, user_id=user_id, table_name=table_name, prefs=prefs)
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    return {"id": req_id, "status": "ok", "payload": {"prefs": saved}}

@handles("delete_data_explorer_table_prefs")
async def handle_delete_data_explorer_table_prefs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...data_explorer_table_prefs import delete_table_prefs as remove_table_prefs

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    user_id = str(payload.get("user_id") or "").strip()
    table_name = str(payload.get("table_name") or "").strip()
    if not user_id or not table_name:
        return {"id": req_id, "status": "error", "error": "user_id and table_name required"}
    try:
        deleted = remove_table_prefs(conn, user_id=user_id, table_name=table_name)
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    return {"id": req_id, "status": "ok", "payload": {"deleted": deleted}}

@handles("llm_integrations_storage")
async def handle_llm_integrations_storage(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...llm_integrations_storage import handle_storage_op

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    op = str(payload.get("op") or "").strip()
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    if not op:
        return {"id": req_id, "status": "error", "error": "op is required"}
    try:
        result = handle_storage_op(conn, op=op, args=args)
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    return {"id": req_id, "status": "ok", "payload": {"result": result}}

@handles("get_user_identity")
async def handle_get_user_identity(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...storage.user_identity import get_user_identity as load_user_identity

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    identity = load_user_identity(conn, dataset_id)
    if identity is None:
        body: Dict[str, Any] = {"status": "ok", "dataset_id": dataset_id, "display_name": None}
    else:
        body = {"status": "ok", "dataset_id": dataset_id, **identity}
    return {"id": req_id, "status": "ok", "payload": body}

@handles("put_user_identity")
async def handle_put_user_identity(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...storage.user_identity import put_user_identity as persist_user_identity

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    raw_dn = payload.get("display_name")
    if isinstance(raw_dn, str):
        next_display_name = raw_dn.strip() or None
    else:
        next_display_name = None
    persist_user_identity(conn, dataset_id, display_name=next_display_name)
    return {
        "id": req_id,
        "status": "ok",
        "payload": {"status": "ok", "dataset_id": dataset_id, "display_name": next_display_name},
    }

@handles("get_sanitization_ollama_config")
async def handle_get_sanitization_ollama_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.sanitization_ollama import effective_config_for_api

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("put_sanitization_ollama_config")
async def handle_put_sanitization_ollama_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.sanitization_ollama import (
        ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE,
        effective_config_for_api,
        normalize_put_device_overrides,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        json_str = normalize_put_device_overrides(payload)
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, json_str)
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_sanitization_ollama_config")
async def handle_delete_sanitization_ollama_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.sanitization_ollama import (
        ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE,
        effective_config_for_api,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE, "{}")
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_facts_llm_config")
async def handle_get_facts_llm_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.facts_llm import effective_config_for_api

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("put_facts_llm_config")
async def handle_put_facts_llm_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.facts_llm import (
        ENGINE_CONFIG_KEY_FACTS_LLM_MODEL,
        effective_config_for_api,
        normalize_put_model,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        model = normalize_put_model(payload)
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL, model)
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_facts_llm_config")
async def handle_delete_facts_llm_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.facts_llm import (
        ENGINE_CONFIG_KEY_FACTS_LLM_MODEL,
        effective_config_for_api,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_FACTS_LLM_MODEL, "")
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_conversation_context")
async def handle_get_conversation_context(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.conversation_context import read_conversation_context

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    conversation_id = str(payload.get("conversation_id") or "").strip()
    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not conversation_id or not dataset_id:
        return {"id": req_id, "status": "error", "error": "conversation_id and dataset_id required"}
    try:
        data = read_conversation_context(conn, conversation_id, dataset_id)
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
    if data is None:
        return {"id": req_id, "status": "error", "error": "Unknown conversation"}
    return {"id": req_id, "status": "ok", "payload": data}

@handles("put_conversation_context")
async def handle_put_conversation_context(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.conversation_context import write_conversation_context

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    conversation_id = str(payload.get("conversation_id") or "").strip()
    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not conversation_id or not dataset_id:
        return {"id": req_id, "status": "error", "error": "conversation_id and dataset_id required"}
    try:
        data = write_conversation_context(conn, conversation_id, dataset_id, payload.get("context_tag"))
    except (ValueError, LookupError) as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
    return {"id": req_id, "status": "ok", "payload": data}

@handles("get_conversation_context_llm_config")
async def handle_get_conversation_context_llm_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.conversation_context_llm import effective_config_for_api

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("put_conversation_context_llm_config")
async def handle_put_conversation_context_llm_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.conversation_context_llm import (
        ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL,
        effective_config_for_api,
        normalize_put_model,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        model = normalize_put_model(payload)
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_CONVERSATION_CONTEXT_LLM_MODEL, model)
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_signal_extraction_config")
async def handle_get_signal_extraction_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.signal_extraction import effective_config_for_api

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("put_signal_extraction_config")
async def handle_put_signal_extraction_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.signal_extraction import (
        ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
        effective_config_for_api,
        normalize_put_device_overrides,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        json_str = normalize_put_device_overrides(payload)
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE, json_str)
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_signal_extraction_config")
async def handle_delete_signal_extraction_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.signal_extraction import (
        ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE,
        effective_config_for_api,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE, "{}")
        data = effective_config_for_api(settings, conn)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **data}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


# ---- Exposure-profile visibility (P1.5): the owner's toggle for whether the
# labeled "what you've been exposed to" profile surfaces on interest/identity
# answers. Default ON; the engine read path is config.settings.exposure_profile_visible.
@handles("get_upgrade_status")
async def handle_get_upgrade_status(message):
    """Upgrade runner + graph-refresh status (read-only; UI progress banner)."""
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...api.upgrades import _status_payload

        return {"id": req_id, "status": "ok", "payload": _status_payload()}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("get_exposure_profile_config")
async def handle_get_exposure_profile_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.settings import resolve_exposure_profile_visible

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        visible = resolve_exposure_profile_visible(settings, conn)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"status": "ok", "exposure_profile_visible": bool(visible)},
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("put_exposure_profile_config")
async def handle_put_exposure_profile_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...config.settings import (
        ENGINE_CONFIG_KEY_EXPOSURE_PROFILE_VISIBLE,
        resolve_exposure_profile_visible,
    )

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    if "exposure_profile_visible" not in payload:
        return {"id": req_id, "status": "error", "error": "exposure_profile_visible (bool) required"}
    raw = payload.get("exposure_profile_visible")
    if isinstance(raw, str):
        visible = raw.strip().lower() in ("1", "true", "on", "yes")
    else:
        visible = bool(raw)
    try:
        set_engine_config_value(
            conn, ENGINE_CONFIG_KEY_EXPOSURE_PROFILE_VISIBLE, "true" if visible else "false"
        )
        effective = resolve_exposure_profile_visible(settings, conn)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"status": "ok", "exposure_profile_visible": bool(effective)},
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
