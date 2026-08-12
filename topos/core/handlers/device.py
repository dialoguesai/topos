"""Connection, device, compute, and LLM message handlers."""
from __future__ import annotations

import asyncio
import time

import topos.core.handlers as hub

from ...services.llm.openai import LlmTypedError
from .common import (
    Any,
    Dict,
    HTTPException,
    Optional,
    RUNTIME_PROFILE_OPERATIONS,
    ScopedTokenValidationError,
    engine_state,
    get_services,
    get_user_id,
    logger,
    resolve_runtime_profile,
    settings,
    store_user_id,
    validate_scoped_invocation_token,
)
from .registry import handles


COMPUTE_ENVELOPE_SCHEMA_VERSION = "2026-05-11"

def _compute_envelope(
    *,
    request_id: str,
    engine_instance_id: str,
    policy_hash: Optional[str],
    runtime_profile: Optional[Dict[str, Any]] = None,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "schema_version": COMPUTE_ENVELOPE_SCHEMA_VERSION,
        "request_id": request_id,
        "engine_instance_id": engine_instance_id,
        "policy_hash": policy_hash,
        "runtime_profile": runtime_profile or {"id": resolve_runtime_profile()},
        "result": result,
    }
    out: Dict[str, Any] = {"id": request_id, "status": status, "payload": payload}
    if error:
        out["error"] = error
    if error_code:
        out["error_code"] = error_code
    return out

def _operation_to_msg_type(operation: str) -> str:
    mapping = {
        "healthcheck": "healthcheck",
        "llm_generation": "llm_generation",
        "ollama_list_models": "ollama_list_models",
        "sanitization.run": "llm_generation",
        "filter_lab.list_job_groups": "list_filter_lab_job_groups",
        "filter_lab.run": "post_filter_lab_job_group",
        "filter_lab.create_job_group": "post_filter_lab_job_group",
    }
    return mapping.get(operation, operation.strip().lower().replace(".", "_"))

@handles("connection_info")
async def handle_connection_info(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    """
        Handle connection_info message from control plane with user_id from auth.
        
        Linking Strategy:
        - If engine has NO user_id: Store auth user_id (engine started, waiting for user)
        - If engine HAS user_id that matches: Log confirmation (already linked)
        - If engine HAS user_id that differs: Warn but keep existing (engine started independently,
          may have data encrypted with that user_id - requires explicit pairing to change)
        """
    payload = message.get("payload") or {}
    auth_user_id = payload.get("user_id") or message.get("user_id")
    if auth_user_id:
        conn = hub.get_db_connection()
        if conn:
            existing_user_id = get_user_id(conn)
            
            if not existing_user_id:
                # Engine started but no user_id yet - link to authenticated user
                store_user_id(conn, auth_user_id)
                logger.debug(
                    "[PIPELINE:CONNECTION] Engine had no user_id. Stored auth user_id=%s from control plane. "
                    "Engine is now linked to authenticated user.",
                    auth_user_id[:8] if auth_user_id else None,
                )
            elif existing_user_id == auth_user_id:
                # Already linked - perfect match
                logger.debug(
                    "[PIPELINE:CONNECTION] Engine user_id=%s matches auth user_id from control plane (already linked)",
                    existing_user_id[:8] if existing_user_id else None,
                )
            else:
                # Mismatch: Engine started independently with its own user_id
                # Don't replace automatically - engine may have data encrypted with existing user_id
                logger.warning(
                    "[PIPELINE:CONNECTION] Engine has independent user_id=%s but control plane provided auth user_id=%s. "
                    "Keeping existing engine user_id to preserve data integrity. "
                    "If you want to link them, use device pairing (this will migrate/re-encrypt data).",
                    existing_user_id[:8] if existing_user_id else None,
                    auth_user_id[:8] if auth_user_id else None,
                )
        # Note: We don't update settings.topos_user_id here because:
        # 1. The real source of truth is the database (engine_config table)
        # 2. settings.topos_user_id is read from environment variable at startup
        # 3. The database value takes precedence for all operations
    # Only send a response if CP sent an id (so it can match); unsolicited connection_info has no id
    connection_info_id = message.get("id")
    if connection_info_id is not None:
        return {"id": connection_info_id, "status": "ok"}
    return None

@handles("healthcheck")
async def handle_healthcheck(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    return {"id": req_id, "status": "ok", "payload": {"status": "ok"}}

@handles("compute_invoke")
async def handle_compute_invoke(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    invocation_token = str(payload.get("invocation_token") or "")
    resource_id = str(payload.get("resource_id") or "")
    operation = str(payload.get("operation") or "")
    request_id = str(message.get("id") or payload.get("request_id") or "")
    policy_hash = payload.get("policy_hash")
    engine_instance_id = str(payload.get("engine_instance_id") or settings.engine_name or "engine_local")
    runtime_profile_id = resolve_runtime_profile()
    allowed_operations = RUNTIME_PROFILE_OPERATIONS.get(runtime_profile_id, [])
    runtime_profile = {
        "id": runtime_profile_id,
        "allowed_operations": allowed_operations,
    }
    if not request_id:
        return None
    try:
        _ = validate_scoped_invocation_token(
            token=invocation_token,
            secret=str(settings.topos_key or ""),
            resource_id=resource_id,
            operation=operation,
            request_id=request_id,
        )
    except ScopedTokenValidationError as exc:
        return _compute_envelope(
            request_id=request_id,
            engine_instance_id=engine_instance_id,
            policy_hash=str(policy_hash) if policy_hash else None,
            runtime_profile=runtime_profile,
            status="error",
            error=exc.message,
            error_code=exc.code,
        )
    if allowed_operations and operation not in allowed_operations:
        return _compute_envelope(
            request_id=request_id,
            engine_instance_id=engine_instance_id,
            policy_hash=str(policy_hash) if policy_hash else None,
            runtime_profile=runtime_profile,
            status="error",
            error=f"operation '{operation}' is not allowed for runtime profile '{runtime_profile_id}'",
            error_code="PROFILE_CAPABILITY_DENIED",
        )
    target_msg_type = _operation_to_msg_type(operation)
    if target_msg_type == "compute_invoke":
        return _compute_envelope(
            request_id=request_id,
            engine_instance_id=engine_instance_id,
            policy_hash=str(policy_hash) if policy_hash else None,
            runtime_profile=runtime_profile,
            status="error",
            error="operation cannot target compute_invoke",
            error_code="OPERATION_NOT_ALLOWED",
        )
    forwarded_message = {
        "id": request_id,
        "type": target_msg_type,
        "payload": payload.get("input") if isinstance(payload.get("input"), dict) else {},
    }
    try:
        forwarded = await hub.handle_control_plane_request(forwarded_message)
    except Exception as exc:  # noqa: BLE001
        return _compute_envelope(
            request_id=request_id,
            engine_instance_id=engine_instance_id,
            policy_hash=str(policy_hash) if policy_hash else None,
            runtime_profile=runtime_profile,
            status="error",
            error=str(exc),
            error_code="COMPUTE_EXECUTION_FAILED",
        )
    if not forwarded:
        return _compute_envelope(
            request_id=request_id,
            engine_instance_id=engine_instance_id,
            policy_hash=str(policy_hash) if policy_hash else None,
            runtime_profile=runtime_profile,
            status="error",
            error="compute handler returned no result",
            error_code="COMPUTE_EXECUTION_FAILED",
        )
    if str(forwarded.get("status") or "").lower() == "ok":
        return _compute_envelope(
            request_id=request_id,
            engine_instance_id=engine_instance_id,
            policy_hash=str(policy_hash) if policy_hash else None,
            runtime_profile=runtime_profile,
            status="ok",
            result=forwarded.get("payload") if isinstance(forwarded.get("payload"), dict) else {"value": forwarded.get("payload")},
        )
    return _compute_envelope(
        request_id=request_id,
        engine_instance_id=engine_instance_id,
        policy_hash=str(policy_hash) if policy_hash else None,
        runtime_profile=runtime_profile,
        status="error",
        error=str(forwarded.get("error") or "compute invocation failed"),
        error_code=str(forwarded.get("error_code") or "COMPUTE_EXECUTION_FAILED"),
    )

def _unusable_num_ctx(payload: Dict[str, Any]) -> Optional[str]:
    """Check `num_ctx` alone, not the whole `GenerationRequest`.

    Validating the envelope here would be the obvious move and the wrong one:
    `GenerationRequest.max_tokens` caps at 4096, while the routines lane
    deliberately asks for 8000-16000 to clear the reasoning floor. So only the
    bound that has no other enforcement on this path is applied.
    """
    raw = payload.get("num_ctx")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return f"num_ctx must be a positive integer, got {raw!r}"
    return None


#: In-flight llm_generation tasks by request id, so ``llm_cancel`` can abort
#: one (PLAN_HOME_CHAT_STREAMING_SLA §5). Cancellation unwinds the httpx
#: stream, which closes the connection — the only cancel Ollama has, and it
#: frees the decode slot within one prefill batch (probe-verified 2026-08-12).
_LLM_GENERATION_TASKS: Dict[str, asyncio.Task] = {}


@handles("llm_generation")
async def handle_llm_generation(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    unusable = _unusable_num_ctx(payload)
    if unusable is not None:
        return {"id": req_id, "status": "error", "error": unusable, "error_code": 422}
    logger.info(
        "llm_generation request: provider=%r model=%r prompt_chars=%d stream=%s",
        payload.get("provider"),
        payload.get("model"),
        len(str(payload.get("prompt") or "")),
        bool(payload.get("stream")),
    )
    current = asyncio.current_task()
    if current is not None:
        _LLM_GENERATION_TASKS[str(req_id)] = current
    try:
        on_delta = None
        on_protocol = None
        if payload.get("stream"):
            cp_client = getattr(engine_state, "control_plane_client", None)
            # Frame contract (SLA plan §2): ALL interim traffic rides
            # status:"chunk" with payload.delta always present — old control
            # planes provably no-op empty-delta chunks, so kind frames are
            # additive. A new top-level status would resolve their pending
            # future; never introduce one.
            seq = 0

            async def _emit_chunk(delta: str) -> None:
                if cp_client is not None:
                    await cp_client.send_message(
                        {"id": req_id, "status": "chunk", "payload": {"delta": delta}}
                    )

            async def _emit_protocol(kind: str, fields: Dict[str, Any]) -> None:
                nonlocal seq
                if cp_client is None:
                    return
                seq += 1
                await cp_client.send_message(
                    {
                        "id": req_id,
                        "status": "chunk",
                        "payload": {
                            "delta": "",
                            "kind": kind,
                            "seq": seq,
                            "ts_ms": int(time.monotonic() * 1000),
                            **fields,
                        },
                    }
                )

            on_delta = _emit_chunk
            on_protocol = _emit_protocol
            # S1 made real: the engine acknowledges the dispatch before any
            # model work, replacing the control plane's synthesized ack.
            await _emit_protocol("ack", {})
        result = await get_services().llm.generate(
            payload, on_delta=on_delta, on_protocol=on_protocol
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except asyncio.CancelledError:
        # llm_cancel: answer the original request with a typed 499 so the
        # control plane's pending future settles, then keep cancelling. The
        # send is shielded — it must not be torn out from under itself by the
        # very cancellation it is reporting.
        cp_client = getattr(engine_state, "control_plane_client", None)
        if cp_client is not None:
            try:
                await asyncio.shield(
                    cp_client.send_message(
                        {
                            "id": req_id,
                            "status": "error",
                            "error": "Generation cancelled by the control plane.",
                            "error_code": 499,
                        }
                    )
                )
            except Exception:  # noqa: BLE001 — reporting must not mask the cancel
                pass
        raise
    except LlmTypedError as exc:
        return {
            "id": req_id,
            "status": "error",
            "error": exc.message,
            "error_code": exc.code,
        }
    except HTTPException as exc:
        detail = exc.detail
        err_msg = detail if isinstance(detail, str) else str(detail)
        return {
            "id": req_id,
            "status": "error",
            "error": err_msg,
            "error_code": exc.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
    finally:
        _LLM_GENERATION_TASKS.pop(str(req_id), None)


@handles("llm_cancel")
async def handle_llm_cancel(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cancel an in-flight llm_generation (SLA plan §5).

    The cancel message carries its OWN fresh id — the target rides in the
    payload. (Reusing the target's id would have this handler's reply settle
    the original request's future on the control plane.)
    """
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    target = str(payload.get("target_request_id") or "").strip()
    if not target:
        return {"id": req_id, "status": "error", "error": "target_request_id required", "error_code": 422}
    task = _LLM_GENERATION_TASKS.get(target)
    if task is None or task.done():
        return {"id": req_id, "status": "ok", "payload": {"cancelled": False, "reason": "not_found"}}
    task.cancel()
    logger.info("llm_cancel: cancelled in-flight generation %s", target[:8])
    return {"id": req_id, "status": "ok", "payload": {"cancelled": True}}

@handles("ollama_list_models")
async def handle_ollama_list_models(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        result = await get_services().llm.list_ollama_models()
        return {"id": req_id, "status": "ok", "payload": {"reachable": True, **result}}
    except HTTPException as exc:
        detail = exc.detail
        err_msg = detail if isinstance(detail, str) else str(detail)
        if exc.status_code == 502:
            # "Ollama is not running" is a fact about this machine, not a failed
            # request. Reported as data so the UI can tell it apart from "Ollama
            # is running with nothing pulled" — indistinguishable until now
            # (PLAN_LOCAL_MODEL_QUICKSTART §1.7).
            return {
                "id": req_id,
                "status": "ok",
                "payload": {"reachable": False, "models": [], "detail": err_msg},
            }
        return {
            "id": req_id,
            "status": "error",
            "error": err_msg,
            "error_code": exc.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("ollama_pull_model")
async def handle_ollama_pull_model(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...engine.ollama_pull import start_pull

        return {"id": req_id, "status": "ok", "payload": start_pull(str(payload.get("model") or ""))}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "error_code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("ollama_pull_status")
async def handle_ollama_pull_status(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...engine.ollama_pull import pull_status

        record = pull_status(str(payload.get("model") or ""))
        return {"id": req_id, "status": "ok", "payload": record}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_device_info")
async def handle_get_device_info(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    context = payload if isinstance(payload, dict) else {}
    try:
        result = await get_services().device.get_device_info(context=context)
        return {"id": req_id, "status": "ok", "payload": result.model_dump()}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("set_device_name")
async def handle_set_device_name(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        result = await get_services().device.set_device_name(payload.get("device_name", ""))
        return {"id": req_id, "status": "ok", "payload": result.model_dump()}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_runtime_bootstrap")
async def handle_get_runtime_bootstrap(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...api.runtime_bootstrap import _get_runtime_bootstrap_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        result = await _get_runtime_bootstrap_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:QUERY] get_runtime_bootstrap error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}
