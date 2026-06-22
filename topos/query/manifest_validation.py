"""Server-side scope manifest resolution and client override rejection."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Optional

from .manifest import ScopeResolutionManifest
from .scope_registry_loader import LEGACY_SCOPE_IDS, get_scope_entry
from .types import MODE_RANK


class ManifestValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


from ..sources.registry import get_sources_by_scope


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
        access_mode_ceiling=str(
            entry.get("default_mode_ceiling") or entry.get("access_mode_ceiling") or "summary"
        ),
        default_source_id=str(single) if single else (source_ids[0] if source_ids else None),
        default_source_ids=source_ids,
        must_not_retrieve=list(entry.get("must_not_retrieve") or []),
    )


def resolve_scope_manifest(
    scope_id: str,
    *,
    client_manifest: Optional[Dict[str, Any]] = None,
    filter_manifest: Optional[Dict[str, Any]] = None,
) -> ScopeResolutionManifest:
    """
    Build authoritative manifest from engine scope registry.
    Client-supplied manifest fields that affect retrieval boundaries are ignored.
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
    if filter_manifest is not None:
        manifest = replace(manifest, filter_manifest=filter_manifest)

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
            req_rank = MODE_RANK.get(client_ceiling, 99)
            auth_rank = MODE_RANK.get(manifest.access_mode_ceiling, 1)
            if req_rank > auth_rank:
                raise ManifestValidationError(
                    "ceiling_escalation",
                    f"Client ceiling {client_ceiling} exceeds registry ceiling {manifest.access_mode_ceiling}",
                )

    return manifest
