"""The bench goes through the owner pipeline's Off-limits pass (review of topos#116, finding 6).

The bench leaves out a candidate who is Off-limits, but a purpose's excerpt, goal label or request text can still
name one. `read_bench` hands the pass to `build_bench_slate`, which runs it before the lineup is chosen and counted:
in the owner's app a role is kept and stamped, for every other caller it is dropped, and no count describes what was
dropped. If the pass cannot run, the bench is empty for every caller. Every person here is invented.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.topos.test_carried_entry_owner_paths import APP, OUTSIDE_CLIENT, as_caller, the_node_knows_its_owner  # noqa: F401
from tests.topos.test_carry_step_review_r1 import conn  # noqa: F401 (conn: fixture)
from topos.analytics import dataset_resolution, relationship_reads
from topos.features.derivation import bench_evidence, bench_sources, social_bench
from topos.features.lifecycle.blackhole import BlackholeStore


def purposes():
    return [
        {"role_shape_id": "r-1", "label": "Review the essay draft", "need_status": "inferred_possible_help",
         "evidence": [{"excerpt": "Asked Sam to look at the essay draft."}]},
        {"role_shape_id": "r-2", "label": "Plan the garden", "need_status": "inferred_possible_help",
         "evidence": [{"excerpt": "Asked about the garden beds."}]},
    ]


def candidates(c, role, dataset_id=None):
    if role["role_shape_id"] == "r-1":
        return [{"person_id": "p-1", "name": "Robin Vale", "capability_status": "demonstrated"}]
    return [{"person_id": "p-2", "name": "Robin Vale", "capability_status": "demonstrated"},
            {"person_id": "p-3", "name": "Kit Moss", "capability_status": "demonstrated",
             "evidence": [{"excerpt": "Sam knows the beds."}]}]


@pytest.fixture
def bench(conn, monkeypatch, the_node_knows_its_owner):  # noqa: F811
    monkeypatch.setattr(bench_evidence, "help_purposes", lambda *a, **kw: purposes())
    monkeypatch.setattr(bench_evidence, "discussion_evidence", lambda *a, **kw: [])
    monkeypatch.setattr(bench_evidence, "discussion_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(bench_sources, "read_help_exchanges",
                        lambda *a, **kw: ([], {"displayed_exchanges": 0, "status": "complete"}))
    monkeypatch.setattr(dataset_resolution, "resolve_messaging_dataset", lambda *a: ("d-1", None))
    monkeypatch.setattr(social_bench, "work_engagement", lambda *a, **kw: {
        "people": [{"name": "Robin Vale"}, {"name": "Kit Moss", "note": "with Sam"}], "coverage": {}})
    monkeypatch.setattr(social_bench, "find_candidates", candidates)
    BlackholeStore(conn).blackhole_entity(entity_ref="Sam", processing_tier="secure", note=None)
    conn.commit()
    return conn


def test_outside_client_gets_no_role_or_person_that_names_an_off_limits_entry(bench):
    out = as_caller(OUTSIDE_CLIENT, relationship_reads.read_bench, bench, dataset_id="d-1")
    assert [r["role_shape_id"] for r in out["roles"]] == ["r-2"]
    assert [c["person_id"] for c in out["roles"][0]["candidates"]] == ["p-2"]
    assert [p["name"] for p in out["people_close_to_this_work"]] == ["Robin Vale"]
    assert "Sam" not in str(out)


def test_no_count_describes_what_the_pass_dropped(bench):
    """The receipt is the leak: an outside client comparing the counts with what it got learns nothing."""
    lineup = as_caller(OUTSIDE_CLIENT, relationship_reads.read_bench, bench, dataset_id="d-1")["coverage"]["lineup"]
    assert lineup["purpose_pool_count"] == lineup["displayed_purpose_count"] == 1
    assert lineup["omitted_purpose_count"] == 0
    assert lineup["displayed_unique_people"] == 1


def test_owner_app_keeps_everything_and_stamps_what_names_an_off_limits_entry(bench):
    out = as_caller(APP, relationship_reads.read_bench, bench, dataset_id="d-1")
    roles = {r["role_shape_id"]: r for r in out["roles"]}
    assert set(roles) == {"r-1", "r-2"}
    assert roles["r-1"].get("blackhole_protected") is True
    assert [c.get("blackhole_protected") for c in roles["r-2"]["candidates"]] == [None, True]
    assert [p.get("blackhole_protected") for p in out["people_close_to_this_work"]] == [None, True]
    assert out["coverage"]["lineup"]["purpose_pool_count"] == 2


def test_a_request_with_no_principal_is_treated_like_an_outside_client(bench):
    out = relationship_reads.read_bench(bench, dataset_id="d-1")
    assert [r["role_shape_id"] for r in out["roles"]] == ["r-2"]


@pytest.mark.parametrize("caller", [APP, OUTSIDE_CLIENT])
def test_a_pass_that_cannot_run_releases_nothing_to_any_caller(bench, monkeypatch, caller):
    from topos.query import retrieval

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("store unavailable")

    monkeypatch.setattr(retrieval, "_blackhole_blocked_record_ids", broken)
    out = as_caller(caller, relationship_reads.read_bench, bench, dataset_id="d-1")
    assert out["roles"] == [] and out["people_close_to_this_work"] == []
    assert out["coverage"]["reason"] == "off_limits_check_unavailable"


def test_roles_without_candidates_follow_the_roles_kept(bench, monkeypatch):
    def only_sam_named(c, role, dataset_id=None):
        return candidates(c, role, dataset_id)[1:] if role["role_shape_id"] == "r-2" else []

    monkeypatch.setattr(social_bench, "find_candidates", only_sam_named)
    out = as_caller(OUTSIDE_CLIENT, relationship_reads.read_bench, bench, dataset_id="d-1")
    assert out["roles_without_candidates"] == ["Plan the garden"]


def test_a_bench_error_that_is_not_the_pass_still_raises(bench, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bench bug")

    monkeypatch.setattr(social_bench, "find_candidates", broken)
    with pytest.raises(RuntimeError):
        as_caller(APP, relationship_reads.read_bench, bench, dataset_id="d-1")
