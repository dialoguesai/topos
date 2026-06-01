"""
Sanitization LLM (Ollama) configuration: file/env defaults + device DB overrides.

Device overrides are stored in SQLite `engine_config` under
`ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE` as JSON (see DeviceSanitizationOllamaOverrides).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from topos.config.settings import Settings

logger = logging.getLogger("topos.config.sanitization_ollama")

# Transform IDs that use Ollama in topos.sanitization.ollama_transforms
SANITIZATION_OLLAMA_TRANSFORM_IDS: tuple[str, ...] = (
    "pii_redaction",
    "nsfw_sanitization",
    "raw_to_summary",
    "raw_to_sentiment",
    "third_party_anonymization",
    "name_removal",
    "contact_removal",
)

ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE = "sanitization_ollama_device"


class DeviceSanitizationOllamaOverrides(BaseModel):
    """Partial overrides stored on device (engine_config JSON). Omitted keys keep file/env defaults."""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(1, ge=1)
    enabled: Optional[bool] = None
    host: Optional[str] = None
    default_model: Optional[str] = None
    timeout_sec: Optional[float] = None
    max_input_chars: Optional[int] = None
    models: Optional[Dict[str, str]] = None


class SanitizationOllamaEffective(BaseModel):
    """Fully resolved config used at runtime (after merge)."""

    enabled: bool
    host: str
    default_model: str
    timeout_sec: float
    auto_pull: bool
    max_input_chars: int
    models: Dict[str, str]


def _settings_transform_model_map(settings: Any) -> Dict[str, Optional[str]]:
    """Map transform_id -> optional per-transform model from Settings."""
    return {
        "pii_redaction": getattr(settings, "sanitization_ollama_model_pii_redaction", None),
        "nsfw_sanitization": getattr(settings, "sanitization_ollama_model_nsfw_sanitization", None),
        "raw_to_summary": getattr(settings, "sanitization_ollama_model_raw_to_summary", None),
        "raw_to_sentiment": getattr(settings, "sanitization_ollama_model_raw_to_sentiment", None),
        "third_party_anonymization": getattr(settings, "sanitization_ollama_model_third_party_anonymization", None),
        "name_removal": getattr(settings, "sanitization_ollama_model_name_removal", None),
        "contact_removal": getattr(settings, "sanitization_ollama_model_contact_removal", None),
    }


def _read_engine_config_value(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Read engine_config without importing topos.core.state (avoids circular imports)."""
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return str(row[0] if not isinstance(row, sqlite3.Row) else row["value"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("engine_config read failed for %s: %s", key, exc)
        return None


def parse_device_overrides_json(raw: Optional[str]) -> DeviceSanitizationOllamaOverrides:
    if not raw or not str(raw).strip():
        return DeviceSanitizationOllamaOverrides()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return DeviceSanitizationOllamaOverrides()
        return DeviceSanitizationOllamaOverrides.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Invalid sanitization_ollama device JSON, ignoring: %s", exc)
        return DeviceSanitizationOllamaOverrides()


def resolve_sanitization_ollama_effective(
    settings: Settings,
    conn: Optional[sqlite3.Connection],
) -> SanitizationOllamaEffective:
    device = DeviceSanitizationOllamaOverrides()
    if conn is not None:
        raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE)
        device = parse_device_overrides_json(raw)

    enabled = settings.sanitization_ollama_enabled
    if device.enabled is not None:
        enabled = device.enabled

    host = (device.host or settings.sanitization_ollama_host or settings.engine_ollama_base_url or "").strip()
    if not host:
        host = "http://127.0.0.1:11434"

    default_model = (device.default_model or settings.sanitization_ollama_default_model or "llama3.2").strip()

    timeout_sec = float(device.timeout_sec if device.timeout_sec is not None else settings.sanitization_ollama_timeout_sec)
    auto_pull = bool(getattr(settings, "sanitization_ollama_auto_pull", True))
    max_input_chars = int(
        device.max_input_chars if device.max_input_chars is not None else settings.sanitization_ollama_max_input_chars
    )

    st_map = _settings_transform_model_map(settings)
    models: Dict[str, str] = {}
    for tid in SANITIZATION_OLLAMA_TRANSFORM_IDS:
        m: Optional[str] = None
        if device.models and tid in device.models and device.models[tid]:
            m = str(device.models[tid]).strip()
        if not m:
            sm = st_map.get(tid)
            if sm and str(sm).strip():
                m = str(sm).strip()
        if not m:
            m = default_model
        models[tid] = m

    return SanitizationOllamaEffective(
        enabled=enabled,
        host=host,
        default_model=default_model,
        timeout_sec=timeout_sec,
        auto_pull=auto_pull,
        max_input_chars=max_input_chars,
        models=models,
    )


def effective_config_for_api(settings: Settings, conn: Optional[sqlite3.Connection]) -> dict[str, Any]:
    """Payload for GET /v1/sanitization-ollama-config (no secrets)."""
    eff = resolve_sanitization_ollama_effective(settings, conn)
    device_raw = None
    device_obj: Optional[DeviceSanitizationOllamaOverrides] = None
    if conn is not None:
        device_raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_SANITIZATION_OLLAMA_DEVICE)
        device_obj = parse_device_overrides_json(device_raw)

    defaults = {
        "enabled": settings.sanitization_ollama_enabled,
        "host": settings.sanitization_ollama_host or settings.engine_ollama_base_url,
        "default_model": settings.sanitization_ollama_default_model,
        "timeout_sec": settings.sanitization_ollama_timeout_sec,
        "auto_pull": settings.sanitization_ollama_auto_pull,
        "max_input_chars": settings.sanitization_ollama_max_input_chars,
        "models": {k: v for k, v in _settings_transform_model_map(settings).items() if v},
    }
    return {
        "transform_ids": list(SANITIZATION_OLLAMA_TRANSFORM_IDS),
        "defaults_from_settings": defaults,
        "device_overrides": device_obj.model_dump(exclude_none=True) if device_obj else {},
        "effective": eff.model_dump(),
    }


def normalize_put_device_overrides(body: dict[str, Any]) -> str:
    """Validate and return JSON string for engine_config."""
    raw = body.get("device_overrides")
    if raw is None:
        raise ValueError("device_overrides is required")
    if not isinstance(raw, dict):
        raise ValueError("device_overrides must be an object")
    parsed = DeviceSanitizationOllamaOverrides.model_validate(raw)
    for tid, name in (parsed.models or {}).items():
        if tid not in SANITIZATION_OLLAMA_TRANSFORM_IDS:
            raise ValueError(f"Unknown transform_id in models: {tid!r}")
        if not str(name).strip():
            raise ValueError(f"Empty model for transform_id {tid!r}")
    return json.dumps(parsed.model_dump(exclude_none=True))
