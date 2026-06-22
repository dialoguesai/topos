"""Signal extraction LLM provider + model: env defaults + device DB overrides."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from topos.config.settings import Settings

logger = logging.getLogger("topos.config.signal_extraction")

ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE = "signal_extraction_device"

SignalExtractionProvider = Literal["ollama", "platform", "openai", "redpill"]
SIGNAL_EXTRACTION_PROVIDERS: frozenset[str] = frozenset({"ollama", "platform", "openai", "redpill"})


class DeviceSignalExtractionOverrides(BaseModel):
    """Partial overrides stored on device (engine_config JSON)."""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(1, ge=1)
    provider: Optional[SignalExtractionProvider] = None
    query_model: Optional[str] = None


def _read_engine_config_value(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return str(row[0] if not isinstance(row, sqlite3.Row) else row["value"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("engine_config read failed for %s: %s", key, exc)
        return None


def parse_device_overrides_json(raw: Optional[str]) -> DeviceSignalExtractionOverrides:
    if not raw or not str(raw).strip():
        return DeviceSignalExtractionOverrides()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return DeviceSignalExtractionOverrides()
        return DeviceSignalExtractionOverrides.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Invalid signal_extraction device JSON, ignoring: %s", exc)
        return DeviceSignalExtractionOverrides()


def resolve_signal_extraction_config(
    settings: Settings,
    conn: Optional[sqlite3.Connection],
) -> DeviceSignalExtractionOverrides:
    device = DeviceSignalExtractionOverrides()
    if conn is not None:
        raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE)
        device = parse_device_overrides_json(raw)
    provider = str(device.provider or "ollama").strip().lower()
    if provider not in SIGNAL_EXTRACTION_PROVIDERS:
        provider = "ollama"
    model_override = str(device.query_model or "").strip()
    default_model = str(settings.ollama_query_model or "llama3.2:latest").strip()
    if provider == "platform":
        default_model = str(settings.openai_model or "gpt-4o-mini").strip()
    elif provider == "redpill":
        from ..engine.backends.redpill import DEFAULT_REDPILL_MODEL

        default_model = DEFAULT_REDPILL_MODEL
    elif provider == "openai":
        default_model = str(settings.openai_model or "gpt-4o-mini").strip()
    return DeviceSignalExtractionOverrides(
        version=device.version,
        provider=provider,  # type: ignore[arg-type]
        query_model=model_override or default_model,
    )


def resolve_signal_extraction_query_model(
    settings: Settings,
    conn: Optional[sqlite3.Connection],
) -> str:
    return str(resolve_signal_extraction_config(settings, conn).query_model or "").strip()


def resolve_signal_extraction_model_request(
    settings: Settings,
    conn: Optional[sqlite3.Connection],
) -> Tuple[str, Optional[str]]:
    """Return (engine_provider, model) for enrichment engine tasks."""
    cfg = resolve_signal_extraction_config(settings, conn)
    provider = str(cfg.provider or "ollama").strip().lower()
    model = str(cfg.query_model or "").strip() or None
    if provider == "platform":
        return "openai", model or settings.openai_model
    if provider == "openai":
        return "openai", model or settings.openai_model
    if provider == "redpill":
        from ..engine.backends.redpill import resolve_redpill_model

        return "redpill", resolve_redpill_model(model)
    return "ollama", model or settings.ollama_query_model


def get_signal_extraction_query_model() -> str:
    from ..config.settings import settings
    from ..core.state import get_db_connection

    return resolve_signal_extraction_query_model(settings, get_db_connection())


def get_signal_extraction_model_request() -> Tuple[str, Optional[str]]:
    from ..config.settings import settings
    from ..core.state import get_db_connection

    return resolve_signal_extraction_model_request(settings, get_db_connection())


def get_signal_extraction_provider() -> str:
    from ..config.settings import settings
    from ..core.state import get_db_connection

    return str(resolve_signal_extraction_config(settings, get_db_connection()).provider or "ollama")


def effective_config_for_api(settings: Settings, conn: Optional[sqlite3.Connection]) -> dict[str, Any]:
    effective = resolve_signal_extraction_config(settings, conn)
    device_obj: Optional[DeviceSignalExtractionOverrides] = None
    if conn is not None:
        raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_SIGNAL_EXTRACTION_DEVICE)
        device_obj = parse_device_overrides_json(raw)
    return {
        "defaults_from_settings": {
            "provider": "ollama",
            "query_model": settings.ollama_query_model,
        },
        "device_overrides": device_obj.model_dump(exclude_none=True) if device_obj else {},
        "effective": {
            "provider": effective.provider or "ollama",
            "query_model": effective.query_model or settings.ollama_query_model,
        },
    }


def normalize_put_device_overrides(body: dict[str, Any]) -> str:
    raw = body.get("device_overrides")
    if raw is None:
        raise ValueError("device_overrides is required")
    if not isinstance(raw, dict):
        raise ValueError("device_overrides must be an object")
    parsed = DeviceSignalExtractionOverrides.model_validate(raw)
    provider = str(parsed.provider or "ollama").strip().lower()
    if provider not in SIGNAL_EXTRACTION_PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(sorted(SIGNAL_EXTRACTION_PROVIDERS))}")
    parsed.provider = provider  # type: ignore[assignment]
    if provider == "platform":
        if parsed.query_model is not None and not str(parsed.query_model).strip():
            parsed.query_model = None
    elif not str(parsed.query_model or "").strip():
        raise ValueError("query_model is required for this provider")
    return json.dumps(parsed.model_dump(exclude_none=True))
