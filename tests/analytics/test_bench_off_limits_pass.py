"""The bench goes through the owner pipeline's Off-limits pass (review of topos#116, finding 6).

The bench leaves out a candidate who is Off-limits, but a purpose's excerpt, goal label or request text can still
name one. `read_bench` now hands the slate to `_blackhole_policy_for_summary` at the owner's tier: in his app the
role is kept and stamped, for his outside client it is dropped. Every person here is invented.
"""
from __future__ import annotations

import pytest

from tests.topos.test_carried_entry_owner_paths import APP, OUTSIDE_CLIENT, as_caller, the_node_knows_its_owner  # noqa: F401
from tests.topos.test_carry_step_review_r1 import conn  # noqa: F401 (conn: fixture)
from topos.analytics import relationship_reads
from topos.features.derivation import social_bench
from topos.features.lifecycle.blackhole import BlackholeStore


def slate():
    named = {"role_shape_id": "r-1", "label": "Review the essay draft",
             "evidence": [{"excerpt": "Asked Sam to look at the essay draft."}],
             "candidates": [{"person_id": "p-1", "name": "Robin Vale", "bench_qualification": {"status": "ready"}}]}
    plain = {"role_shape_id": "r-2", "label": "Plan the garden",
             "evidence": [{"excerpt": "Asked about the garden beds."}],
             "candidates": [{"person_id": "p-2", "name": "Robin Vale"},
                            {"person_id": "p-3", "name": "Kit Moss", "evidence": [{"excerpt": "Sam knows the beds."}]}]}
    return {"roles": [named, plain], "roles_without_candidates": [],
            "people_close_to_this_work": [{"name": "Robin Vale"}, {"name": "Kit Moss", "note": "with Sam"}],
            "coverage": {"dataset_id": "d-1"}}


@pytest.fixture
def bench(conn, monkeypatch, the_node_knows_its_owner):  # noqa: F811
    monkeypatch.setattr(social_bench, "build_bench_slate", lambda c, dataset_id=None: slate())
    BlackholeStore(conn).blackhole_entity(entity_ref="Sam", processing_tier="secure", note=None)
    conn.commit()
    return conn


def test_outside_client_gets_no_role_or_person_that_names_an_off_limits_entry(bench):
    out = as_caller(OUTSIDE_CLIENT, relationship_reads.read_bench, bench, dataset_id="d-1")
    assert [r["role_shape_id"] for r in out["roles"]] == ["r-2"]
    assert [c["person_id"] for c in out["roles"][0]["candidates"]] == ["p-2"]
    assert [p["name"] for p in out["people_close_to_this_work"]] == ["Robin Vale"]
    assert "Sam" not in str(out)


def test_owner_app_keeps_everything_and_stamps_what_names_an_off_limits_entry(bench):
    out = as_caller(APP, relationship_reads.read_bench, bench, dataset_id="d-1")
    assert [r["role_shape_id"] for r in out["roles"]] == ["r-1", "r-2"]
    assert out["roles"][0].get("blackhole_protected") is True
    assert [c.get("blackhole_protected") for c in out["roles"][1]["candidates"]] == [None, True]
    assert [p.get("blackhole_protected") for p in out["people_close_to_this_work"]] == [None, True]


def test_a_pass_that_cannot_run_releases_nothing_outside_the_app(bench, monkeypatch):
    from topos.query import retrieval

    def broken(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(retrieval, "_blackhole_policy_for_summary", broken)
    out = as_caller(OUTSIDE_CLIENT, relationship_reads.read_bench, bench, dataset_id="d-1")
    assert out["roles"] == [] and out["people_close_to_this_work"] == []


def test_roles_without_candidates_follow_the_roles_kept(bench, monkeypatch):
    def only_sam_named(c, dataset_id=None):
        s = slate()
        s["roles"][1]["candidates"] = [s["roles"][1]["candidates"][1]]
        return s

    monkeypatch.setattr(social_bench, "build_bench_slate", only_sam_named)
    out = as_caller(OUTSIDE_CLIENT, relationship_reads.read_bench, bench, dataset_id="d-1")
    assert out["roles_without_candidates"] == ["Plan the garden"]
