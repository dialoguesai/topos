from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..auth import require_api_key
from ..ingestion.ingest_helpers import ingest_file_payload, ingest_ui_payload
from ..api.enrichment import _process_enrichment_core
from ..engine.usage_observation import emit_usage_observation
from ..sources import install_service
from ..sources.definitions import with_source_capabilities

router = APIRouter()
logger = logging.getLogger("topos.api.source_install")


def _ok_envelope(request_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": "ok", "request_id": request_id, **payload}


def _log_request(action: str, request_id: str, payload: Optional[Dict[str, Any]]) -> None:
    payload = payload or {}
    logger.info(
        "[SOURCE_INSTALL] action=%s request_id=%s source_id=%s version_id=%s dataset_id=%s",
        action,
        request_id,
        str(payload.get("source_id") or ""),
        str(payload.get("version_id") or ""),
        str(payload.get("dataset_id") or ""),
    )


def _scope_from_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = payload or {}
    raw_scope = payload.get("scope")
    if isinstance(raw_scope, dict):
        return raw_scope
    return {
        "user_id": payload.get("user_id"),
        "device_id": payload.get("device_id"),
        "topos_id": payload.get("topos_id"),
        "dataset_id": payload.get("dataset_id"),
    }


def _require_scope_fields(scope: Dict[str, Any], *, required: tuple[str, ...]) -> None:
    missing = [field for field in required if not str(scope.get(field) or "").strip()]
    if missing:
        raise ValueError(f"{', '.join(missing)} required")


def _scope_candidates(scope: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Generate progressively relaxed scope candidates for source lookup."""
    user_id = str(scope.get("user_id") or "*").strip() or "*"
    device_id = str(scope.get("device_id") or "*").strip() or "*"
    topos_id = str(scope.get("topos_id") or "*").strip() or "*"
    dataset_id = str(scope.get("dataset_id") or "*").strip() or "*"

    candidates = [
        {"user_id": user_id, "device_id": device_id, "topos_id": topos_id, "dataset_id": dataset_id},
        {"user_id": user_id, "device_id": device_id, "topos_id": topos_id, "dataset_id": "*"},
        {"user_id": user_id, "device_id": device_id, "topos_id": "*", "dataset_id": "*"},
        {"user_id": user_id, "device_id": "*", "topos_id": "*", "dataset_id": "*"},
        {"user_id": "*", "device_id": "*", "topos_id": "*", "dataset_id": "*"},
    ]

    deduped: list[Dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for cand in candidates:
        key = (
            str(cand.get("user_id") or "*"),
            str(cand.get("device_id") or "*"),
            str(cand.get("topos_id") or "*"),
            str(cand.get("dataset_id") or "*"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cand)
    return deduped


def _resolve_active_source_definition(source_id: str, scope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve active source def with tolerant scope fallback for test endpoints."""
    for candidate_scope in _scope_candidates(scope):
        source_def = install_service.get_active_source_definition(source_id=source_id, scope=candidate_scope)
        if source_def:
            return source_def

    # Final fallback: any active install for this source owned by the same user.
    wanted_user = str(scope.get("user_id") or "").strip()
    installs = install_service.list_installs(source_id=source_id)
    for rec in installs:
        if not rec.is_active:
            continue
        rec_user = str((rec.scope or {}).get("user_id") or "*").strip()
        if wanted_user and rec_user not in (wanted_user, "*"):
            continue
        return rec.source_definition_json
    return None


async def _install_source_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    record = install_service.install_source(
        source_definition_json=payload.get("source_definition_json")
        if isinstance(payload.get("source_definition_json"), dict)
        else None,
        source_version_row_json=payload.get("source_version_row_json")
        if isinstance(payload.get("source_version_row_json"), dict)
        else None,
        version_id=str(payload.get("version_id")).strip() if payload.get("version_id") else None,
        scope=_scope_from_payload(payload),
    )
    return {"status": "ok", "install": record.to_dict()}


async def _list_install_status_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    scope = _scope_from_payload(payload)
    source_id = str(payload.get("source_id")).strip() if payload.get("source_id") else None
    installs = install_service.list_installs(
        scope=scope,
        source_id=source_id,
    )
    return {"status": "ok", "installs": [record.to_dict() for record in installs]}


async def _list_sources_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    scope = _scope_from_payload(payload)
    _require_scope_fields(scope, required=("user_id", "dataset_id", "topos_id"))
    install_service.rehydrate_active_installs_runtime()
    installs = install_service.list_installs(scope=scope)
    # Keep one active source definition per source_id.
    active_by_source: Dict[str, Dict[str, Any]] = {}
    for rec in installs:
        if not rec.is_active:
            continue
        sid = str(rec.source_id or "").strip()
        source_def = rec.source_definition_json if isinstance(rec.source_definition_json, dict) else {}
        if not sid or sid in active_by_source or not source_def:
            continue
        active_by_source[sid] = with_source_capabilities(source_def)
    sources = list(active_by_source.values())
    return {"status": "ok", "sources": sources}


async def _patch_source_install_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_id = str(payload.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("source_id is required")
    partial = payload.get("source_definition_json")
    if not isinstance(partial, dict):
        raise ValueError("source_definition_json object is required")
    record = install_service.patch_source_install(
        source_id=source_id,
        scope=_scope_from_payload(payload),
        source_definition_json=partial,
    )
    return {"status": "ok", "install": record.to_dict()}


async def _uninstall_source_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_id = str(payload.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("source_id is required")
    # delete_source_tables is deprecated; maps to scrub-lite (no brief refresh).
    result = install_service.uninstall_source(
        source_id=source_id,
        scope=_scope_from_payload(payload),
        delete_source_tables=bool(payload.get("delete_source_tables")),
    )
    return {"status": "ok", **result}


async def _test_ingestion_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_id = str(payload.get("source_id") or "").strip()
    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not source_id or not dataset_id:
        raise ValueError("source_id and dataset_id are required")
    source_scope = _scope_from_payload(payload)
    source_def = _resolve_active_source_definition(source_id=source_id, scope=source_scope)
    if not source_def:
        raise LookupError(f"No active install found for source_id={source_id}")

    from ..sources.definitions import (
        DELIVERY_CLIENT_PUSH,
        DELIVERY_OWNER_UI,
        DELIVERY_OWNER_UPLOAD,
        derive_delivery,
    )

    source_type = str(source_def.get("source_type") or "file")
    # Installed definitions serialized before the delivery rollout lack the field.
    delivery = str(source_def.get("delivery") or "").strip() or derive_delivery(source_type, source_id)
    schema_id = str(source_def.get("schema_id") or source_def.get("parser_id") or "").strip()
    if not schema_id:
        raise ValueError("Installed source definition is missing schema_id/parser_id")

    if delivery == DELIVERY_OWNER_UPLOAD:
        file_path = str(payload.get("sample_file_path") or "").strip()
        if not file_path:
            raise ValueError("sample_file_path is required for file source tests")
        result = await ingest_file_payload(
            dataset_id=dataset_id,
            schema_id=schema_id,
            file_path=file_path,
            source_id=source_id,
        )
    elif delivery in (DELIVERY_CLIENT_PUSH, DELIVERY_OWNER_UI):
        sample_payload = payload.get("sample_payload")
        if not isinstance(sample_payload, dict):
            raise ValueError("sample_payload object is required for ui_stream source tests")
        result = await ingest_ui_payload(
            dataset_id=dataset_id,
            schema_id=schema_id,
            payload=sample_payload,
            source_id=source_id,
        )
    else:
        raise ValueError(f"Unsupported installed source_type for test ingestion: {source_type}")

    return {"status": "ok", "source_id": source_id, "dataset_id": dataset_id, "result": result}


async def _test_enrichment_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_id = str(payload.get("source_id") or "").strip()
    dataset_id = str(payload.get("dataset_id") or "").strip() or None
    if not source_id:
        raise ValueError("source_id is required")
    job_names = payload.get("job_names")
    if job_names is not None and not isinstance(job_names, list):
        raise ValueError("job_names must be a list when provided")
    result = await _process_enrichment_core(
        source_id=source_id,
        dataset_id=dataset_id,
        job_names=job_names,
        force_reprocess=bool(payload.get("force_reprocess")),
    )
    return {"status": "ok", "source_id": source_id, "dataset_id": dataset_id, "result": result}


@router.post("/source-install", dependencies=[Depends(require_api_key)])
async def install_source(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    _log_request("install_source", request_id, payload)
    try:
        result = await _install_source_core(payload)
        scope = _scope_from_payload(payload)
        await emit_usage_observation(
            action="source.install.completed",
            quantity=1,
            producer="api.source_install",
            canonical_action_identity={
                "source_id": str(payload.get("source_id") or ""),
                "version_id": str(payload.get("version_id") or ""),
                "dataset_id": str(scope.get("dataset_id") or ""),
                "user_id": str(scope.get("user_id") or ""),
            },
            topos_id=str(scope.get("dataset_id") or ""),
            trust_class="observe_only",
            metadata={"endpoint": "/source-install"},
        )
        return _ok_envelope(request_id, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/source-install-status", dependencies=[Depends(require_api_key)])
async def source_install_status(
    source_id: Optional[str] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
    topos_id: Optional[str] = Query(default=None),
    dataset_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    payload = {
        "source_id": source_id,
        "user_id": user_id,
        "device_id": device_id,
        "topos_id": topos_id,
        "dataset_id": dataset_id,
    }
    _log_request("source_install_status", request_id, payload)
    result = await _list_install_status_core(payload)
    return _ok_envelope(request_id, result)


@router.patch("/source-install", dependencies=[Depends(require_api_key)])
async def patch_source_install(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    _log_request("patch_source_install", request_id, payload)
    try:
        result = await _patch_source_install_core(payload)
        return _ok_envelope(request_id, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/source-install", dependencies=[Depends(require_api_key)])
async def uninstall_source(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    _log_request("uninstall_source", request_id, payload)
    try:
        result = await _uninstall_source_core(payload)
        return _ok_envelope(request_id, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/source-test-ingestion", dependencies=[Depends(require_api_key)])
async def source_test_ingestion(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    _log_request("source_test_ingestion", request_id, payload)
    try:
        result = await _test_ingestion_core(payload)
        return _ok_envelope(request_id, result)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/source-test-enrichment", dependencies=[Depends(require_api_key)])
async def source_test_enrichment(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    _log_request("source_test_enrichment", request_id, payload)
    try:
        result = await _test_enrichment_core(payload)
        return _ok_envelope(request_id, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/source-test-enrichment-trigger", dependencies=[Depends(require_api_key)])
async def source_test_enrichment_trigger(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    # Alias endpoint for explicit "manual trigger" semantics used by control plane/UI contracts.
    request_id = str(uuid.uuid4())
    _log_request("source_test_enrichment_trigger", request_id, payload)
    try:
        result = await _test_enrichment_core(payload)
        return _ok_envelope(request_id, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))

