"""ScopeResolutionManifest serialize/deserialize."""

from topos.query.manifest import ScopeResolutionManifest


def test_scope_resolution_manifest_round_trip() -> None:
    manifest = ScopeResolutionManifest(
        scope_id="activity:read",
        primary_dimensions=["Profile", "Interests"],
        signal_objects=["activity_tags"],
        canonical_tables=["activity_events"],
        summary_objects=["activity_summary"],
        inference_objects=["interest_scores"],
        access_mode_ceiling="summary",
        filter_manifest={"filters": []},
        must_not_retrieve=["journal_entries"],
    )
    payload = manifest.to_dict()
    restored = ScopeResolutionManifest.from_dict(payload)
    assert restored.scope_id == "activity:read"
    assert restored.primary_dimensions == ["Profile", "Interests"]
    assert restored.canonical_tables == ["activity_events"]
    assert restored.must_not_retrieve == ["journal_entries"]
