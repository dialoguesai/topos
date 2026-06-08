from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .source_install import _list_install_status_core, _scope_from_payload
from ..core.state import get_db_connection
from ..llm_integrations_storage import (
    ACTIVE_PROVIDERS,
    DEFAULT_MODELS,
    SUPPORTED_BYOK_PROVIDERS,
    handle_storage_op,
)

DEFAULT_SECTIONS = ("llm", "install_status")


def _normalize_include(raw: Any) -> Set[str]:
    if isinstance(raw, str):
        parts = {part.strip() for part in raw.split(",") if part.strip()}
        return parts or set(DEFAULT_SECTIONS)
    if isinstance(raw, list):
        parts = {str(part).strip() for part in raw if str(part).strip()}
        return parts or set(DEFAULT_SECTIONS)
    return set(DEFAULT_SECTIONS)


def _provider_status_row(provider: str, cred: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    configured = bool(cred and str(cred.get("ciphertext") or "").strip())
    return {
        "provider": provider,
        "configured": configured,
        "key_hint": str(cred.get("key_hint") or "") if configured else None,
        "default_model": (
            str(cred.get("default_model") or DEFAULT_MODELS.get(provider) or "")
            if configured
            else DEFAULT_MODELS.get(provider)
        ),
        "last_validated_at": cred.get("last_validated_at") if configured else None,
    }


def _build_llm_integrations_section(topos_id: str) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database not available")
    cred_rows = handle_storage_op(conn, op="list_credentials", args={"topos_id": topos_id})
    prefs = handle_storage_op(conn, op="get_preferences", args={"topos_id": topos_id}) or {}
    cred_by_provider = {
        str(row.get("provider") or ""): row for row in cred_rows if isinstance(row, dict)
    }
    providers = [_provider_status_row(provider, cred_by_provider.get(provider)) for provider in SUPPORTED_BYOK_PROVIDERS]
    return {
        "status": "ok",
        "topos_id": topos_id,
        "storage_backend": "engine",
        "active_provider": prefs.get("active_provider"),
        "providers": providers,
        "supported_active_providers": list(ACTIVE_PROVIDERS),
    }


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _run_sync_section(runner: Callable[[], Any]) -> tuple[Optional[Any], Optional[str], float]:
    started = time.perf_counter()
    try:
        return runner(), None, _elapsed_ms(started)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc), _elapsed_ms(started)


async def _run_async_section(runner: Callable[[], Awaitable[Any]]) -> tuple[Optional[Any], Optional[str], float]:
    started = time.perf_counter()
    try:
        return await runner(), None, _elapsed_ms(started)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc), _elapsed_ms(started)


def _record_section(
    sections: Dict[str, Any],
    section_errors: Dict[str, str],
    section_timings_ms: Dict[str, float],
    *,
    section_key: str,
    result_key: str,
    value: Optional[Any],
    err: Optional[str],
    elapsed_ms: float,
) -> None:
    section_timings_ms[section_key] = elapsed_ms
    if err:
        section_errors[section_key] = err
    elif value is not None:
        sections[result_key] = value


async def _get_runtime_bootstrap_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    include = _normalize_include(payload.get("include"))
    scope = _scope_from_payload(payload)
    topos_id = str(scope.get("topos_id") or payload.get("topos_id") or "").strip()

    sections: Dict[str, Any] = {}
    section_errors: Dict[str, str] = {}
    section_timings_ms: Dict[str, float] = {}

    if "llm" in include:
        if not topos_id:
            section_errors["llm"] = "topos_id required"
        else:
            value, err, elapsed_ms = _run_sync_section(lambda: _build_llm_integrations_section(topos_id))
            _record_section(
                sections,
                section_errors,
                section_timings_ms,
                section_key="llm",
                result_key="llm_integrations",
                value=value,
                err=err,
                elapsed_ms=elapsed_ms,
            )

    if "install_status" in include:
        value, err, elapsed_ms = await _run_async_section(lambda: _list_install_status_core(payload))
        _record_section(
            sections,
            section_errors,
            section_timings_ms,
            section_key="install_status",
            result_key="install_status",
            value=value,
            err=err,
            elapsed_ms=elapsed_ms,
        )

    body: Dict[str, Any] = {
        "status": "ok",
        **sections,
        "section_timings_ms": section_timings_ms,
    }
    if section_errors:
        body["section_errors"] = section_errors
    return body

