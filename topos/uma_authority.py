"""Finite grant authority shared by UMA transport and direct HTTP readers."""
from __future__ import annotations

from typing import Any, Mapping, Optional
from .uma_resource_id import parse_dataset_id_from_uma_dataset_resource_id

MESSAGE_STREAM_SCOPES = {
    "conversation": frozenset({"messages:read", "all:read"}),
    "ai_chat": frozenset({"ai_conversations:read", "aiChat:read", "aiMessages:read", "all:read"}),
}


def message_stream_granted(scopes: Any, stream: str) -> bool:
    """Known explicit aliases only; coarse read never expands into message access."""
    required = MESSAGE_STREAM_SCOPES.get(stream)
    return bool(required and isinstance(scopes, list) and any(
        isinstance(scope, str) and scope.strip() in required for scope in scopes
    ))


def raw_table_projection_allowed(scopes: Any, manifest: Any, table: str) -> bool:
    """Apply persisted view/table obligations to each UMA raw reader.

    Table permission remains correlated with the scope which grants that table;
    an unconstrained AI scope must not erase an empty messages scope selection.
    Baseline scope/allowed_tables checks remain the responsibility of the entry
    point, including when no optional projection metadata exists.
    """
    if manifest is None:
        return True
    if manifest.access_mode_ceiling is not None and manifest.access_mode_ceiling != "raw":
        return False
    restrictions = manifest.scope_table_allowlist
    if restrictions is None:
        return True
    if not isinstance(scopes, list):
        return False
    aliases = {"messages": "conversation_messages", "ai_messages": "ai_chat_messages", "ai_chat": "ai_chat_messages"}
    canonical = aliases.get(table, table)
    for scope in scopes:
        if not isinstance(scope, str):
            continue
        if scope == "all:read":
            covers = True
        elif canonical in {"conversation_messages", "ai_chat_messages"}:
            covers = message_stream_granted([scope], "conversation" if canonical == "conversation_messages" else "ai_chat")
        else:
            from .query.manifest_validation import ManifestValidationError, resolve_scope_manifest
            try:
                covers = canonical in resolve_scope_manifest(scope).canonical_tables
            except ManifestValidationError:
                covers = False
        if covers and (scope not in restrictions or canonical in {aliases.get(t, t) for t in restrictions[scope]}):
            return True
    return False


def bound_uma_scope(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Resource identity is authoritative over optional caller-supplied hints."""
    resource_id = str(payload.get("resource_id") or "").strip()
    dataset = parse_dataset_id_from_uma_dataset_resource_id(resource_id)
    parts = resource_id.split(":")
    if not dataset or len(parts) < 4 or not parts[1].strip() or not parts[-1].strip():
        raise ValueError("resource_binding_required")
    owner = parts[1].strip()
    requested_dataset = str(payload.get("dataset_id") or "").strip()
    requested_owner = str(payload.get("owner_user_id") or "").strip()
    if (requested_dataset and requested_dataset != dataset) or (requested_owner and requested_owner != owner):
        raise ValueError("resource_binding_required")
    return dataset, owner


def dataset_scope_predicate(
    columns: set[str], dataset_id: Optional[str], owner_user_id: Optional[str],
    *, alias: str = "", whole_engine_scope: bool = False,
) -> tuple[str, tuple[Any, ...]]:
    """An owner or tenant predicate cannot substitute for the granted dataset.

    The alias is an internal constant, never request text. Caller code must
    apply this predicate before pagination or aggregation. Whole-engine scope
    is an internal decision from local_node_resource_scope(), never payload.
    """
    if whole_engine_scope:
        return "1 = 1", ()
    if not dataset_id or "dataset_id" not in columns:
        raise ValueError("dataset_scope_unavailable")
    prefix = f"{alias}." if alias else ""
    predicate = f'{prefix}"dataset_id" = ?'
    params: tuple[Any, ...] = (dataset_id,)
    if "owner_user_id" in columns:
        if not owner_user_id:
            raise ValueError("owner_scope_required")
        predicate += f' AND {prefix}"owner_user_id" = ?'
        params += (owner_user_id,)
    return predicate, params



def _is_local_node_connection(conn: Any) -> bool:
    import os
    import sqlite3
    from .config.settings import settings

    return (
        isinstance(conn, sqlite3.Connection)
        and str(settings.topos_database_mode).strip().lower() in {"local", "sqlite"}
        and str(settings.topos_pool_mode).strip().lower() == "off"
        and not settings.hosted_pool_lease_enabled
        and not any(os.getenv(key) for key in ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB"))
    )


def _persisted_node_owner(conn: Any) -> Optional[str]:
    import sqlite3

    try:
        # Read only: get_user_id() creates missing engine_config state.
        row = conn.execute("SELECT value FROM engine_config WHERE key = 'user_id'").fetchone()
        return str(row[0]).strip() if row and row[0] else None
    except sqlite3.Error:
        return None


def require_local_resource_binding(conn: Any, resource_id: str, owner_user_id: str) -> None:
    """An RPT for another owner's resource cannot authorize this local node.

    Hosted/pooled data is independently constrained by its dataset predicate.
    The local HTTP door additionally binds every resource to its persisted node
    owner, including custom resources whose message table has no owner column.
    """
    import hashlib
    from .config.settings import settings

    if not _is_local_node_connection(conn):
        return
    if _persisted_node_owner(conn) != owner_user_id:
        raise ValueError("resource_owner_mismatch")
    key = str(settings.topos_key or "").strip()
    device = hashlib.sha256(key.encode()).hexdigest()[:16] if key else None
    if not device or resource_id.rsplit(":", 1)[-1] != device:
        raise ValueError("resource_device_mismatch")


def local_node_resource_scope(conn: Any, resource_id: str, *, rpt_validated: bool = False) -> bool:
    """Recognize the registered local ENGINE resource, not a logical dataset.

    Direct nodes register owner:default:keyhash for their physical database;
    ingestion uses additional logical dataset IDs inside that same node. Only
    the verified CP relay or successful direct-HTTP RPT validation can exercise
    that resource. No request payload flag participates in this proof.
    """
    import hashlib
    from .config.settings import settings
    from .principal import current_principal

    principal = current_principal()
    if not rpt_validated and getattr(principal, "channel", None) != "cp_relay":
        return False
    if not _is_local_node_connection(conn):
        return False
    key = str(settings.topos_key or "").strip()
    if not key:
        return False
    try:
        _, owner = bound_uma_scope({"resource_id": resource_id})
    except ValueError:
        return False
    if _persisted_node_owner(conn) != owner:
        return False
    device = hashlib.sha256(key.encode()).hexdigest()[:16]
    return resource_id == f"dataset:{owner}:{owner}:default:{device}:{device}"
