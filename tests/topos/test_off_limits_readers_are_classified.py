"""Third fix round: every reader of the Off-limits list in the node is listed here with the view it reads.

An entry the upgrade carried is read by every path that can answer another person and by no path that serves the
owner himself (ruling P). That only holds while every reader is one or the other on purpose, so this file is the
list: each module under `topos/` that reads the list, with what it is. A module that starts reading the list, or
stops, fails here until someone has said which view it reads, and why.

The default of every store read is EVERYONE, so a reader this list missed would read every entry: it could starve
one of the owner's own tools again (the re-check's finding), and could never show a carried person to anyone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import topos

pytestmark = pytest.mark.public

ROOT = Path(topos.__file__).parent
READS = re.compile(r"entity_blackholes|BlackholeStore\(|blackholed_name_terms|blackholed_entity_ids|"
                   r"pending_rebuild_names|off_limits_terms|BlackholeGuard\(|guard_from_message\(|guard_for\(|"
                   r"owner_ui_guard\(|is_entity_protected\(|EntityBoundary\(")

SHARE = "the share side: every entry, always (it asks for no view)"
OWN = "the owner's own processing: his view (`off_limits_view.for_own_processing`)"
REQUEST = "answers a request: the request's own view (`off_limits_view.for_request`), from its verified principal"
GUARD = "takes a guard built by its caller: the guard's view"
UPKEEP = "keeps the entries themselves right (ids after a merge, reap protection, the clock): every entry"
DOORS = "the owner's own doors on the list: every entry, shown with what waits"
STORE = "the list itself"

READERS = {
    # --- the share side -------------------------------------------------------------------------------------------
    "permissions_v2/entity_boundary.py": SHARE,
    "permissions_v2/message_evidence.py": SHARE,
    "permissions_v2/evidence_reviews.py": SHARE,
    "permissions_v2/evidence.py": SHARE,
    "permissions_v2/protection_clock.py": SHARE,
    "permissions_v2/automatic_message_review.py": SHARE,
    "permissions_v2/interest_family.py": SHARE,
    "permissions_v2/permitted_derivation.py": SHARE,
    "permissions_v2/refresh_loop.py": SHARE,
    "permissions_v2/search_index.py": SHARE,
    # --- the list, its doors and its upkeep -----------------------------------------------------------------------
    "storage/db/migrations/entity_blackhole_v1.py": STORE,
    "features/lifecycle/blackhole.py": STORE,
    "features/lifecycle/contact_excludes.py": STORE,
    "features/lifecycle/off_limits_list.py": DOORS,
    "api/signal.py": DOORS,
    "features/lifecycle/derived_scrub.py": UPKEEP,
    "features/lifecycle/exclusions.py": UPKEEP,
    "features/lifecycle/gc.py": UPKEEP,
    "features/lifecycle/record_protection.py": UPKEEP,
    "features/entities/resolver.py": UPKEEP,
    "features/entities/consolidation.py": UPKEEP,
    # --- readers that answer a request ----------------------------------------------------------------------------
    "features/lifecycle/blackhole_guard.py": REQUEST,
    "query/retrieval.py": REQUEST,
    "query/closeness.py": REQUEST,
    "query/aggregate.py": GUARD,
    "core/handlers/aggregate.py": GUARD,
    "core/handlers/messages.py": GUARD,
    "core/handlers/__init__.py": "the relay's inspection floor: every entry (it serves frames the node cannot place)",
    "core/handlers/signal_features.py": DOORS,
    # --- the node's own processing of the owner's data ------------------------------------------------------------
    "features/lifecycle/blackhole_llm.py": OWN,
    "features/lifecycle/blackhole_rebuild.py": OWN,
    "features/derivation/net_subject_policy.py": OWN,
    "features/derivation/person_reading.py": OWN,
    "features/derivation/synthesize.py": OWN,
    "features/entities/affinity.py": OWN,
    "features/entities/affinity_owner.py": OWN,
    "features/entities/affinity_quality.py": OWN,
    "features/entities/context_vectors.py": OWN,
    "features/entities/fact_materializer.py": OWN,
    "features/entities/graph_inputs.py": OWN,
    "features/signal/topic_clustering.py": OWN,
    "features/signal/cluster_labels.py": "takes the term set its caller read (topic_clustering): that set's view",
}


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_every_module_that_reads_the_list_is_listed_and_no_other():
    found = {str(path.relative_to(ROOT)) for path in ROOT.rglob("*.py") if READS.search(path.read_text(encoding="utf-8"))}
    assert found == set(READERS), {"reads the list and is not listed": sorted(found - set(READERS)),
                                   "listed and no longer reads it": sorted(set(READERS) - found)}


def test_each_reader_of_the_owners_own_view_asks_for_it_by_name():
    """A module listed as the owner's own processing reads through `for_own_processing` (or the OWNER view itself,
    in the clean-up); one that answers a request, through `for_request`."""
    for relative, kind in READERS.items():
        text = _source(relative)
        if kind == OWN:
            assert "for_own_processing" in text or "view=OWNER" in text or "carried_waiting_json" in text, relative
        if kind == REQUEST:
            assert "for_request" in text or "_off_limits_view()" in text, relative


def test_the_share_side_asks_for_no_view_and_builds_the_boundary_whole():
    """Nothing under `permissions_v2` names the owner's view, and `waiting=False` is passed in one place in the whole
    node: the legacy guard, for the owner's own client."""
    for path in sorted((ROOT / "permissions_v2").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "off_limits_view" not in text and "for_own_processing" not in text and "for_request" not in text, path.name
        assert "view=OWNER" not in text and "waiting=False" not in text, path.name
    passes = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*.py")
              if re.search(r"EntityBoundary\([^)]*waiting=", path.read_text(encoding="utf-8"))]
    assert passes == ["features/lifecycle/blackhole_guard.py"]


def test_no_reader_scans_a_term_set_by_hand_any_more():
    """The scan sites the re-check named, and the others like them, ask the store's own term set (`OffLimitsTerms`),
    which looks for a name anywhere and for a handle, a username or an id only as itself. A new bare
    `term in text` loop over Off-limits terms would bring the old reading back for identifiers."""
    for relative in ("query/retrieval.py", "query/aggregate.py", "features/lifecycle/blackhole_guard.py",
                     "features/lifecycle/blackhole_llm.py"):
        text = _source(relative)
        assert not re.search(r"any\(\s*(?:t|term)\s+(?:and\s+(?:t|term)\s+)?in\s+\w+(?:\.lower\(\))?\s+for\s+(?:t|term)\s+in\s+"
                             r"(?:terms|blocked_terms|self\._blocked_terms\(\))", text), relative
        assert ".found(" in text or ".found_in(" in text, relative
