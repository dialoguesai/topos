"""FT168 item 4: the census, the build and the owner's count say the same about facts, with the ontology on and off.

`scripts/permissions_v2/grant_census.py` is run on a copy of a fixture node, placed the way `census_copy.py` places
one (backups of the databases, byte copies of the rest) and read through the alias of the node's own path. Three
readings of the same share are compared: the facts the node's build made members of its index, the facts the census
makes members and how it classes the others, and the owner's count and preview.

Invented data only: six work facts, of which two are the owner's plain statements with a clean source.
"""
from __future__ import annotations

import collections
import hashlib
import shutil
import time
from pathlib import Path

import pytest

from tests.permissions_v2 import ft168_support as ft
from tests.permissions_v2.test_ft168_switch_loops import fact_members
from tests.permissions_v2.test_grant_census import _copy_layout, census_of, gc

SHARED = ("Owner works at Northwind.", "Owner works on Project Lantern.")


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A share of messages and facts with consent for Work and career. Facts: two shareable (clean sources that say
    them); one whose source is highly sensitive; one its source does not say; one the owner kept private; one stored
    under an ontology outside the release."""
    monkeypatch.setenv(ft.FACTS_SWITCH, "true")
    now = int(time.time())
    made = ft.build(tmp_path / "home", [
        ft.Message("I work at Northwind.", age=300),
        ft.Message("I work on Project Lantern.", age=600),
        ft.Message("I work on Project Harbor.", age=900, labels={"sensitivity": "special"}),
        ft.Message("Budget review for the dock team moved to Thursday", age=1200),
        ft.Message("I worked at Southgate.", age=1500),
        ft.Message("I work on Project Meridian.", age=1800),
        ft.Message("Vendor call about the launch checklist", age=2100)], now=now, policy=ft.policy_at(now))
    made.ids = {
        "shared_employer": ft.state_fact(made, "imessage:1", "works_at", "Northwind"),
        "shared_project": ft.state_fact(made, "imessage:2", "works_on", "Project Lantern"),
        "sensitive_source": ft.state_fact(made, "imessage:3", "works_on", "Project Harbor"),
        "not_what_the_source_says": ft.state_fact(made, "imessage:4", "works_on", "the dock team"),
        "kept_private": ft.state_fact(made, "imessage:5", "worked_at", "Southgate", disclosure="owner_only"),
        "another_ontology": ft.pack_fact(made, "imessage:6", ontology_id="health.mental", version="0.2.3",
                                         predicate="works_on", value="Project Meridian"),
    }
    ft.seed_registry(made)
    assert made.rebuild()[ft.GRANT] == "ready"
    yield made
    ft.close(made)


def census_on_a_copy(node, tmp_path):
    """The census of a fresh copy of the node's stores, read as the node through the alias of its own path."""
    target = tmp_path / "copy"
    if target.exists():
        shutil.rmtree(target)
    copy = _copy_layout(tmp_path, node)
    return census_of(node, canonical=copy / Path(node.index.resolver.path).name, live=str(node.index.resolver.path),
                     durable=copy / "permissions-v2")


def readings(node, tmp_path) -> dict:
    """What the three say about the share's facts, as counts and codes."""
    census = census_on_a_copy(node, tmp_path)
    compared = gc.compare_index(census)
    aggregate = gc.aggregate(census, run_at="t")
    members = sorted(outcome.wire for outcome in census.members.values() if outcome.family == "fact")
    by_id = {outcome.record_id: outcome for outcome in census.typed if outcome.family == "fact"}
    counted = ft.owner_count(node)
    preview = {item["fact_id"]: item for item in counted["fact_preview"]["items"]}
    return {
        "build_fact_members": fact_members(node),
        "census_fact_members": members,
        "census_reasons": {name: (by_id[fact_id].reason if fact_id in by_id else "not a candidate")
                           for name, fact_id in node.ids.items()},
        "census_classes": collections.Counter(gc.reason_class(outcome.reason) for outcome in by_id.values()
                                              if outcome.reason != "permitted"),
        "sets_equal": compared["sets_equal"],
        "explained": {key: value for key, value in compared.items() if key.startswith(("census_reason", "unmatched",
                                                                                      "aged_out"))} or compared,
        "gate": aggregate["gate"],
        "count": counted["kinds"]["facts"],
        "preview_reasons": {name: (preview[fact_id]["reason"] if fact_id in preview else "not listed")
                            for name, fact_id in node.ids.items()},
    }


def wires(statements) -> list[str]:
    return sorted(hashlib.sha256(text.encode("utf-8")).hexdigest() for text in statements)


def test_with_the_ontology_on_the_census_the_build_and_the_count_agree(node, tmp_path, capsys):
    read = readings(node, tmp_path)
    with capsys.disabled():
        print(f"\nFT168 item 4, ontology on: build fact members {read['build_fact_members']}; census fact members "
              f"{len(read['census_fact_members'])}; census reasons {read['census_reasons']}; classes "
              f"{dict(read['census_classes'])}; index equals census: {read['sets_equal']}; count {read['count']}; "
              f"preview {read['preview_reasons']}")
    assert read["build_fact_members"] == 2
    assert read["census_fact_members"] == wires(SHARED)                # the same two, by their wire content
    assert read["sets_equal"] and read["gate"]["census_equals_live_set"] and read["gate"]["unknown_reasons"] == 0
    assert read["count"]["can_share"] == 2
    assert read["census_classes"] == {"policy": 2, "engineering": 1}, "every withheld fact has a known class"
    assert read["count"]["held_back"] == {"highly_sensitive": 1, "outside_your_choices": 1, "not_checked_yet": 0,
                                          "not_proven_yours": 0, "you_held_back": 0, "could_not_check": 1}
    assert read["census_reasons"]["sensitive_source"] == "not a candidate"     # as in the build: its source is no member
    assert read["preview_reasons"]["sensitive_source"] == "evidence_not_permitted"
    assert read["census_reasons"]["shared_employer"] == read["census_reasons"]["shared_project"] == "permitted"
    assert read["preview_reasons"]["shared_employer"] == read["preview_reasons"]["shared_project"] == "eligible"
    # Every fact the census withholds, the preview withholds, and none of them is in the count's "can share".
    for name in ("not_what_the_source_says", "kept_private", "another_ontology"):
        assert read["census_reasons"][name] not in ("permitted", "not a candidate"), name
        assert read["preview_reasons"][name] not in ("eligible", "not listed"), name
    assert read["census_reasons"]["not_what_the_source_says"] == "fact_not_grounded"
    assert read["census_reasons"]["kept_private"] == "owner_only"
    assert read["census_reasons"]["another_ontology"] == "fact_ontology_unsupported"


def test_switched_off_the_census_files_every_shared_fact_under_policy_and_the_count_under_held_back(node, tmp_path,
                                                                                                    capsys):
    """Right after the switch (the index not yet rebuilt), then after the build, then switched on and built again."""
    before = readings(node, tmp_path)
    assert ft.switch(node, ft.WORK, False)

    # 1. Off, and the index still lists the two facts: the census, which decides as the release does, has none.
    waiting = readings(node, tmp_path)
    assert waiting["build_fact_members"] == 2 and waiting["census_fact_members"] == []
    assert waiting["census_reasons"]["shared_employer"] == waiting["census_reasons"]["shared_project"] == "fact_ontology_off"
    assert gc.reason_class("fact_ontology_off") == "policy" and waiting["census_classes"].get("unknown", 0) == 0
    assert waiting["count"]["can_share"] == 0
    assert waiting["preview_reasons"]["shared_employer"] == waiting["preview_reasons"]["shared_project"] == "fact_ontology_off"
    # The switch is asked before the grounding: the fact its source does not say is now "off" as well, to the census,
    # the preview and the count alike. So "you held back" grows by three, of which two come back when it is
    # switched on. The fact kept private and the one of another ontology are refused before the switch is asked,
    # and the one with a sensitive source is filed by its source: those three do not move.
    assert waiting["census_reasons"]["not_what_the_source_says"] == "fact_ontology_off"
    assert waiting["preview_reasons"]["not_what_the_source_says"] == "fact_ontology_off"
    moved = {name: waiting["count"]["held_back"][name] - before["count"]["held_back"][name]
             for name in waiting["count"]["held_back"]}
    assert moved == {"highly_sensitive": 0, "outside_your_choices": 0, "not_checked_yet": 0, "not_proven_yours": 0,
                     "you_held_back": 3, "could_not_check": -1}, moved
    for name in ("kept_private", "another_ontology", "sensitive_source"):
        assert waiting["census_reasons"][name] == before["census_reasons"][name], name
    for name in ("kept_private", "another_ontology"):
        assert waiting["preview_reasons"][name] == before["preview_reasons"][name], name
    # The one place the preview and the count part ways while it is off: the fact with the sensitive source. The
    # preview now gives the switch as its reason (asked first); the count still files it by its source, as highly
    # sensitive. Switching on will not make it shareable, whatever the preview's words suggest.
    assert waiting["preview_reasons"]["sensitive_source"] == "fact_ontology_off"
    assert waiting["count"]["held_back"]["highly_sensitive"] == 1
    # The index is two members ahead of the census until the queued build; the census names why.
    assert not waiting["sets_equal"]

    # 2. The build the loop queues (here by the owner's hand): the three agree again, on no fact.
    assert node.rebuild()[ft.GRANT] == "ready"
    off = readings(node, tmp_path)
    assert off["build_fact_members"] == 0 and off["census_fact_members"] == [] and off["sets_equal"]
    assert off["gate"]["census_equals_live_set"] and off["gate"]["unknown_reasons"] == 0
    assert off["count"]["can_share"] == 0
    assert off["census_reasons"]["shared_project"] == "fact_ontology_off"

    # 3. On again, and built: back to the first reading.
    assert ft.switch(node, ft.WORK, True) and node.rebuild()[ft.GRANT] == "ready"
    back = readings(node, tmp_path)
    for key in ("build_fact_members", "census_fact_members", "census_reasons", "sets_equal", "count",
                "preview_reasons"):
        assert back[key] == before[key], key
    with capsys.disabled():
        print(f"\nFT168 item 4, off before the build: index holds {waiting['build_fact_members']} facts, census "
              f"{len(waiting['census_fact_members'])}; index equals census: {waiting['sets_equal']}, explained "
              f"{waiting['explained']}; gate {waiting['gate']}; count {waiting['count']}. off after the build: "
              f"census reasons {off['census_reasons']}; count {off['count']}; gate {off['gate']}")


def test_the_census_pins_and_reason_map_hold_on_this_commit():
    """The census refuses to run against engine source it was not read against; the two checks that say so."""
    from tests.permissions_v2 import test_grant_census as census_tests
    census_tests.test_every_reason_code_has_a_class()
    census_tests.test_mirrored_engine_source_is_the_source_the_census_was_read_against()
