from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from topos.config.local_model_builds import tag_for_this_machine

# Set before any huggingface_hub import during app/route loading.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


DEFAULT_TOPOS_CONTROL_PLANE_URL = "wss://cp.logu3s.com/ws/engine"

#: The 9B local model these defaults settle on, named in its PORTABLE build.
#: Resolved to this machine's build at Settings() time — Apple Silicon takes
#: the MLX build, every other platform takes this one. Hardcoding the MLX tag
#: is what made LLM fact extraction and the privacy judge ask Windows and Linux
#: nodes for a model that does not exist for them (local_model_builds).
DEFAULT_LOCAL_9B_MODEL = "qwen3.5:9b"


class Settings(BaseSettings):
    """Topos settings sourced from environment."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    topos_key: Optional[str] = Field(None)
    openai_api_key: Optional[str] = Field(None)

    environment: str = Field("development")
    log_format: Optional[str] = Field(None)
    log_level: str = Field("INFO")

    openai_base_url: str = Field("https://api.openai.com/v1")
    openai_model: str = Field("gpt-4o-mini")
    red_pill_api_key: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("RED_PILL_API_KEY", "REDPILL_API_KEY"),
    )
    red_pill_api_base: str = Field(
        "https://api.redpill.ai/v1",
        validation_alias=AliasChoices("RED_PILL_API_BASE", "REDPILL_API_BASE"),
    )

    gt_cloud_api_key: Optional[str] = Field(None)
    griptape_nodes_api_base_url: str = Field("https://api.nodes.griptape.ai")

    allowed_origins_raw: str = Field(
        "http://localhost:3000",
        # pydantic v2 ignores Field(env=); without the alias this field only
        # read ALLOWED_ORIGINS_RAW and the documented ALLOWED_ORIGINS was dead.
        validation_alias=AliasChoices("ALLOWED_ORIGINS", "ALLOWED_ORIGINS_RAW"),
    )
    allowed_origin_regex: Optional[str] = Field(None)
    enable_health_auth: bool = Field(False)

    request_timeout_seconds: float = Field(20.0)
    openai_timeout_seconds: float = Field(15.0)
    connection_retry_initial_seconds: float = Field(1.0)
    connection_retry_max_seconds: float = Field(30.0)
    connection_retry_jitter_ratio: float = Field(0.2)
    connection_readiness_timeout_seconds: float = Field(15.0)
    wait_for_control_plane_on_startup: bool = Field(False)
    wait_for_sync_on_startup: bool = Field(False)
    control_plane_inbound_concurrency_limit: int = Field(16)
    control_plane_inbound_max_pending: int = Field(128)
    control_plane_presence_outbox_size: int = Field(64)
    sync_cursor_retry_attempts: int = Field(3)
    sync_cursor_retry_delay_seconds: float = Field(0.5)

    rate_limit_per_minute: int = Field(60)
    topos_control_plane_url: Optional[str] = Field(DEFAULT_TOPOS_CONTROL_PLANE_URL)
    control_plane_verify_ssl: bool = Field(True)
    hosted_pool_lease_enabled: bool = Field(False)
    hosted_pool_allow_static_key_in_cloud: bool = Field(False)
    hosted_pool_enforce_lease_in_cloud: bool = Field(True)
    hosted_pool_lease_audience: Optional[str] = Field(None)
    hosted_pool_lease_issue_path: str = Field("/v1/system/pool-connectors/lease/issue")
    hosted_pool_lease_renew_path: str = Field("/v1/system/pool-connectors/lease/renew")
    hosted_pool_lease_revoke_path: str = Field("/v1/system/pool-connectors/lease/revoke")
    hosted_pool_lease_pool_group: str = Field("default")
    hosted_pool_lease_renew_skew_seconds: int = Field(60)

    engine_mode: str = Field("full")
    enable_llm: bool = Field(True)
    engine_transport_mode: str = Field("ws")
    engine_name: Optional[str] = Field(None)
    topos_compute_profile: str = Field("basic_hosted")

    engine_ollama_base_url: str = Field("http://localhost:11434")
    ollama_query_model: str = Field(
        "llama3.2:latest",
        validation_alias=AliasChoices("TOPOS_OLLAMA_QUERY_MODEL", "OLLAMA_QUERY_MODEL"),
    )
    # Ingest-time signal extraction (goals, dimension summaries) — separate from the fast
    # query-inference model because ingest is slow-tolerant and quality-critical (plan C1/E1):
    # llama3.2-3b drops ~49% of goal extractions to malformed JSON; a 9B model parses cleanly.
    # Empty string ⇒ fall back to ollama_query_model (the floor tier keeps the small model).
    ollama_extraction_model: str = Field(
        default_factory=lambda: tag_for_this_machine(DEFAULT_LOCAL_9B_MODEL),
        validation_alias=AliasChoices(
            "TOPOS_OLLAMA_EXTRACTION_MODEL", "OLLAMA_EXTRACTION_MODEL"
        ),
    )
    # B4 (PLAN_NODE_UPGRADE §B4 / PLAN_PROVENANCE_SPLIT P4.3): role-gated LLM fact
    # extraction — an ADDITIVE pass on top of the rules-only floor. Tri-state:
    # unset ⇒ auto (ON when ollama_extraction_model resolves to a non-empty model),
    # "0"/"off"/"false"/"no" ⇒ force off, anything else ⇒ force on. The extraction
    # JOB degrades to rules-only if Ollama is unreachable; it never crashes ingest.
    # Resolve with features.facts.llm_extract.facts_llm_enabled(), not by reading
    # this field directly (the auto default lives there).
    topos_facts_llm: Optional[str] = Field(None)
    # Owner-selectable model for the LLM fact pass. Empty ⇒ fall back to
    # ollama_extraction_model → ollama_query_model. A per-node device override
    # (engine_config["facts_llm_model"], set via /v1/facts-llm-config) wins over
    # this env default. Resolve with config.facts_llm.resolve_facts_llm_model();
    # thinking vs non-thinking is auto-detected per model by the Ollama adapter.
    facts_llm_model: str = Field(
        "",
        validation_alias=AliasChoices("TOPOS_FACTS_LLM_MODEL", "FACTS_LLM_MODEL"),
    )
    engine_default_provider: str = Field("huggingface")
    # §D minimizer runs on EVERY grantee query, so it uses a small/fast local model. The judge
    # only runs in nightly privacy evals (F.4/CER semantic scoring), so it can be larger/slower.
    disclosure_minimizer_model: str = Field(
        "llama3.2:latest",
        validation_alias=AliasChoices(
            "TOPOS_DISCLOSURE_MINIMIZER_MODEL", "DISCLOSURE_MINIMIZER_MODEL"
        ),
    )
    privacy_judge_model: str = Field(
        default_factory=lambda: tag_for_this_machine(DEFAULT_LOCAL_9B_MODEL),
        validation_alias=AliasChoices("TOPOS_PRIVACY_JUDGE_MODEL", "PRIVACY_JUDGE_MODEL"),
    )

    engine_max_resident_models: int = Field(3)
    engine_model_idle_ttl_sec: int = Field(0)
    engine_memory_rss_soft_limit_mb: int = Field(0)
    engine_flush_after_task: bool = Field(False)
    engine_ml_device: Optional[str] = Field(None)

    # Sanitization field-transforms via Ollama (see topos.config.sanitization_ollama + engine_config overrides)
    sanitization_ollama_enabled: bool = Field(False)
    sanitization_ollama_host: Optional[str] = Field(None)
    sanitization_ollama_default_model: str = Field("llama3.2")
    sanitization_ollama_timeout_sec: float = Field(120.0)
    # llm_generation / routine hops — must cover local thinking models. Do NOT
    # reuse sanitization_ollama_timeout_sec (120s): that cuts digest synthesis
    # mid-flight and surfaces as "Ollama unreachable".
    engine_ollama_generate_timeout_sec: float = Field(300.0)
    ollama_list_timeout_sec: float = Field(10.0)
    sanitization_ollama_auto_pull: bool = Field(True)
    sanitization_ollama_max_input_chars: int = Field(8000)
    sanitization_ollama_model_pii_redaction: Optional[str] = Field(None)
    sanitization_ollama_model_nsfw_sanitization: Optional[str] = Field(None)
    sanitization_ollama_model_raw_to_summary: Optional[str] = Field(None)
    sanitization_ollama_model_raw_to_sentiment: Optional[str] = Field(None)
    sanitization_ollama_model_third_party_anonymization: Optional[str] = Field(None)
    sanitization_ollama_model_name_removal: Optional[str] = Field(None)
    sanitization_ollama_model_contact_removal: Optional[str] = Field(None)

    # Local PII redaction via Hugging Face openai/privacy-filter (see topos.sanitization.privacy_filter)
    privacy_filter_enabled: bool = Field(True)
    privacy_filter_model: str = Field("openai/privacy-filter")
    privacy_filter_device: Optional[str] = Field(None)
    privacy_filter_max_input_chars: Optional[int] = Field(None)
    nsfw_classifier_enabled: bool = Field(True)
    nsfw_classifier_model: str = Field("michellejieli/NSFW_text_classifier")
    nsfw_classifier_threshold: float = Field(0.5)
    nsfw_classifier_max_input_chars: int = Field(512)
    sanitization_prewarm_on_startup: bool = Field(True)
    platform_privacy_via_engine: bool = Field(True)
    topos_engine_service_url: Optional[str] = Field(None)

    topos_database_path: Optional[str] = Field(None)
    # Packet resolution (PLAN_DERIVATION_LAYER.md): env default only; the per-database
    # engine_config value overrides. Validated against RESOLUTIONS at read time.
    topos_packet_resolution: str = Field("scores_only")
    # Minimum free disk the owner wants left on the volume Ollama writes models
    # to. Env default only; the per-node engine_config value overrides (the
    # General settings bar writes it). See topos/engine/disk_space.py.
    topos_min_free_disk_bytes: int = Field(10 * 1024**3)
    topos_database_mode: str = Field("local")
    topos_database_service_url: Optional[str] = Field(None)
    topos_postgres_dsn: Optional[str] = Field(None)
    topos_postgres_host: Optional[str] = Field(None)
    topos_postgres_port: Optional[int] = Field(None)
    topos_postgres_db: Optional[str] = Field(None)
    topos_postgres_user: Optional[str] = Field(None)
    topos_postgres_password: Optional[str] = Field(None)
    topos_postgres_reset_incompatible_schema: bool = Field(False)
    topos_default_dataset_id: str = Field("default")
    topos_user_id: Optional[str] = Field(None)
    # Pooled read enforcement mode for hosted shared-tenancy engines.
    # "off" preserves legacy behavior; set to "pooled" to require tenant-scoped reads.
    topos_pool_mode: str = Field("off")

    scrub_min_embeddings_for_recluster: int = Field(3)
    scrub_sync_row_limit: int = Field(50000)

    topos_sync_url: str = Field("wss://cp.logu3s.com/ws/sync")
    enable_sync: bool = Field(True)

    # P1.5 (PLAN_PROVENANCE_SPLIT): exposure-profile visibility. When True
    # (default), exposure-ledger stats ("you've been reading a lot about X") and
    # exposure-only interest items are surfaced but always attribution-labeled;
    # when False the owner has opted OUT and retrieval/composition must suppress
    # them. This env value is the node default; the per-node engine_config key
    # "exposure_profile_visible" (a bool the owner toggles from the UI) overrides
    # it — read both through exposure_profile_visible() below, never this field
    # directly, so the DB toggle is always honored.
    exposure_profile_visible: bool = Field(True)

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


# --- P1.5 exposure-profile visibility (PLAN_PROVENANCE_SPLIT) -------------------------
# Frozen engine_config key the UI toggle writes; a per-node bool the owner sets.
ENGINE_CONFIG_KEY_EXPOSURE_PROFILE_VISIBLE = "exposure_profile_visible"
ENGINE_CONFIG_KEY_PACKET_RESOLUTION = "packet_resolution"
# Owner-set floor for free disk space, in bytes. Written by Settings -> General;
# read by topos/engine/disk_space.py and the model manager.
ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES = "min_free_disk_bytes"

#: What the owner may set the floor to. Zero is allowed and means "do not hold
#: anything back"; the ceiling only exists so a typo cannot make every download
#: impossible forever.
MIN_FREE_DISK_BYTES_DEFAULT = 10 * 1024**3
MIN_FREE_DISK_BYTES_MIN = 0
MIN_FREE_DISK_BYTES_MAX = 1024**4  # 1 TB

_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "f", "no", "n", "off"})


def _coerce_bool(raw: object, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw).strip().lower()
    if not text:
        return default
    # engine_config values are JSON-ish strings; unwrap a JSON-quoted/boolean form.
    if text in _TRUE_STRINGS:
        return True
    if text in _FALSE_STRINGS:
        return False
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return default
    return _coerce_bool(parsed, default)


def _read_engine_config_value(conn, key: str) -> Optional[str]:
    """Read engine_config without importing topos.core.state (avoids circular
    imports); mirrors config/signal_extraction._read_engine_config_value."""
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            return str(row["value"])  # sqlite3.Row
        except (TypeError, IndexError, KeyError):
            return str(row[0])
    except Exception:  # noqa: BLE001 — missing table/row on fresh DB is not fatal
        return None


def resolve_exposure_profile_visible(settings_obj: "Settings", conn=None) -> bool:
    """Effective exposure-profile visibility: the per-node engine_config toggle
    ("exposure_profile_visible") when set, else the env/settings default (True).

    Callers in retrieval/composition read this before surfacing exposure-ledger
    stats or exposure-only interest items; False means the owner opted out and
    those artifacts must be suppressed (P1.5)."""
    default = bool(getattr(settings_obj, "exposure_profile_visible", True))
    raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_EXPOSURE_PROFILE_VISIBLE)
    if raw is None:
        return default
    return _coerce_bool(raw, default)


def resolve_packet_resolution(settings_obj: "Settings", conn=None) -> str:
    """Effective packet-resolution SETTING (before interlocks): the per-database
    engine_config value ("packet_resolution") when set and valid, else the
    env/settings default ("scores_only"). Interlocks (owner floor, model locality)
    live in query.packet_resolution.effective_packet_resolution — this returns
    only what the owner asked for."""
    valid = ("scores_only", "facts", "facts_all")
    default = str(getattr(settings_obj, "topos_packet_resolution", "") or "scores_only").strip().lower()
    if default not in valid:
        default = "scores_only"
    raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    return value if value in valid else default


def resolve_min_free_disk_bytes(settings_obj: "Settings", conn=None) -> int:
    """Free bytes the owner wants kept available on the models volume.

    The per-database engine_config value ("min_free_disk_bytes") when set and
    parseable, else the env/settings default (10 GB). Clamped to
    [MIN_FREE_DISK_BYTES_MIN, MIN_FREE_DISK_BYTES_MAX] so a bad value degrades to
    a usable floor rather than refusing every download for good.

    This is the number the disk check treats as the reserve and the number the
    model manager tries to keep the volume above by evicting models it can
    re-download.
    """
    default = _coerce_min_free_disk_bytes(
        getattr(settings_obj, "topos_min_free_disk_bytes", None),
        MIN_FREE_DISK_BYTES_DEFAULT,
    )
    raw = _read_engine_config_value(conn, ENGINE_CONFIG_KEY_MIN_FREE_DISK_BYTES)
    if raw is None:
        return default
    return _coerce_min_free_disk_bytes(raw, default)


def _coerce_min_free_disk_bytes(raw: object, default: int) -> int:
    """Bytes from a setting, env string, or engine_config value; `default` when
    it is not a number. Values outside the allowed range are clamped, not
    rejected — an owner who typed 900 TB meant "a lot", not "never download"."""
    if raw is None:
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    if value < MIN_FREE_DISK_BYTES_MIN:
        return MIN_FREE_DISK_BYTES_MIN
    if value > MIN_FREE_DISK_BYTES_MAX:
        return MIN_FREE_DISK_BYTES_MAX
    return value


def exposure_profile_visible(conn=None) -> bool:
    """Module-level convenience: resolve against the global settings singleton
    and (when not passed) the process DB connection. Retrieval passes its own
    query connection so multi-db verification runs read the right node."""
    if conn is None:
        try:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        except Exception:  # noqa: BLE001
            conn = None
    return resolve_exposure_profile_visible(settings, conn)
