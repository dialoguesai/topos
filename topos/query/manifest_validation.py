"""Server-side scope manifest resolution and client override rejection."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional

from .manifest import ScopeResolutionManifest
from .scope_registry_loader import LEGACY_SCOPE_IDS, get_scope_entry
from .types import MODE_RANK


class ManifestValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


from ..sources.registry import get_sources_by_scope


def _max_supported_access_mode(entry: Dict[str, Any]) -> str:
    modes: List[str] = []
    if entry.get("raw_tables"):
        modes.append("raw")
    if entry.get("summary_objects") or entry.get("signal_objects"):
        modes.append("summary")
    if entry.get("inference_objects"):
        modes.append("inference")
    if not modes:
        return "summary"
    return max(modes, key=lambda mode: MODE_RANK[mode])


def _read_grant_access_mode_ceiling(filter_manifest: Optional[Dict[str, Any]]) -> Optional[str]:
    if not filter_manifest or not isinstance(filter_manifest, dict):
        return None
    nested = filter_manifest.get("filter_manifest")
    manifest = nested if isinstance(nested, dict) else filter_manifest
    ceiling = str(manifest.get("access_mode_ceiling") or "").strip().lower()
    if ceiling in ("raw", "summary", "inference"):
        return ceiling
    return None


def _read_scope_table_allowlist(filter_manifest: Optional[Dict[str, Any]], scope_id: str) -> Optional[List[str]]:
    if not filter_manifest or not isinstance(filter_manifest, dict):
        return None
    nested = filter_manifest.get("filter_manifest")
    root = nested if isinstance(nested, dict) else filter_manifest
    raw = root.get("scope_table_allowlist")
    if not isinstance(raw, dict):
        return None
    tables = raw.get(scope_id)
    if not isinstance(tables, list):
        return None
    return [str(t).strip() for t in tables if str(t).strip()]


def _normalize_id_list(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in raw:
        sid = str(item or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _resolve_accessible_entity_cohorts(
    cohorts: List[str],
    db_conn: Optional[Any] = None,
) -> List[str]:
    """D-002 / C1: resolve membership cohort tokens → person entity ids.

    Without ``db_conn``, membership cohorts fail closed to ``[]`` (A8 aggregate
    permit still uses the audit ``accessible_entity_cohorts`` list). Pipeline
    re-applies resolvers with a live connection before selector checks.
    """
    from .cohort_resolvers import resolve_accessible_entity_cohorts

    return resolve_accessible_entity_cohorts(cohorts, db_conn)


def _entity_selector_policy_keys_present(filter_manifest: Optional[Dict[str, Any]]) -> bool:
    """True when the grant explicitly configured entity selector policy (even if empty).

    Missing keys = legacy unrestricted. Present empty list = deny named people.
    """
    if not filter_manifest or not isinstance(filter_manifest, dict):
        return False
    nested = filter_manifest.get("filter_manifest")
    nested_dict = nested if isinstance(nested, dict) else {}
    for key in ("accessible_entity_ids", "accessible_entity_cohorts"):
        if key in filter_manifest or key in nested_dict:
            return True
    return False


def _read_accessible_entity_policy(
    filter_manifest: Optional[Dict[str, Any]],
    db_conn: Optional[Any] = None,
) -> tuple[List[str], List[str], bool]:
    """Read grant siblings accessible_entity_ids / accessible_entity_cohorts (D-002).

    Stored on uma_permissions.filters next to filter_manifest (not inside pydantic
    FilterManifest — that strips unknown keys). Accept top-level on the filters blob
    or, for test convenience, nested under filter_manifest.

    Returns (merged_ids, cohorts, policy_active). Merged ids = enums ∪ resolve(cohorts)
    when ``db_conn`` is available (C1); otherwise enums only (fail closed).
    """
    if not filter_manifest or not isinstance(filter_manifest, dict):
        return [], [], False
    nested = filter_manifest.get("filter_manifest")
    nested_dict = nested if isinstance(nested, dict) else {}
    policy_active = _entity_selector_policy_keys_present(filter_manifest)

    def _get(key: str) -> Any:
        if key in filter_manifest:
            return filter_manifest.get(key)
        return nested_dict.get(key)

    explicit = _normalize_id_list(_get("accessible_entity_ids"))
    cohorts = _normalize_id_list(_get("accessible_entity_cohorts"))
    # Fail closed without an explicit db_conn; pipeline.apply_cohort_membership
    # widens at grant-apply time with the live connection.
    from_cohorts = _resolve_accessible_entity_cohorts(cohorts, db_conn)
    merged = _normalize_id_list([*explicit, *from_cohorts])
    return merged, cohorts, policy_active


def manifest_from_scope_entry(entry: Dict[str, Any]) -> ScopeResolutionManifest:
    source_ids = list(entry.get("default_source_ids") or [])
    single = entry.get("default_source_id")
    if single and single not in source_ids:
        source_ids.insert(0, str(single))
    scope_id = str(entry["scope_id"])
    for registry_source_id in get_sources_by_scope(scope_id):
        if registry_source_id not in source_ids:
            source_ids.append(registry_source_id)
    return ScopeResolutionManifest(
        scope_id=str(entry["scope_id"]),
        primary_dimensions=list(entry.get("primary_dimensions") or []),
        signal_objects=list(entry.get("signal_objects") or []),
        canonical_tables=list(entry.get("raw_tables") or entry.get("canonical_tables") or []),
        summary_objects=list(entry.get("summary_objects") or []),
        inference_objects=list(entry.get("inference_objects") or []),
        access_mode_ceiling=_max_supported_access_mode(entry),
        default_source_id=str(single) if single else (source_ids[0] if source_ids else None),
        default_source_ids=source_ids,
        must_not_retrieve=list(entry.get("must_not_retrieve") or []),
    )


def resolve_scope_manifest(
    scope_id: str,
    *,
    client_manifest: Optional[Dict[str, Any]] = None,
    filter_manifest: Optional[Dict[str, Any]] = None,
    db_conn: Optional[Any] = None,
) -> ScopeResolutionManifest:
    """
    Build authoritative manifest from engine scope registry.
    Client-supplied manifest fields that affect retrieval boundaries are ignored.

    Optional ``db_conn`` enables C1 cohort → entity-id resolution at grant-resolve
    time. Pipeline also re-applies resolvers before selector checks.
    """
    sid = (scope_id or "").strip()
    if not sid:
        raise ManifestValidationError("missing_scope", "scope_id is required")
    if sid in LEGACY_SCOPE_IDS:
        raise ManifestValidationError("legacy_scope", f"Legacy scope_id deprecated: {sid}")

    entry = get_scope_entry(sid)
    if entry is None:
        raise ManifestValidationError("unknown_scope", f"Unknown wiki scope_id: {sid}")

    manifest = manifest_from_scope_entry(entry)
    grant_ceiling = _read_grant_access_mode_ceiling(filter_manifest)
    if grant_ceiling:
        manifest = replace(manifest, access_mode_ceiling=grant_ceiling)
    allowlist = _read_scope_table_allowlist(filter_manifest, sid)
    if allowlist is not None:
        allowed = set(allowlist)
        filtered = [t for t in manifest.canonical_tables if t in allowed]
        manifest = replace(manifest, canonical_tables=filtered)
    if filter_manifest is not None:
        manifest = replace(manifest, filter_manifest=filter_manifest)
    entity_ids, entity_cohorts, policy_active = _read_accessible_entity_policy(
        filter_manifest, db_conn=db_conn
    )
    if policy_active:
        manifest = replace(
            manifest,
            accessible_entity_ids=entity_ids,
            accessible_entity_cohorts=entity_cohorts,
            entity_selector_policy_active=True,
        )

    if client_manifest:
        client_sid = str(client_manifest.get("scope_id") or "").strip()
        if client_sid and client_sid != sid:
            raise ManifestValidationError("scope_mismatch", f"scope_id mismatch: {client_sid} != {sid}")
        client_ceiling = str(
            client_manifest.get("access_mode_ceiling")
            or client_manifest.get("default_mode_ceiling")
            or ""
        ).strip()
        if client_ceiling:
            max_supported = _max_supported_access_mode(entry)
            req_rank = MODE_RANK.get(client_ceiling, 99)
            auth_rank = MODE_RANK.get(max_supported, 1)
            if req_rank > auth_rank:
                raise ManifestValidationError(
                    "ceiling_escalation",
                    f"Client ceiling {client_ceiling} exceeds supported ceiling {max_supported}",
                )

    return manifest
