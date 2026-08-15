"""Node-side cache of the control plane's model packs (PLAN_MODEL_PACKS.md M2).

The control plane is authoritative (control_plane/model_packs_storage.py);
this module's only job is to survive it being unreachable. Everything a
caller needs — `resolve_role_model` — answers from a cached copy in
`engine_config`, never the network. The one function that touches the
network, `sync_model_packs_from_control_plane`, is meant to be called from
the engine's existing heartbeat loop, not from the resolve path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Set, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from topos.config.settings import Settings

logger = logging.getLogger("topos.config.model_packs")

ENGINE_CONFIG_KEY_MODEL_PACKS_CACHE = "model_packs_cache"

#: Mirrors control_plane/model_pack_roles.py::ROLES. Duplicated rather than
#: (`scope` = scope routing: raw text → UMA scope set. Bound to the on-device
#: `scope-head` provider by default; NOT the extraction `classify` role.)
#: imported — the node and control plane are deployed independently — and
#: asserted against the control-plane copy in test_model_packs_cache.py.
ROLES: Tuple[str, ...] = ("primary", "reasoning", "tool", "classify", "scope")

#: The one non-LLM provider; only the `scope` role may bind it. The node serves it
#: from `topos.query.scope_head` (ModelSlot.SCOPE_HEAD), never through an LLM adapter.
SCOPE_HEAD_PROVIDER = "scope-head"

#: Mirrors control_plane/model_pack_roles.py::FUNCTION_ROLES for attribution.
FUNCTION_ROLES: Dict[str, str] = {
    "facts": "classify",
    "conversation_context": "classify",
    "signal_extraction": "tool",
    "sanitization": "tool",
    "routine_orchestration": "tool",
    "routine_synthesis": "primary",
    "home_chat": "primary",
}

#: Subtype / task labels → pack role for ledger attribution (S6).
SUBTYPE_ROLES: Dict[str, str] = {
    "fact_llm_extract": "classify",
    "facts": "classify",
    "conversation_context": "classify",
    "signal_extraction": "tool",
    "sanitization": "tool",
    "topic_extraction": "classify",
    "goal_extraction": "classify",
    "emotion_classification": "classify",
    "emo_27": "classify",
    "query_inference": "primary",
    "truth_verdict": "reasoning",
    "brief_update": "primary",
    "raw_to_summary": "primary",
}

SOURCE_OVERRIDE = "override"
SOURCE_PACK = "pack"
SOURCE_ENGINE_DEFAULT = "engine_default"

LOCAL_PROVIDERS = frozenset({"ollama"})

_SYNC_PATH = "/v1/topos/model-packs/sync"


@dataclass(frozen=True)
class ResolvedModel:
    """Node mirror of ``control_plane.model_pack_resolution.ResolvedModel``."""

    provider: str
    model: str
    source: str
    role: Optional[str] = None
    pack_id: Optional[str] = None

    def as_config(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if self.provider:
            out["provider"] = self.provider
        if self.model:
            out["model"] = self.model
        return out


class RoleBinding(BaseModel):
    """One role's model and the knobs it was bound with, as the CP sends it.

    The inference parameters are cached alongside the model rather than being
    looked up separately, for the reason `control_plane/model_pack_resolution.py`
    gives: a role's model and its settings are one decision, and a consumer that
    took the model from here and `thinking` from elsewhere could run a model at
    settings its owner attached to a different one.

    The wire spelling is camelCase (the control plane dumps by alias), but the
    node's own cache round-trips through `model_dump_json`, which writes field
    names — so both spellings must load or a restart would drop every parameter.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    provider: str
    model: str
    thinking: Optional[bool] = None
    effort: Optional[str] = None
    context: Optional[int] = None
    max_tokens: Optional[int] = Field(default=None, alias="maxTokens")


class CachedPack(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pack_id: str
    roles: Dict[str, RoleBinding] = Field(default_factory=dict)


class ModelPacksCache(BaseModel):
    """The shape stored under `engine_config[model_packs_cache]`."""

    model_config = ConfigDict(extra="ignore")

    revision: int = 0
    fetched_at: float = 0.0
    active: str = ""
    packs: Dict[str, CachedPack] = Field(default_factory=dict)


def _read_cache(conn: sqlite3.Connection) -> Optional[ModelPacksCache]:
    from ..core.state import get_engine_config_value

    raw = get_engine_config_value(conn, ENGINE_CONFIG_KEY_MODEL_PACKS_CACHE)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_packs_cache is not valid JSON, treating as cold: %s", exc)
        return None
    try:
        return ModelPacksCache.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_packs_cache failed validation, treating as cold: %s", exc)
        return None


def _write_cache(conn: sqlite3.Connection, cache: ModelPacksCache) -> None:
    from ..core.state import set_engine_config_value

    set_engine_config_value(conn, ENGINE_CONFIG_KEY_MODEL_PACKS_CACHE, cache.model_dump_json())


def cached_revision(conn: Optional[sqlite3.Connection]) -> int:
    """The revision this node last cached, or 0 for a cold cache."""
    if conn is None:
        return 0
    cache = _read_cache(conn)
    return cache.revision if cache is not None else 0


def resolve_active_pack_id(conn: Optional[sqlite3.Connection]) -> Optional[str]:
    """Active pack id from cache, or None when cold / missing."""
    if conn is None:
        return None
    cache = _read_cache(conn)
    if cache is None:
        return None
    active = str(cache.active or "").strip()
    return active or None


def active_pack_dict(conn: Optional[sqlite3.Connection]) -> Optional[Dict[str, Any]]:
    """Active pack as the dict shape ``resolve_model`` expects, or None."""
    if conn is None:
        return None
    cache = _read_cache(conn)
    if cache is None or not cache.active:
        return None
    pack = cache.packs.get(cache.active)
    if pack is None:
        return None
    roles: Dict[str, Any] = {}
    for role, binding in pack.roles.items():
        roles[role] = {
            "provider": binding.provider,
            "model": binding.model,
            **(
                {"thinking": binding.thinking}
                if binding.thinking is not None
                else {}
            ),
            **({"effort": binding.effort} if binding.effort is not None else {}),
            **({"context": binding.context} if binding.context is not None else {}),
            **(
                {"maxTokens": binding.max_tokens}
                if binding.max_tokens is not None
                else {}
            ),
        }
    return {"pack_id": pack.pack_id, "roles": roles}


def resolve_role_binding(
    conn: Optional[sqlite3.Connection], role: Any
) -> Optional[RoleBinding]:
    """The whole binding for `role` from the active cached pack, or None.

    Cache-only, by design: a missing connection, a cold cache, a network
    that's never been reachable, and an active pack that doesn't bind this
    role all answer None the same way — a caller always has exactly one
    fallback to reach for (its own engine default), rather than a different
    one per failure mode.
    """
    if conn is None:
        return None
    cache = _read_cache(conn)
    if cache is None or not cache.active:
        return None
    pack = cache.packs.get(cache.active)
    if pack is None:
        return None
    return pack.roles.get(str(role or "").strip().lower())


def resolve_role_model(conn: Optional[sqlite3.Connection], role: Any) -> Optional[Tuple[str, str]]:
    """`(provider, model)` for `role`, for callers that route on those alone."""
    binding = resolve_role_binding(conn, role)
    if binding is None:
        return None
    return (binding.provider, binding.model)


def _as_binding(raw: Any) -> Optional[Dict[str, str]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        model = raw.strip()
        return {"provider": "", "model": model} if model else None
    if isinstance(raw, Mapping):
        provider = str(raw.get("provider") or "").strip().lower()
        model = str(raw.get("model") or "").strip()
        if not provider and not model:
            return None
        return {"provider": provider, "model": model}
    return None


def _local_model_present(binding: Mapping[str, str], installed_local_models: Optional[Any]) -> bool:
    if installed_local_models is None:
        return True
    if str(binding.get("provider") or "").strip().lower() not in LOCAL_PROVIDERS:
        return True
    installed = {str(name or "").strip().lower() for name in installed_local_models}
    wanted = str(binding.get("model") or "").strip().lower()
    if wanted in installed:
        return True
    if ":" not in wanted and f"{wanted}:latest" in installed:
        return True
    if wanted.endswith(":latest") and wanted[: -len(":latest")] in installed:
        return True
    return False


#: Bound on first probe rather than at import: reaching `topos.engine.backends`
#: drags in the whole inference stack, and this module is imported by config
#: readers that must stay cheap. Tests patch this name.
OllamaAdapter: Any = None

#: Short enough that a model pulled a moment ago is usable without a restart,
#: long enough that a batch of a thousand rows does not open a thousand sockets.
INSTALLED_LOCAL_MODELS_TTL_SECONDS = 30.0

_installed_lock = threading.Lock()
_installed_cache: Optional[Tuple[float, Optional[Set[str]]]] = None


def reset_installed_local_models_cache() -> None:
    """Forget the probe result. Test seam, and the hook a fresh pull would use."""
    global _installed_cache
    with _installed_lock:
        _installed_cache = None


def installed_local_models() -> Optional[Set[str]]:
    """Tags this machine has actually pulled, or None when Ollama did not answer.

    None means "I did not check", which is not the same claim as the empty set.
    Demoting every local pack role because a probe hiccuped would change the
    owner's models on a transient failure, so callers treat None as "trust the
    pack" (see `_local_model_present`).
    """
    global _installed_cache
    now = time.monotonic()
    with _installed_lock:
        cached = _installed_cache
        if cached is not None and now - cached[0] < INSTALLED_LOCAL_MODELS_TTL_SECONDS:
            return cached[1]

    result: Optional[Set[str]]
    try:
        global OllamaAdapter
        if OllamaAdapter is None:
            from ..engine.backends.ollama import OllamaAdapter as _Adapter

            OllamaAdapter = _Adapter
        adapter = OllamaAdapter()
        if not adapter.is_reachable():
            result = None
        else:
            result = {str(name).strip().lower() for name in adapter.list_models() if name}
    except Exception as exc:  # noqa: BLE001 — an unanswered probe is not an empty machine
        logger.debug("installed local model probe failed: %s", exc)
        result = None

    with _installed_lock:
        _installed_cache = (now, result)
    return result


def resolve_model(
    *,
    role: Any,
    override: Any = None,
    pack: Any = None,
    engine_default: Any = None,
    installed_local_models: Optional[Any] = None,
) -> ResolvedModel:
    """Node mirror of CP precedence: override → pack role → engine default.

    Kept in lockstep with ``control_plane.model_pack_resolution.resolve_model``
    — the node cannot import the control plane, so the rule is written twice
    on purpose (S6). Change one, change the other.
    """
    resolved_role = str(role or "").strip().lower() or None
    if resolved_role and resolved_role not in ROLES:
        # Hop aliases used by routines.
        if resolved_role == "orchestration":
            resolved_role = "tool"
        elif resolved_role == "synthesis":
            resolved_role = "primary"
        else:
            resolved_role = None

    explicit = _as_binding(override)
    if explicit:
        return ResolvedModel(
            provider=explicit["provider"],
            model=explicit["model"],
            source=SOURCE_OVERRIDE,
            role=resolved_role,
        )

    binding: Optional[Dict[str, str]] = None
    pack_id: Optional[str] = None
    if resolved_role and isinstance(pack, Mapping):
        pack_id = str(pack.get("pack_id") or "") or None
        roles = pack.get("roles")
        if isinstance(roles, Mapping):
            raw = roles.get(resolved_role)
            if isinstance(raw, Mapping):
                provider = str(raw.get("provider") or "").strip().lower()
                model = str(raw.get("model") or "").strip()
                if provider and model:
                    binding = {"provider": provider, "model": model}

    if binding and _local_model_present(binding, installed_local_models):
        return ResolvedModel(
            provider=binding["provider"],
            model=binding["model"],
            source=SOURCE_PACK,
            role=resolved_role,
            pack_id=pack_id,
        )

    fallback = _as_binding(engine_default) or {}
    demoted_local = bool(binding) and binding["provider"] in LOCAL_PROVIDERS
    if demoted_local and fallback.get("provider", "") not in LOCAL_PROVIDERS:
        # Mirrors the CP: a role pinned to this machine whose tag is missing
        # stays on this machine. An engine default that is empty or someone
        # else's hardware would answer a failed download by moving the owner's
        # data off the box, which no code path may decide on its own.
        return ResolvedModel(
            provider=binding["provider"],
            model="",
            source=SOURCE_ENGINE_DEFAULT,
            role=resolved_role,
        )

    return ResolvedModel(
        provider=fallback.get("provider", ""),
        model=fallback.get("model", ""),
        source=SOURCE_ENGINE_DEFAULT,
        role=resolved_role,
    )


def resolve_for_function(
    function_key: str,
    *,
    override: Any = None,
    pack: Any = None,
    engine_default: Any = None,
    installed_local_models: Optional[Any] = None,
) -> ResolvedModel:
    role = FUNCTION_ROLES.get(str(function_key or "").strip().lower())
    return resolve_model(
        role=role,
        override=override,
        pack=pack,
        engine_default=engine_default,
        installed_local_models=installed_local_models,
    )


def attribution_for_engine_call(
    conn: Optional[sqlite3.Connection],
    *,
    subtype: Optional[str] = None,
    task_type: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """``(pack_id, role)`` for a node-side generation, from the cached pack."""
    pack_id = resolve_active_pack_id(conn)
    key = str(subtype or "").strip().lower()
    role = SUBTYPE_ROLES.get(key)
    if role is None:
        role = FUNCTION_ROLES.get(key)
    if role is None and str(task_type or "").strip().lower() == "enrichment":
        role = "classify"
    if pack_id and role is None:
        role = "primary"
    return pack_id, role


def _parse_sync_payload(body: Dict[str, Any]) -> ModelPacksCache:
    packs: Dict[str, CachedPack] = {}
    for row in body.get("packs") or []:
        pack_id = str(row.get("pack_id") or "").strip()
        if not pack_id:
            continue
        roles: Dict[str, RoleBinding] = {}
        for role, binding in (row.get("roles") or {}).items():
            if not isinstance(binding, dict):
                continue
            provider = str(binding.get("provider") or "").strip()
            model = str(binding.get("model") or "").strip()
            if not provider or not model:
                continue
            try:
                # Validated from the payload rather than rebuilt field by field:
                # a rebuild silently drops every parameter the control plane
                # learns to send that this node predates.
                parsed = RoleBinding.model_validate({**binding, "provider": provider, "model": model})
            except Exception as exc:  # noqa: BLE001
                logger.warning("dropping unparseable role binding %s: %s", role, exc)
                continue
            roles[str(role).strip().lower()] = parsed
        packs[pack_id] = CachedPack(pack_id=pack_id, roles=roles)
    return ModelPacksCache(
        revision=int(body.get("revision") or 0),
        fetched_at=time.time(),
        active=str(body.get("active") or ""),
        packs=packs,
    )


def apply_sync_payload(conn: sqlite3.Connection, body: Dict[str, Any]) -> bool:
    """Write an already-fetched sync payload to cache if its revision moved.

    Split out from the HTTP call so the "equal revision does nothing, a
    different one overwrites" behavior is testable without a network. Returns
    whether it wrote.
    """
    incoming_revision = int(body.get("revision") or 0)
    if incoming_revision == cached_revision(conn):
        return False
    _write_cache(conn, _parse_sync_payload(body))
    return True


def _control_plane_http_base(settings: "Settings") -> str:
    base = str(getattr(settings, "topos_control_plane_url", "") or "").strip()
    if base.startswith("wss://"):
        base = base.replace("wss://", "https://")
    elif base.startswith("ws://"):
        base = base.replace("ws://", "http://")
    return base.split("/ws/")[0].rstrip("/")


async def fetch_model_packs_sync_payload(
    settings: "Settings",
    *,
    timeout_seconds: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """Raw GET against the control plane's sync endpoint, or None on any failure.

    Mirrors the request shape `engine/usage_guard.py` already uses for
    node-to-control-plane REST calls: the node's own engine key as a bearer
    token, no topos_id in the URL (the control plane resolves that from the
    key — the node itself does not track its own topos_id).
    """
    control_plane_http = _control_plane_http_base(settings)
    api_key = str(getattr(settings, "topos_key", "") or "").strip()
    if not control_plane_http or not api_key:
        return None
    url = f"{control_plane_http}{_SYNC_PATH}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("model-packs sync fetch failed, keeping cached copy: %s", exc)
        return None


async def sync_model_packs_from_control_plane(
    settings: "Settings",
    conn: Optional[sqlite3.Connection],
    *,
    timeout_seconds: float = 10.0,
) -> bool:
    """Fetch-and-cache in one call, for the heartbeat loop. Never raises.

    An unreachable control plane, a missing connection, or a malformed
    response all leave the existing cache untouched — `resolve_role_model`
    already treats a cold cache as "fall through to the engine default", so
    there is nothing more defensive to do here than log and return False.
    """
    if conn is None:
        return False
    body = await fetch_model_packs_sync_payload(settings, timeout_seconds=timeout_seconds)
    if body is None:
        return False
    try:
        return apply_sync_payload(conn, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model-packs sync payload malformed, keeping cached copy: %s", exc)
        return False
