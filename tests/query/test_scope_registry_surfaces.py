"""Every live scope must declare a surface retrieval can actually read.

Guards the regression the 2026-07-03 registry sync (bee78d5) introduced:
`public_bio:read` and `work_context:read` kept `implementation_status: "live"`
while their only declared surfaces were the summary objects `public_bio` and
`work_stub_summary` — names no migration creates and nothing in the package
produces. Retrieval never reads `summary_objects` (they only declare which
access modes the scope supports, via `_max_supported_access_mode`); the content
lanes are `manifest.canonical_tables` and `signal_objects`. `public_bio:read`
therefore returned `{"summaries": []}` for every query, in every environment,
and `work_context:read` could answer from goals but never from profile records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from topos.data_explorer_tables import CANONICAL_SCHEMA_TABLES
from topos.query.manifest_validation import resolve_scope_manifest

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "topos" / "query" / "scope_registry.json"


def _scopes() -> List[Dict[str, Any]]:
    with REGISTRY_PATH.open(encoding="utf-8") as fh:
        return list(json.load(fh)["scopes"])


def _live_scope_ids() -> List[str]:
    return [
        str(entry["scope_id"])
        for entry in _scopes()
        if str(entry.get("implementation_status") or "live") == "live"
    ]


@pytest.mark.parametrize("scope_id", _live_scope_ids())
def test_live_scope_declares_a_readable_lane(scope_id: str) -> None:
    """A live scope needs a canonical table or a signal object — not just a
    summary object. `summary_objects` names never reach a query: they set the
    access-mode ceiling and nothing else."""
    manifest = resolve_scope_manifest(scope_id)
    assert manifest.canonical_tables or manifest.signal_objects, (
        f"{scope_id} is advertised live but exposes no readable lane "
        f"(canonical_tables={manifest.canonical_tables}, signal_objects={manifest.signal_objects}, "
        f"summary_objects={manifest.summary_objects}). Either wire a lane or mark it "
        f"implementation_status: 'stub' upstream in control_plane/uma/scope_registry.json."
    )


def test_registry_tables_are_real_canonical_tables() -> None:
    """Every table a scope declares must name a table the canonical schema
    defines; a name outside it can never return rows. Both keys count —
    `raw_tables` also raises the scope's mode ceiling to `raw`, `canonical_tables`
    supplies the same lane without claiming raw support."""
    unknown = {}
    for entry in _scopes():
        declared = list(entry.get("raw_tables") or []) + list(entry.get("canonical_tables") or [])
        missing = [table for table in declared if table not in CANONICAL_SCHEMA_TABLES]
        if missing:
            unknown[str(entry["scope_id"])] = missing
    assert not unknown, f"scopes naming non-canonical tables: {unknown}"


@pytest.mark.parametrize("scope_id", ["public_bio:read", "work_context:read"])
def test_profile_scopes_stay_capped_at_summary(scope_id: str) -> None:
    """Reading `profile_records` must not hand these scopes raw disclosure.
    They carry it as `canonical_tables`, not `raw_tables`, precisely because
    `_max_supported_access_mode` treats a declared `raw_tables` entry as
    "supports raw mode" — which would silently lift both scopes past the
    `default_mode_ceiling` of `summary` they declare."""
    entry = next(e for e in _scopes() if e["scope_id"] == scope_id)
    assert not entry.get("raw_tables")
    assert entry["default_mode_ceiling"] == "summary"
    assert resolve_scope_manifest(scope_id).access_mode_ceiling == "summary"


@pytest.mark.parametrize("scope_id", ["public_bio:read", "work_context:read"])
def test_profile_scopes_expose_profile_records(scope_id: str) -> None:
    """The two profile-backed scopes read `profile_records` and authorize the
    resume source that fills it (harness rows P-01..P-04, W-03, N-03)."""
    manifest = resolve_scope_manifest(scope_id)
    assert "profile_records" in manifest.canonical_tables
    assert "demo_resume_file" in manifest.default_source_ids
