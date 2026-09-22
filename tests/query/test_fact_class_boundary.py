"""G6: the fact sensitivity split has to be enforced, not merely described.

The registry splits derived facts in two (topos/query/scope_registry.json):

    facts:read            fact_classes = ["standard"]   "Excludes special-class
                                                         packs -- health, inner
                                                         state and finances live
                                                         behind facts_sensitive"
    facts_sensitive:read  fact_classes = ["special"]

Every other field of the two entries is identical: both declare
signal_objects/summary_objects/inference_objects = ["fact"], no raw tables and
no canonical tables. So `fact_classes` is the ONLY thing that distinguishes
them -- and before this change `fact_classes` appeared nowhere in the engine
except those two JSON lines. `ScopeResolutionManifest` had no such field, so a
`facts:read` grant and a `facts_sensitive:read` grant compiled to byte-identical
manifests and released byte-identical facts.

The owner-facing consequence: an owner who granted "everyday facts" was also
granting health, inner-state and money facts, and the consent card said
otherwise.

This battery pins the split at the two places it has to hold: the compiled
manifest carries the classes, and the fact gate uses them.
"""

from __future__ import annotations

import pytest

from topos.query.manifest_validation import resolve_scope_manifest

pytestmark = [pytest.mark.p0]


def _fact(sensitivity: str | None, *, disclosure: str = "scoped") -> dict:
    """A fact as the gate sees it: object_type, disclosure, sensitivity."""
    item = {"object_type": "fact", "disclosure": disclosure}
    if sensitivity is not None:
        item["sensitivity"] = sensitivity
    return item


def test_the_compiled_manifest_carries_the_scopes_declared_fact_classes():
    assert resolve_scope_manifest("facts:read").fact_classes == ["standard"]
    assert resolve_scope_manifest("facts_sensitive:read").fact_classes == ["special"]


def test_a_scope_that_declares_no_classes_is_unrestricted_not_empty():
    """Only the two fact scopes declare classes; every other scope is unchanged.

    An empty list must mean "this scope says nothing about fact classes", not
    "deny every class" -- otherwise messages:read would stop carrying the facts
    it carries today. The deny-all reading belongs to an explicitly empty
    *allowlist*, which no scope has.
    """
    from topos.query.retrieval import _fact_class_allowed

    manifest = resolve_scope_manifest("messages:read")
    assert manifest.fact_classes == []
    assert _fact_class_allowed(_fact("special"), manifest) is True
    assert _fact_class_allowed(_fact("personal"), manifest) is True


def test_facts_read_does_not_release_a_special_class_fact():
    from topos.query.retrieval import _fact_class_allowed

    manifest = resolve_scope_manifest("facts:read")
    assert _fact_class_allowed(_fact("special"), manifest) is False


@pytest.mark.parametrize("sensitivity", ["personal", "none", "", None])
def test_facts_read_still_releases_everything_it_advertises(sensitivity):
    from topos.query.retrieval import _fact_class_allowed

    manifest = resolve_scope_manifest("facts:read")
    assert _fact_class_allowed(_fact(sensitivity), manifest) is True


def test_facts_sensitive_read_is_the_scope_that_carries_the_special_class():
    from topos.query.retrieval import _fact_class_allowed

    manifest = resolve_scope_manifest("facts_sensitive:read")
    assert _fact_class_allowed(_fact("special"), manifest) is True


def test_the_two_fact_scopes_are_no_longer_the_same_grant():
    """The G6 statement itself: before the fix these two compared equal here."""
    from topos.query.retrieval import _fact_class_allowed

    standard = resolve_scope_manifest("facts:read")
    sensitive = resolve_scope_manifest("facts_sensitive:read")
    special = _fact("special")
    assert _fact_class_allowed(special, standard) != _fact_class_allowed(special, sensitive)


def test_the_owner_tier_is_not_narrowed_by_the_class_gate():
    """The gate bounds what a GRANT releases; the owner reads their own facts.

    `_fact_disclosure_allowed` already exempts owner_raw, and the full gate has
    to keep that shape or an owner asking their own node a health question
    through facts:read would get nothing.
    """
    from topos.query.retrieval import _fact_release_allowed

    manifest = resolve_scope_manifest("facts:read")
    assert _fact_release_allowed(_fact("special"), "owner_raw", manifest) is True
    assert _fact_release_allowed(_fact("special"), "default_disclosure", manifest) is False


def test_the_owner_only_disclosure_gate_still_applies_underneath():
    """Composition: class and disclosure are independent vetoes, both required."""
    from topos.query.retrieval import _fact_release_allowed

    manifest = resolve_scope_manifest("facts_sensitive:read")
    owner_only_special = _fact("special", disclosure="owner_only")
    # facts_sensitive:read declares signal_objects ["fact"], not "owner_facts",
    # so an owner_only fact stays withheld even though its class is allowed.
    assert _fact_release_allowed(owner_only_special, "default_disclosure", manifest) is False
    assert _fact_release_allowed(_fact("special"), "default_disclosure", manifest) is True
