"""S5 (PLAN_QUERY_LOOP.md) — the derived-layer scope vocabulary.

protects: the derived layer has grant words — every capability step downstream
(graph lane, fact projection, derived index) hangs authorization on these five
entries — and the words are registered WITHOUT entering routing: stub scopes
resolve manifests for deliberate calls but never become classifier targets,
so an unreadable scope cannot become a routed-empty false-absence turn.

Owner sign-off 2026-09-02: graph:read at raw (owner-lane parity with the
social_graph analytics read); facts split standard/special mirroring the
facts vs facts_all packet distinction; dossiers deliberately absent pending
the net-subject write-path policy; persons/relationship_edges absent because
those tables are empty on live nodes (an empty-table scope is a false-absence
generator by construction — the August relationship_context lesson).
"""

from __future__ import annotations

import pytest

from topos.query.manifest_validation import resolve_scope_manifest

# scope_id -> (expected ceiling, expected tables reachable through the manifest)
S5_SCOPES = {
    "graph:read": ("raw", ["entities", "entity_edges"]),
    "facts:read": ("inference", []),
    "facts_sensitive:read": ("inference", []),
    "goals:read": ("inference", ["user_goals"]),
    "topics:read": ("summary", ["topic_clusters", "topic_cluster_members"]),
}


@pytest.mark.parametrize("scope_id", sorted(S5_SCOPES))
def test_manifest_resolves_with_signed_off_ceiling_and_tables(scope_id: str) -> None:
    ceiling, tables = S5_SCOPES[scope_id]
    manifest = resolve_scope_manifest(scope_id)
    assert manifest is not None, f"{scope_id} must resolve a manifest"
    assert manifest.access_mode_ceiling == ceiling, (
        f"{scope_id} ceiling drifted from the signed-off value {ceiling!r}"
    )
    assert sorted(manifest.canonical_tables) == sorted(tables), (
        f"{scope_id} tables drifted from the signed-off list"
    )


@pytest.mark.parametrize("scope_id", sorted(S5_SCOPES))
def test_client_cannot_escalate_the_signed_off_ceiling(scope_id: str) -> None:
    """A client manifest claiming a wider ceiling is REJECTED, not clamped —
    the registry is authoritative, and the resolver refuses rather than
    silently narrowing (escalation is an error worth surfacing)."""
    from topos.query.manifest_validation import ManifestValidationError

    ceiling, _ = S5_SCOPES[scope_id]
    if ceiling == "raw":
        pytest.skip("raw is the top mode; nothing above it to escalate to")
    with pytest.raises(ManifestValidationError):
        resolve_scope_manifest(scope_id, client_manifest={"access_mode_ceiling": "raw"})


def test_stub_scopes_stay_out_of_the_classifier() -> None:
    """implementation_status='stub' must never feed classifier prototypes —
    routing to a scope with no reader would manufacture routed-empty turns."""
    from topos.query import scope_classifier

    entries = scope_classifier._live_scope_entries() if hasattr(
        scope_classifier, "_live_scope_entries"
    ) else None
    if entries is None:
        # Fall back to the registry contract the classifier reads.
        import json
        from pathlib import Path

        registry = json.loads(
            (Path(__file__).resolve().parents[2] / "topos/query/scope_registry.json").read_text()
        )
        live_ids = {
            s["scope_id"]
            for s in registry["scopes"]
            if str(s.get("implementation_status", "")) == "live"
        }
    else:
        live_ids = {str(e.get("scope_id")) for e in entries}
    # S6/S7: graph:read and the facts pair flipped live WITH their readers
    # (graph lane; predicate-generic facts_direct). A registry demotion would
    # silently starve those lanes, so pin them into the live set; the two
    # remaining S5 stubs stay out.
    _live_flipped = {"graph:read", "facts:read", "facts_sensitive:read"}
    for flipped in sorted(_live_flipped):
        assert flipped in live_ids, (
            f"{flipped} demoted from the live set while its reader ships"
        )
    for scope_id in S5_SCOPES:
        if scope_id in _live_flipped:
            continue
        assert scope_id not in live_ids, (
            f"{scope_id} reached the live/classifier set while still a stub"
        )


def test_dossiers_and_empty_tables_stay_unscoped() -> None:
    """The two deliberate exclusions hold until their preconditions land:
    dossiers await the net-subject policy; persons/relationship_edges await
    real rows. A scope appearing for them here means someone skipped the
    precondition — red on purpose."""
    import json
    from pathlib import Path

    registry = json.loads(
        (Path(__file__).resolve().parents[2] / "topos/query/scope_registry.json").read_text()
    )
    for entry in registry["scopes"]:
        tables = list(entry.get("raw_tables") or []) + list(entry.get("canonical_tables") or [])
        assert "persons" not in tables and "relationship_edges" not in tables
        assert "dossier" not in str(entry.get("scope_id", ""))
        assert "entity_dossier" not in list(entry.get("signal_objects") or [])
