"""A runtime install of a bundled source must not shadow bundled lane policy.

The live node carried a 2026-05-29 source_runtime_installs row for
browser_visits snapshotting the then-bundled definition
(enrichment_trigger='manual', no enrichment jobs). Rehydrated into REGISTRY at
every boot, the snapshot shadowed the 2026-07-09 bundled flip to automatic
url_classification+embeddings — and once the 2026-07-06 manual gate shipped,
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
    "raw_enrichment_jobs": ["url_classification"],
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
        "signal_derivation_jobs": ["url_classification"],
        "default_scope_id": "activity",
        "allowed_scope_ids": ["activity:read", "activity:write"],
    }
    handle = install_source_definition(payload)
    try:
        installed = REGISTRY["custom_lane_policy_source"]
        assert installed.enrichment_trigger == "manual"
        assert list(installed.canonical_enrichment_jobs) == []
        assert list(installed.signal_derivation_jobs or []) == ["url_classification"]
    finally:
        handle.uninstall()
    assert "custom_lane_policy_source" not in REGISTRY
