from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    status: str
    time: float
    cloud_connected: Optional[bool] = None
    control_plane_connection: Optional[Dict[str, Any]] = None
    sync_connection: Optional[Dict[str, Any]] = None


class GenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    max_tokens: Optional[int] = Field(None, ge=1, le=4096)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    provider: Optional[Literal["openai", "ollama"]] = None
    model: Optional[str] = Field(None, max_length=256)

    @field_validator("prompt")
    @classmethod
    def _strip_prompt(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Prompt cannot be empty")
        return v

    @field_validator("model")
    @classmethod
    def _strip_model(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s if s else None


class GenerationResponse(BaseModel):
    output: str
    model: str
    usage: dict


class OllamaModelsResponse(BaseModel):
    """Model names available on the Ollama server (see /api/tags)."""

    models: List[str]


class StoreMessageRequest(BaseModel):
    dataset_id: Optional[str] = None
    sender_type: str = Field(..., description="human, assistant, system, or tool")
    content: str = Field(..., min_length=1)
    message_id: Optional[str] = None
    ts: Optional[str] = None
    user_id: Optional[str] = None


class StoreMessageResponse(BaseModel):
    op_id: str
    message_id: str
    status: str


class PairingCodeResponse(BaseModel):
    pairing_code: str
    qr_data: str
    expires_in: int = 3600


class PairDeviceRequest(BaseModel):
    pairing_code: str
    keep_existing_data: bool = True


class PairDeviceResponse(BaseModel):
    status: str
    user_id: str
    message: str = "ok"


class SyncResponse(BaseModel):
    status: str
    message: str
    ops_synced: int = 0


class DeviceInfoResponse(BaseModel):
    user_id: Optional[str]
    dataset_id: Optional[str]
    sync_connected: bool
    sync_enabled: bool
    engine_class: str
    engine_mode: str
    llm_enabled: bool
    database_mode: str
    database_version: Optional[str] = None
    engine_name: Optional[str]
    engine_version: Optional[str] = None
    system: Dict[str, Any]
    last_sync_at: Optional[str] = None
    last_received_hlc_ts: Optional[str] = None
    last_received_op_id: Optional[str] = None
    oplog_count: Optional[int] = None
    oplog_bytes: Optional[int] = None
    ops_since_last_sync: Optional[int] = None
    oplog_bytes_since_last_sync: Optional[int] = None


class DeviceNameRequest(BaseModel):
    device_name: str = Field(..., min_length=1, max_length=64)


class DeviceNameResponse(BaseModel):
    status: str
    device_name: str


class ResetDatabaseResponse(BaseModel):
    status: str
    message: str
    tables_cleared: List[str]


class SyncDatabaseResponse(BaseModel):
    status: str
    message: str
    file_size_bytes: Optional[int] = None
