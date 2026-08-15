"""A runtime install of a bundled source must not shadow bundled lane policy.

The live node carried a 2026-05-29 source_runtime_installs row for
browser_visits snapshotting the then-bundled definition
(enrichment_trigger='manual', no enrichment jobs). Rehydrated into REGISTRY at
every boot, the snapshot shadowed the 2026-07-09 bundled flip to automatic
embeddings — and once the 2026-07-06 manual gate shipped,
every live browser push ran ZERO enrichment/signal jobs: August 2026 ended
0/1,034 visits embedded or url-classified, with 2,304 inbox jobs reporting
"done". browser_events (highlights), chatgpt_ui, imessage, signal, and
gcal_events installs were silently missing newer signal jobs the same way.

Lane policy (trigger + job lists) is owned by the bundled registry; per-source
user toggles belong in source_enrichment_overrides, which the lane resolvers
apply on top. Non-bundled sources keep their declared policy untouched.
"""

from __future__ import annotations

from topos.sources.registry import BUNDLED_REGISTRY, REGISTRY
from topos.sources.runtime_install import install_source_definition

# Field-for-field the live stale install row (2026-05-29), minus filter tiers.
STALE_BROWSER_VISITS_SNAPSHOT = {
    "source_id": "browser_visits",
    "display_name": "Browser Visits",
    "source_type": "ui_stream",
    "schema_id": "browser.visits.v1",
    "parser_id": "browser.visits.v1",
    "canonical_mapper_id": None,
    "canonical_group_id": None,
    "raw_enrichment_jobs": ["embeddings"],
    "canonical_enrichment_jobs": [],
    "enrichment_trigger": "manual",
    "ingestion_trigger": "automatic",
    "default_scope_id": "activity",
    "allowed_scope_ids": ["activity:read", "activity:write"],
}


def test_stale_bundled_install_adopts_bundled_lane_policy() -> None:
    handle = install_source_definition(dict(STALE_BROWSER_VISITS_SNAPSHOT))
    try:
        installed = REGISTRY["browser_visits"]
        bundled = BUNDLED_REGISTRY["browser_visits"]
        assert installed.enrichment_trigger == "automatic"
        assert list(installed.canonical_enrichment_jobs) == list(
            bundled.canonical_enrichment_jobs
        )
        assert "embeddings" in installed.canonical_enrichment_jobs
        assert list(installed.signal_derivation_jobs or []) == list(
            bundled.signal_derivation_jobs or []
        )
        assert list(installed.raw_enrichment_jobs or []) == list(
            bundled.raw_enrichment_jobs or []
        )
    finally:
        handle.uninstall()
    assert REGISTRY["browser_visits"] is BUNDLED_REGISTRY["browser_visits"]


def test_non_bundled_source_keeps_declared_lane_policy() -> None:
    payload = {
        "source_id": "custom_lane_policy_source",
        "display_name": "Custom Lane Policy",
        "source_type": "ui_stream",
        "schema_id": "browser.visits.v1",
        "parser_id": "browser.visits.v1",
        "enrichment_trigger": "manual",
        "canonical_enrichment_jobs": [],
        "signal_derivation_jobs": ["embeddings"],
        "default_scope_id": "activity",
        "allowed_scope_ids": ["activity:read", "activity:write"],
    }
    handle = install_source_definition(payload)
    try:
        installed = REGISTRY["custom_lane_policy_source"]
        assert installed.enrichment_trigger == "manual"
        assert list(installed.canonical_enrichment_jobs) == []
        assert list(installed.signal_derivation_jobs or []) == ["embeddings"]
    finally:
        handle.uninstall()
    assert "custom_lane_policy_source" not in REGISTRY


# Field-for-field the live github_activity install row (active on the node
# 2026-08-14), minus filter tiers: taken before canonical_field_map existed.
STALE_GITHUB_ACTIVITY_SNAPSHOT = {
    "source_id": "github_activity",
    "display_name": "GitHub Activity",
    "source_type": "ui_stream",
    "delivery": "client_push",
    "schema_id": "github.activity.v1",
    "parser_id": "github.activity.v1",
    "canonical_mapper_id": "github_activity",
    "canonical_group_id": "activity",
    "raw_enrichment_jobs": [],
    "canonical_enrichment_jobs": [],
    "enrichment_trigger": "automatic",
    "ingestion_trigger": "automatic",
    "default_scope_id": "activity",
    "allowed_scope_ids": ["activity:read", "activity:write"],
}


def test_stale_bundled_install_adopts_bundled_field_map() -> None:
    """A snapshot that predates a declaration must not map less than the build.

    Live failure this pins: the active github_activity install was written
    before `canonical_field_map`, so the shipped declaration
    (activity_events.content <- payload.commits[*].message) never reached the
    mapper. A reprocess re-mapped all 458 push rows and wrote 458 NULL
    contents — no error, no warning, the fix simply absent.
    """
    handle = install_source_definition(dict(STALE_GITHUB_ACTIVITY_SNAPSHOT))
    try:
        installed = REGISTRY["github_activity"]
        bundled = BUNDLED_REGISTRY["github_activity"]
        assert installed.canonical_field_map == bundled.canonical_field_map
        assert installed.canonical_field_map["activity_events"]["content"] == {
            "path": "payload.commits[*].message",
            "join": "\n\n",
        }
    finally:
        handle.uninstall()
    assert REGISTRY["github_activity"] is BUNDLED_REGISTRY["github_activity"]


def test_installed_stale_snapshot_maps_commit_messages_end_to_end() -> None:
    """The adoption is only real if the mapper built from the install produces
    content — the registry field agreeing is not the same as the row landing."""
    from topos.canonicalization.declared_field_map import build_canonical_mapper
    from topos.ingestion.parsers.base import NormalizedRecord

    handle = install_source_definition(dict(STALE_GITHUB_ACTIVITY_SNAPSHOT))
    try:
        mapper = build_canonical_mapper(
            REGISTRY["github_activity"],
            default_table="activity_events",
            fallback_mapper_id="browser_activity",
        )
        record = NormalizedRecord(
            record_id="e1",
            payload={
                "id": "e1",
                "type": "PushEvent",
                "repo": {"name": "dialoguesai/topos"},
                "created_at": "2026-08-14T12:00:00Z",
                "payload": {"size": 1, "commits": [{"sha": "a" * 40, "message": "fix the thing"}]},
            },
        )
        activity = [r for r in mapper.map_many(record) if (r.table or "activity_events") == "activity_events"]
        assert activity[0].payload["content"] == "fix the thing"
    finally:
        handle.uninstall()
