from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_TOPOS_CONTROL_PLANE_URL = "wss://cp.logu3s.com/ws/engine"


class Settings(BaseSettings):
    """Topos settings sourced from environment."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    topos_key: Optional[str] = Field(None, env="TOPOS_KEY")
    openai_api_key: Optional[str] = Field(None, env="OPENAI_API_KEY")

    environment: str = Field("development", env="ENVIRONMENT")
    log_format: Optional[str] = Field(None, env="LOG_FORMAT")
    log_level: str = Field("DEBUG", env="LOG_LEVEL")

    openai_base_url: str = Field("https://api.openai.com/v1", env="OPENAI_BASE_URL")
    openai_model: str = Field("gpt-4o-mini", env="OPENAI_MODEL")
    red_pill_api_key: Optional[str] = Field(None, env="RED_PILL_API_KEY")
    red_pill_api_base: str = Field("https://api.redpill.ai/v1", env="RED_PILL_API_BASE")

    gt_cloud_api_key: Optional[str] = Field(None, env="GT_CLOUD_API_KEY")
    griptape_nodes_api_base_url: str = Field(
        "https://api.nodes.griptape.ai", env="GRIPTAPE_NODES_API_BASE_URL"
    )

    allowed_origins_raw: str = Field("http://localhost:3000", env="ALLOWED_ORIGINS")
    allowed_origin_regex: Optional[str] = Field(None, env="ALLOWED_ORIGIN_REGEX")
    enable_health_auth: bool = Field(False, env="ENABLE_HEALTH_AUTH")

    request_timeout_seconds: float = Field(20.0, env="REQUEST_TIMEOUT_SECONDS")
    openai_timeout_seconds: float = Field(15.0, env="OPENAI_TIMEOUT_SECONDS")
    connection_retry_initial_seconds: float = Field(1.0, env="CONNECTION_RETRY_INITIAL_SECONDS")
    connection_retry_max_seconds: float = Field(30.0, env="CONNECTION_RETRY_MAX_SECONDS")
    connection_retry_jitter_ratio: float = Field(0.2, env="CONNECTION_RETRY_JITTER_RATIO")
    connection_readiness_timeout_seconds: float = Field(15.0, env="CONNECTION_READINESS_TIMEOUT_SECONDS")
    wait_for_control_plane_on_startup: bool = Field(False, env="WAIT_FOR_CONTROL_PLANE_ON_STARTUP")
    wait_for_sync_on_startup: bool = Field(False, env="WAIT_FOR_SYNC_ON_STARTUP")
    control_plane_inbound_concurrency_limit: int = Field(16, env="CONTROL_PLANE_INBOUND_CONCURRENCY_LIMIT")
    control_plane_inbound_max_pending: int = Field(128, env="CONTROL_PLANE_INBOUND_MAX_PENDING")
    control_plane_presence_outbox_size: int = Field(64, env="CONTROL_PLANE_PRESENCE_OUTBOX_SIZE")
    sync_cursor_retry_attempts: int = Field(3, env="SYNC_CURSOR_RETRY_ATTEMPTS")
    sync_cursor_retry_delay_seconds: float = Field(0.5, env="SYNC_CURSOR_RETRY_DELAY_SECONDS")

    rate_limit_per_minute: int = Field(60, env="RATE_LIMIT_PER_MINUTE")
    topos_control_plane_url: Optional[str] = Field(
        DEFAULT_TOPOS_CONTROL_PLANE_URL, env="TOPOS_CONTROL_PLANE_URL"
    )
    control_plane_verify_ssl: bool = Field(True, env="CONTROL_PLANE_VERIFY_SSL")
    hosted_pool_lease_enabled: bool = Field(False, env="HOSTED_POOL_LEASE_ENABLED")
    hosted_pool_allow_static_key_in_cloud: bool = Field(
        False, env="HOSTED_POOL_ALLOW_STATIC_KEY_IN_CLOUD"
    )
    hosted_pool_enforce_lease_in_cloud: bool = Field(
        True, env="HOSTED_POOL_ENFORCE_LEASE_IN_CLOUD"
    )
    hosted_pool_lease_audience: Optional[str] = Field(None, env="HOSTED_POOL_LEASE_AUDIENCE")
    hosted_pool_lease_issue_path: str = Field(
        "/v1/system/pool-connectors/lease/issue", env="HOSTED_POOL_LEASE_ISSUE_PATH"
    )
    hosted_pool_lease_renew_path: str = Field(
        "/v1/system/pool-connectors/lease/renew", env="HOSTED_POOL_LEASE_RENEW_PATH"
    )
    hosted_pool_lease_revoke_path: str = Field(
        "/v1/system/pool-connectors/lease/revoke", env="HOSTED_POOL_LEASE_REVOKE_PATH"
    )
    hosted_pool_lease_pool_group: str = Field("default", env="HOSTED_POOL_LEASE_POOL_GROUP")
    hosted_pool_lease_renew_skew_seconds: int = Field(60, env="HOSTED_POOL_LEASE_RENEW_SKEW_SECONDS")

    engine_mode: str = Field("full", env="ENGINE_MODE")
    enable_llm: bool = Field(True, env="ENABLE_LLM")
    engine_transport_mode: str = Field("ws", env="ENGINE_TRANSPORT_MODE")
    engine_name: Optional[str] = Field(None, env="ENGINE_NAME")
    topos_compute_profile: str = Field("basic_hosted", env="TOPOS_COMPUTE_PROFILE")

    engine_ollama_base_url: str = Field("http://localhost:11434", env="ENGINE_OLLAMA_BASE_URL")
    ollama_query_model: str = Field("llama3.2:latest", env="TOPOS_OLLAMA_QUERY_MODEL")
    engine_default_provider: str = Field("huggingface", env="ENGINE_DEFAULT_PROVIDER")

    # Sanitization field-transforms via Ollama (see topos.config.sanitization_ollama + engine_config overrides)
    sanitization_ollama_enabled: bool = Field(False, env="SANITIZATION_OLLAMA_ENABLED")
    sanitization_ollama_host: Optional[str] = Field(None, env="SANITIZATION_OLLAMA_HOST")
    sanitization_ollama_default_model: str = Field("llama3.2", env="SANITIZATION_OLLAMA_DEFAULT_MODEL")
    sanitization_ollama_timeout_sec: float = Field(120.0, env="SANITIZATION_OLLAMA_TIMEOUT_SEC")
    ollama_list_timeout_sec: float = Field(10.0, env="OLLAMA_LIST_TIMEOUT_SEC")
    sanitization_ollama_auto_pull: bool = Field(True, env="SANITIZATION_OLLAMA_AUTO_PULL")
    sanitization_ollama_max_input_chars: int = Field(8000, env="SANITIZATION_OLLAMA_MAX_INPUT_CHARS")
    sanitization_ollama_model_pii_redaction: Optional[str] = Field(None, env="SANITIZATION_OLLAMA_MODEL_PII_REDACTION")
    sanitization_ollama_model_nsfw_sanitization: Optional[str] = Field(None, env="SANITIZATION_OLLAMA_MODEL_NSFW_SANITIZATION")
    sanitization_ollama_model_raw_to_summary: Optional[str] = Field(None, env="SANITIZATION_OLLAMA_MODEL_RAW_TO_SUMMARY")
    sanitization_ollama_model_raw_to_sentiment: Optional[str] = Field(None, env="SANITIZATION_OLLAMA_MODEL_RAW_TO_SENTIMENT")
    sanitization_ollama_model_third_party_anonymization: Optional[str] = Field(
        None, env="SANITIZATION_OLLAMA_MODEL_THIRD_PARTY_ANONYMIZATION"
    )
    sanitization_ollama_model_name_removal: Optional[str] = Field(None, env="SANITIZATION_OLLAMA_MODEL_NAME_REMOVAL")
    sanitization_ollama_model_contact_removal: Optional[str] = Field(None, env="SANITIZATION_OLLAMA_MODEL_CONTACT_REMOVAL")

    # Local PII redaction via Hugging Face openai/privacy-filter (see topos.sanitization.privacy_filter)
    privacy_filter_enabled: bool = Field(True, env="PRIVACY_FILTER_ENABLED")
    privacy_filter_model: str = Field("openai/privacy-filter", env="PRIVACY_FILTER_MODEL")
    privacy_filter_device: Optional[str] = Field(None, env="PRIVACY_FILTER_DEVICE")
    privacy_filter_max_input_chars: Optional[int] = Field(None, env="PRIVACY_FILTER_MAX_INPUT_CHARS")
    nsfw_classifier_enabled: bool = Field(True, env="NSFW_CLASSIFIER_ENABLED")
    nsfw_classifier_model: str = Field(
        "michellejieli/NSFW_text_classifier",
        env="NSFW_CLASSIFIER_MODEL",
    )
    nsfw_classifier_threshold: float = Field(0.5, env="NSFW_CLASSIFIER_THRESHOLD")
    nsfw_classifier_max_input_chars: int = Field(512, env="NSFW_CLASSIFIER_MAX_INPUT_CHARS")
    platform_privacy_via_engine: bool = Field(True, env="PLATFORM_PRIVACY_VIA_ENGINE")
    topos_engine_service_url: Optional[str] = Field(None, env="TOPOS_ENGINE_SERVICE_URL")

    topos_database_path: Optional[str] = Field(None, env="TOPOS_DATABASE_PATH")
    topos_database_mode: str = Field("local", env="TOPOS_DATABASE_MODE")
    topos_database_service_url: Optional[str] = Field(None, env="TOPOS_DATABASE_SERVICE_URL")
    topos_postgres_dsn: Optional[str] = Field(None, env="TOPOS_POSTGRES_DSN")
    topos_postgres_host: Optional[str] = Field(None, env="TOPOS_POSTGRES_HOST")
    topos_postgres_port: Optional[int] = Field(None, env="TOPOS_POSTGRES_PORT")
    topos_postgres_db: Optional[str] = Field(None, env="TOPOS_POSTGRES_DB")
    topos_postgres_user: Optional[str] = Field(None, env="TOPOS_POSTGRES_USER")
    topos_postgres_password: Optional[str] = Field(None, env="TOPOS_POSTGRES_PASSWORD")
    topos_postgres_reset_incompatible_schema: bool = Field(
        False, env="TOPOS_POSTGRES_RESET_INCOMPATIBLE_SCHEMA"
    )
    topos_default_dataset_id: str = Field("default", env="TOPOS_DEFAULT_DATASET_ID")
    topos_user_id: Optional[str] = Field(None, env="TOPOS_USER_ID")
    # Pooled read enforcement mode for hosted shared-tenancy engines.
    # "off" preserves legacy behavior; set to "pooled" to require tenant-scoped reads.
    topos_pool_mode: str = Field("off", env="TOPOS_POOL_MODE")

    scrub_min_embeddings_for_recluster: int = Field(3, env="SCRUB_MIN_EMBEDDINGS_FOR_RECLUSTER")
    scrub_sync_row_limit: int = Field(50000, env="SCRUB_SYNC_ROW_LIMIT")

    topos_sync_url: str = Field("wss://cp.logu3s.com/ws/sync", env="TOPOS_SYNC_URL")
    enable_sync: bool = Field(True, env="ENABLE_SYNC")

    @property
    def allowed_origins(self) -> List[str]:
        raw = self.allowed_origins_raw
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(o).strip() for o in raw if str(o).strip()]
        raw_str = str(raw).strip()
        if not raw_str:
            return []
        if raw_str.startswith("["):
            try:
                parsed = json.loads(raw_str)
                if isinstance(parsed, list):
                    return [str(o).strip() for o in parsed if str(o).strip()]
            except json.JSONDecodeError:
                pass
        return [o.strip() for o in raw_str.split(",") if o.strip()]

    def get_sync_url(self) -> str:
        """Get sync URL (defaults to wss://cp.logu3s.com/ws/sync)."""
        return self.topos_sync_url

    @property
    def control_plane_url(self) -> Optional[str]:
        return self.topos_control_plane_url

    @control_plane_url.setter
    def control_plane_url(self, value: Optional[str]) -> None:
        self.topos_control_plane_url = value

    @property
    def database_path(self) -> Optional[str]:
        return self.topos_database_path

    @database_path.setter
    def database_path(self, value: Optional[str]) -> None:
        self.topos_database_path = value

    @property
    def database_mode(self) -> str:
        return self.topos_database_mode

    @database_mode.setter
    def database_mode(self, value: str) -> None:
        self.topos_database_mode = value

    @property
    def database_service_url(self) -> Optional[str]:
        return self.topos_database_service_url

    @database_service_url.setter
    def database_service_url(self, value: Optional[str]) -> None:
        self.topos_database_service_url = value

    @property
    def postgres_dsn(self) -> Optional[str]:
        return self.topos_postgres_dsn

    @postgres_dsn.setter
    def postgres_dsn(self, value: Optional[str]) -> None:
        self.topos_postgres_dsn = value

    @property
    def postgres_host(self) -> Optional[str]:
        return self.topos_postgres_host

    @postgres_host.setter
    def postgres_host(self, value: Optional[str]) -> None:
        self.topos_postgres_host = value

    @property
    def postgres_port(self) -> Optional[int]:
        return self.topos_postgres_port

    @postgres_port.setter
    def postgres_port(self, value: Optional[int]) -> None:
        self.topos_postgres_port = value

    @property
    def postgres_db(self) -> Optional[str]:
        return self.topos_postgres_db

    @postgres_db.setter
    def postgres_db(self, value: Optional[str]) -> None:
        self.topos_postgres_db = value

    @property
    def postgres_user(self) -> Optional[str]:
        return self.topos_postgres_user

    @postgres_user.setter
    def postgres_user(self, value: Optional[str]) -> None:
        self.topos_postgres_user = value

    @property
    def postgres_password(self) -> Optional[str]:
        return self.topos_postgres_password

    @postgres_password.setter
    def postgres_password(self, value: Optional[str]) -> None:
        self.topos_postgres_password = value

    @property
    def postgres_reset_incompatible_schema(self) -> bool:
        return self.topos_postgres_reset_incompatible_schema

    @postgres_reset_incompatible_schema.setter
    def postgres_reset_incompatible_schema(self, value: bool) -> None:
        self.topos_postgres_reset_incompatible_schema = value

    @property
    def default_dataset_id(self) -> str:
        return self.topos_default_dataset_id

    @default_dataset_id.setter
    def default_dataset_id(self, value: str) -> None:
        self.topos_default_dataset_id = value

    @property
    def user_id(self) -> Optional[str]:
        return self.topos_user_id

    @user_id.setter
    def user_id(self, value: Optional[str]) -> None:
        self.topos_user_id = value

    @property
    def engine_pool_mode(self) -> str:
        return self.topos_pool_mode

    @engine_pool_mode.setter
    def engine_pool_mode(self, value: str) -> None:
        self.topos_pool_mode = value

    @property
    def sync_url(self) -> str:
        return self.topos_sync_url

    @sync_url.setter
    def sync_url(self, value: str) -> None:
        self.topos_sync_url = value

    @model_validator(mode="after")
    def _validate_topos_key_or_lease(self) -> "Settings":
        is_cloud_runtime = bool(
            os.getenv("K_SERVICE")
            or os.getenv("K_REVISION")
            or os.getenv("CLOUD_RUN_JOB")
        )
        lease_env_explicit = os.getenv("HOSTED_POOL_LEASE_ENABLED") is not None

        if is_cloud_runtime and self.topos_control_plane_url and not lease_env_explicit:
            # Cloud-hosted runtimes should default to lease-based connector identities
            # unless an operator explicitly sets HOSTED_POOL_LEASE_ENABLED.
            self.hosted_pool_lease_enabled = True

        if (
            is_cloud_runtime
            and self.topos_control_plane_url
            and self.topos_key
            and not self.hosted_pool_lease_enabled
            and self.hosted_pool_enforce_lease_in_cloud
            and not self.hosted_pool_allow_static_key_in_cloud
        ):
            raise ValueError(
                "Cloud runtime requires hosted pool lease mode by default. "
                "Set HOSTED_POOL_LEASE_ENABLED=true, or set "
                "HOSTED_POOL_ALLOW_STATIC_KEY_IN_CLOUD=true for break-glass static key mode."
            )

        if self.topos_key:
            return self
        if self.hosted_pool_lease_enabled and self.topos_control_plane_url:
            return self
        raise ValueError(
            "TOPOS_KEY is required unless HOSTED_POOL_LEASE_ENABLED=true and TOPOS_CONTROL_PLANE_URL is configured."
        )


settings = Settings()
