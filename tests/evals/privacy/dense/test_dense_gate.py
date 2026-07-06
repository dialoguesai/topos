"""§G.1 — the dense-artifact disclosure gate (the G.7 interlock, at the retrieval layer).

`_fact_disclosure_allowed` is the mechanical interlock: a dense artifact tagged
`disclosure="owner_only"` never leaves the owner tier unless the scope manifest's
`signal_objects` explicitly lists the artifact's grant. This is what makes stat rollups,
dossiers, and facts owner-only by default — the property every dense artifact must satisfy
before it is exposable beyond owner_raw.
"""

from __future__ import annotations

import pytest

from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import _OWNER_ONLY_GRANTS, _fact_disclosure_allowed

pytestmark = [pytest.mark.private]


def _manifest(signal_objects=None):
    return ScopeResolutionManifest(
        scope_id="activity:read", primary_dimensions=["Memory"],
        canonical_tables=[], signal_objects=list(signal_objects or []),
    )


def _fact(object_type, disclosure="owner_only"):
    return {"object_type": object_type, "disclosure": disclosure, "tag": "most active 02:00–04:00 weekdays"}


@pytest.mark.parametrize("object_type,grant", list(_OWNER_ONLY_GRANTS.items()))
def test_owner_only_artifact_blocked_for_grantee_without_grant(object_type, grant):
    fact = _fact(object_type)
    # No grant in the manifest → a grantee must not receive it.
    assert _fact_disclosure_allowed(fact, "default_disclosure", _manifest([])) is False


@pytest.mark.parametrize("object_type,grant", list(_OWNER_ONLY_GRANTS.items()))
def test_owner_always_sees_owner_only_artifacts(object_type, grant):
    assert _fact_disclosure_allowed(_fact(object_type), "owner_raw", _manifest([])) is True


@pytest.mark.parametrize("object_type,grant", list(_OWNER_ONLY_GRANTS.items()))
def test_grantee_allowed_only_when_scope_grants_the_artifact_type(object_type, grant):
    # Granting the SPECIFIC artifact's signal object unlocks it.
    assert _fact_disclosure_allowed(_fact(object_type), "default_disclosure", _manifest([grant])) is True


def test_granting_one_artifact_type_does_not_unlock_another():
    # A scope that grants stat_insights must NOT thereby expose dossiers.
    dossier = _fact("entity_dossier")  # needs "entity_dossiers"
    assert _fact_disclosure_allowed(dossier, "default_disclosure", _manifest(["stat_insights"])) is False


def test_non_owner_only_fact_always_allowed():
    # A fact not tagged owner_only flows normally (it went through the ordinary disclosure path).
    assert _fact_disclosure_allowed(_fact("stat_insight", disclosure="default"), "default_disclosure", _manifest([])) is True


def test_missing_disclosure_field_is_treated_as_open():
    # Defensive: a fact without the tag is not owner-only (it never claimed to be).
    assert _fact_disclosure_allowed({"object_type": "stat_insight"}, "default_disclosure", _manifest([])) is True
