"""Connection, device, compute, and LLM message handlers."""
from __future__ import annotations

import asyncio
import os
import re
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
    get_engine_config_value,
    get_services,
    get_user_id,
    logger,
    resolve_runtime_profile,
    set_engine_config_value,
    settings,
    store_user_id,
    validate_scoped_invocation_token,
)
from .registry import handles


COMPUTE_ENVELOPE_SCHEMA_VERSION = "2026-05-11"

#: engine_config key for the control plane's Topos id (CONTRACTS §3).
TOPOS_ID_CONFIG_KEY = "topos_id"


def _persist_topos_id(conn, topos_id: str) -> bool:
    """Store the control plane's ``topos_id`` once — only when empty, like ``user_id``.

    The node names its canonical scope ``{owner}:topos:{topos_id}`` from this
    without waiting for a permissions_v2 binding (PLAN §2.1). A later
    ``connection_info`` carrying a DIFFERENT id is logged and ignored: the
    stored id is what the rows were written under, and switching it under a
    live database is a pairing operation, not a reconnect.
    """
    existing = (get_engine_config_value(conn, TOPOS_ID_CONFIG_KEY) or "").strip()
    if existing:
        if existing != topos_id:
            logger.warning(
                "[PIPELINE:CONNECTION] Engine already holds topos_id=%s; control plane sent %s. Keeping the stored one.",
                existing[:8],
                topos_id[:8],
            )
        return False
    set_engine_config_value(conn, TOPOS_ID_CONFIG_KEY, topos_id)
    logger.debug("[PIPELINE:CONNECTION] Stored topos_id=%s from control plane.", topos_id[:8])
    return True

# --- key rotation (CONTRACTS §3; L8 payload) --------------------------------------------------------------
#: `key_rotated` arrives ONCE over the old key's socket:
#: ``{"topos_id", "topos_key": "tpn_<40 hex>", "grace_seconds", "revoke_at": <ISO 8601 UTC>}``.
KEY_ROTATED_TYPE = "key_rotated"
_NEW_KEY_RE = re.compile(r"^tpn_[0-9a-f]{40}$")
#: Only a VERIFIED owner-side control-plane stamp may rotate the key: never the unstamped deferral
#: (``cp_relay``), never a third party, never the local socket. A rotation frame moves the node's
#: credential, so it is held to the strictest class the relay can vouch for.
_ROTATION_ALLOWED_CLASSES = frozenset({"owner_app", "owner_automation"})
#: Rotation bookkeeping. Holds key TEXT only for the fallback (``previous_key``) — never logged.
_ROTATION_STATE: Dict[str, Any] = {}
#: How long the new key gets to connect before the node falls back to the old one for the grace.
ROTATION_CONNECT_WAIT_SECONDS = 30.0


def _client():
    return getattr(engine_state, "control_plane_client", None)


async def _sleep_async(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _rotation_frame_allowed(principal) -> bool:
    return (
        getattr(principal, "channel", None) == "cp_relay"
        and getattr(principal, "cls", None) in _ROTATION_ALLOWED_CLASSES
    )


def _current_api_key() -> str:
    client = _client()
    return str(
        getattr(client, "api_key", None)
        or getattr(settings, "topos_key", None)
        or os.environ.get("TOPOS_KEY")
        or ""
    ).strip()


async def _reconnect_with_new_key(
    client,
    *,
    new_key: str,
    previous_key: str,
    revoke_at: str,
    wait_seconds: float = ROTATION_CONNECT_WAIT_SECONDS,
    settle_seconds: float = 1.0,
) -> bool:
    """Restart the relay client under the new key; fall back to the old one if it never connects.

    The client builds its bearer header once per ``_run`` (control_plane_client.py), so a
    stop()/start() is the reconnect. The old key stays in memory (``_ROTATION_STATE``) until the
    new key's first successful connect; if that does not come within ``wait_seconds`` the node
    reconnects with the old key for the rest of the grace (the control plane keeps it until
    ``revoke_at``) while the new key stays saved in ``.env`` for the next start. Neither key is
    ever logged.
    """
    await _sleep_async(settle_seconds)  # let the ok reply leave on the old socket first
    _ROTATION_STATE["previous_key"] = previous_key
    client.api_key = new_key
    await client.stop()
    client.start()
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            connected = bool(client.get_connection_status().get("connected"))
        except Exception:  # noqa: BLE001
            connected = False
        if connected:
            _ROTATION_STATE["previous_key"] = None
            logger.info("key_rotated: reconnected with the new key; the old key is dropped from memory")
            return True
        if time.monotonic() >= deadline:
            break
        await _sleep_async(1.0)
    logger.warning(
        "key_rotated: the new key did not connect within %ss; reconnecting with the old key until %s "
        "(the new key stays saved in .env and applies on the next start)",
        wait_seconds,
        revoke_at,
    )
    client.api_key = previous_key
    await client.stop()
    client.start()
    return False


def _schedule_reconnect(*, new_key: str, previous_key: str, revoke_at: str) -> None:
    client = _client()
    if client is None:
        logger.warning("key_rotated: no control-plane client to reconnect; the new key is saved and applies on the next start")
        return
    loop = asyncio.get_running_loop()
    loop.create_task(_reconnect_with_new_key(client, new_key=new_key, previous_key=previous_key, revoke_at=revoke_at))


@handles("key_rotated")  # literal: tests/test_protocol_handled_types.py scans the source for it
async def handle_key_rotated(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Save the rotated key to ``~/.topos/.env`` (0600, via ``_save_topos_key``), reply ok, reconnect.

    Refused unless the frame carries a verified owner-side control-plane stamp (403, like every
    owner door). Idempotent: a second frame naming the key already in use (or already in the file)
    answers ok and does nothing. Nothing here prints or logs a key.
    """
    req_id = message.get("id")
    from ...principal import current_principal

    if not _rotation_frame_allowed(current_principal()):
        return {"id": req_id, "status": "error", "code": 403, "error": "owner_mode_required"}
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    new_key = str(payload.get("topos_key") or "").strip()
    if not _NEW_KEY_RE.fullmatch(new_key):
        return {"id": req_id, "status": "error", "code": 422, "error": "key_rotated: topos_key missing or malformed"}
    revoke_at = str(payload.get("revoke_at") or "")
    grace_seconds = payload.get("grace_seconds")

    from ...cli.commands import _read_env_file_key, _save_topos_key, _user_env_path

    env_path = _user_env_path()
    current = _current_api_key()
    if new_key == current or new_key == (_read_env_file_key(env_path) or "") or new_key == _ROTATION_STATE.get("applied_key"):
        logger.info("key_rotated: this key is already in use; nothing to do")
        return {"id": req_id, "status": "ok"}
    try:
        _save_topos_key(new_key, env_path=env_path)
    except OSError as exc:
        logger.warning("key_rotated: could not write %s (%s); the old key stays in use", env_path, type(exc).__name__)
        return {"id": req_id, "status": "error", "code": 500, "error": "key_rotated: could not save the new key"}
    os.environ["TOPOS_KEY"] = new_key
    try:
        settings.topos_key = new_key
    except Exception:  # noqa: BLE001 — a read-only settings object changes nothing below
        pass
    _ROTATION_STATE["applied_key"] = new_key
    _ROTATION_STATE["revoke_at"] = revoke_at
    logger.info(
        "key_rotated: new key saved to %s for topos_id=%s; reconnecting before %s (grace %ss)",
        env_path,
        str(payload.get("topos_id") or "")[:12],
        revoke_at,
        grace_seconds,
    )
    _schedule_reconnect(new_key=new_key, previous_key=current, revoke_at=revoke_at)
    return {"id": req_id, "status": "ok"}


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

@handles("connection_info", off_loop=True)
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
    topos_id = str(payload.get("topos_id") or message.get("topos_id") or "").strip()
    if topos_id:
        conn = hub.get_db_connection()
        if conn:
            try:
                _persist_topos_id(conn, topos_id)
            except Exception as exc:  # noqa: BLE001 — a failed write must not drop the connection
                logger.warning("[PIPELINE:CONNECTION] could not store topos_id: %s", type(exc).__name__)
    # Only send a response if CP sent an id (so it can match); unsolicited connection_info has no id
    connection_info_id = message.get("id")
    if connection_info_id is not None:
        return {"id": connection_info_id, "status": "ok"}
    return None

@handles("healthcheck")
async def handle_healthcheck(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Liveness, plus a verdict on whether the database can be read.

    This is the healthcheck the app actually sees — the control plane forwards
    its ``/healthcheck`` route over this socket. It answered ``ok`` without ever
    consulting SQLite, so a node with a dead database was indistinguishable from
    a working one and the app rendered a connected Topos over an empty graph.

    ``status`` stays ``ok`` regardless: reaching this handler proves the event
    loop is alive, which is what liveness means and what the reconnect logic
    keys on. The database verdict rides alongside it.
    """
    req_id = message.get("id")
    if not req_id:
        return None
    from ..db_health import probe_db_health

    db_ok, db_error = await probe_db_health()
    payload: Dict[str, Any] = {"status": "ok", "db_ok": db_ok}
    if db_error:
        payload["db_error"] = db_error
    return {"id": req_id, "status": "ok", "payload": payload}

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
    started, wall_started = time.monotonic(), time.time()
    # Every outcome line carries the id: two routines overlap on the node, and
    # the service-layer lines have none, so pairing by order is ambiguous.
    logger.info(
        "llm_generation request: id=%s provider=%r model=%r prompt_chars=%d stream=%s",
        req_id,
        payload.get("provider"),
        payload.get("model"),
        len(str(payload.get("prompt") or "")),
        bool(payload.get("stream")),
    )

    def _log_failed(exc: BaseException, code: Any) -> None:
        # Classes and codes only: an error's text can quote an Ollama body.
        # openai.py raises `from exc`, so the cause names the transport error.
        logger.warning(
            "llm_generation failed: id=%s provider=%r model=%r code=%s cause=%s"
            " elapsed_s=%.1f wall_s=%.1f stream=%s",
            req_id,
            payload.get("provider"),
            payload.get("model"),
            code,
            type(exc.__cause__ or exc).__name__,
            time.monotonic() - started,
            time.time() - wall_started,
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
        logger.info(
            "llm_generation done: id=%s model=%r output_chars=%d elapsed_s=%.1f wall_s=%.1f",
            req_id,
            payload.get("model"),
            len(str((result or {}).get("output") or "")),
            time.monotonic() - started,
            time.time() - wall_started,
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
            except Exception as e:  # noqa: BLE001 — reporting must not mask the cancel
                logger.warning(
                    "llm_generation cancel reply not sent: id=%s exc=%s", req_id, type(e).__name__
                )
        raise
    except LlmTypedError as exc:
        _log_failed(exc, exc.code)
        return {
            "id": req_id,
            "status": "error",
            "error": exc.message,
            "error_code": exc.code,
        }
    except HTTPException as exc:
        _log_failed(exc, exc.status_code)
        detail = exc.detail
        err_msg = detail if isinstance(detail, str) else str(detail)
        return {
            "id": req_id,
            "status": "error",
            "error": err_msg,
            "error_code": exc.status_code,
        }
    except Exception as exc:  # noqa: BLE001
        _log_failed(exc, None)
        # A bare `ValueError()` has empty text; the control plane would record
        # a failure with no reason at all.
        return {"id": req_id, "status": "error", "error": str(exc) or type(exc).__name__}
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
    # Schema stamps that this payload knows how to distinguish reachable from
    # empty. The CP refuses to default `reachable` without it — an older node
    # answering `{models: []}` must read as unknown, not "Ollama up, empty".
    _schema = {"ollama_models_schema": 1}
    # The setup card needs to name an install command, and the command depends
    # on the machine the NODE runs on — not the browser's. Carried on the
    # payload the card already polls so it never has to guess from a user agent
    # (topos/engine/ollama_setup_guidance.py).
    from ...engine.ollama_setup_guidance import install_guidance

    _platform = {"setup_guidance": install_guidance()}
    try:
        result = await get_services().llm.list_ollama_models()
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"reachable": True, **_schema, **_platform, **result},
        }
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
                "payload": {
                    "reachable": False,
                    "models": [],
                    "detail": err_msg,
                    **_schema,
                    **_platform,
                },
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

        # `size_bytes` lets the caller be refused before the transfer starts
        # rather than partway in. The setup card knows the curated starter's
        # size; anything else is caught by the mid-stream check once Ollama
        # reports the real total.
        return {
            "id": req_id,
            "status": "ok",
            "payload": start_pull(
                str(payload.get("model") or ""),
                known_size_bytes=payload.get("size_bytes"),
            ),
        }
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

@handles("ollama_install")
async def handle_ollama_install(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...engine.ollama_install import start_install

        return {"id": req_id, "status": "ok", "payload": start_install()}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("ollama_install_status")
async def handle_ollama_install_status(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...engine.ollama_install import install_status

        return {"id": req_id, "status": "ok", "payload": install_status()}
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

@handles("get_disk_space_policy")
async def handle_get_disk_space_policy(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The owner's minimum-free-disk floor and the bounds the settings form needs."""
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...api.disk_space_config import policy_payload

        return {
            "id": req_id,
            "status": "ok",
            "payload": {"status": "ok", **policy_payload(hub.get_db_connection())},
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("put_disk_space_policy")
async def handle_put_disk_space_policy(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Set the floor. Out-of-range values are refused, not clamped — the form has
    to show the number the node actually agreed to."""
    req_id = message.get("id")
    if not req_id:
        return None
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        from ...api.disk_space_config import normalize_put_min_free_bytes, policy_payload
        from ...config.settings import ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES
        from .common import set_engine_config_value

        value = normalize_put_min_free_bytes(message.get("payload"))
        set_engine_config_value(conn, ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES, str(value))
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"status": "ok", **policy_payload(conn)},
        }
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "error_code": 400}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_disk_status")
async def handle_get_disk_status(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Free space against the floor, plus what the model manager could reclaim.

    One payload for both surfaces that ask it — the settings panel and the
    sidebar warning — so the two can never disagree about whether the node is
    under its floor.
    """
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...api import disk_space_config
        from .common import run_db_read

        # NR166 R3b: in a worker thread, on that worker's own connection. This resolved a connection on the
        # loop and the core handed it to its worker: the one thing `run_db_read` exists to prevent.
        payload = await run_db_read(lambda conn: disk_space_config._status_sync(conn))
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **payload}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
