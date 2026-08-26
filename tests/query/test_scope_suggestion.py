"""A caller that names a scope that does not exist still gets its answer.

The home chat picks its own `scope_id` as free text and a small local model
invents plausible ones. Live 2026-08-26: "Who are my friends?" was sent as
scope_id "social_graph", the turn was DENIED before any retrieval, and the owner
was told "I don't have access to your contacts" while 1,386 of them sat in the
store.
"""

from topos.query.scope_registry_loader import (get_scope_entry, list_scopes,
                                               suggest_scope_id)


def test_an_invented_scope_snaps_to_the_subject_its_words_name():
    assert suggest_scope_id("social_graph", "Who are my friends?") == "relationship_context:read"
    assert suggest_scope_id("social_graph", "Who is in my close circle?") == "relationship_context:read"
    assert suggest_scope_id("wellness", "How did I sleep?") == "health:read"
    assert suggest_scope_id("calendar", "What is on my calendar?") == "schedule:read"


def test_a_bare_id_missing_its_action_resolves():
    assert suggest_scope_id("relationship_context", "") == "relationship_context:read"
    assert suggest_scope_id("health", "") == "health:read"


def test_a_near_miss_spelling_resolves():
    assert suggest_scope_id("relationship_contxt:read", "") == "relationship_context:read"


def test_a_valid_scope_is_never_rewritten():
    for entry in list_scopes():
        sid = str(entry.get("scope_id"))
        assert suggest_scope_id(sid, "anything") is None


def test_an_enrichment_only_scope_is_never_a_target():
    """`contacts:resolve` resolves identifiers for other lanes; it is not an
    answer surface, and the bare id "contacts" used to snap straight onto it."""
    assert get_scope_entry("contacts:resolve") is not None      # it IS a real scope
    assert suggest_scope_id("contacts", "Who are my friends?") == "relationship_context:read"
    for probe in ("contact", "contacts", "identifiers", "resolve"):
        assert suggest_scope_id(probe, "") != "contacts:resolve"


def test_nonsense_gets_no_suggestion_rather_than_a_wrong_one():
    assert suggest_scope_id("totally_unrelated_thing", "") is None
    assert suggest_scope_id("", "Who are my friends?") is None


def test_suggestion_never_invents_a_scope_outside_the_catalog():
    valid = {str(e.get("scope_id")) for e in list_scopes()}
    for probe in ("social_graph", "wellness", "calendar", "money", "places", "work"):
        got = suggest_scope_id(probe, "")
        assert got is None or got in valid
