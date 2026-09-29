"""OD-20 daily census diff (scripts/permissions_v2/daily_census.py): alert rules, the quiet gate, the disk floor,
cleanup on every path, and the keyless census against a real index. Synthetic fixtures only."""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_grant_census import built, node_for  # noqa: F401 (helpers)
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2.search_index import root_for

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
dc = importlib.import_module("daily_census")
gc = importlib.import_module("grant_census")
GIB = 1 << 30


def aggregate(**overrides):
    """A quiet day's IF-1 aggregate, reduced to what the diff reads."""
    base = {"U": 100, "census_members": 40, "live_index_members": 40, "index_state": "ready",
            "gate": {"keyless": True, "census_equals_live_after_aging": True, "unknown_reasons": 0},
            "index_comparison": {"census_members": 40, "live_members": 40, "index_aged_out": 0, "keyless": True},
            "window": {"lower_utc": "2026-09-01T00:00:00+00:00"},
            "pool": {"eligible": 50, "linked_total": 60, "p_impl_zero_on": "2026-10-26",
                     "p_impl_by_event_day": {"2026-09-01": 10, "2026-09-10": 30}},
            "caps": {"max_permitted_records": 4960, "protected_vocabulary_band": "under_50pct"},
            "families": {"message": 40, "fact": 0, "goal": 0, "relationship": 0},
            "job_state": {"last": {"message_assessment_catchup": {"state": "complete", "seconds_before_copy": 500}}},
            "withheld_in_window": [
                {"source_id": "imessage", "reason_code": "provenance_unlinked", "policy_veto": "none",
                 "reason_class": "engineering", "count": 20},
                {"source_id": "imessage", "reason_code": "provenance_unlinked", "policy_veto": "not_owner_authored",
                 "reason_class": "engineering", "count": 30}]}
    for key, value in overrides.items():
        base[key] = value
    return base


def codes(result):
    return sorted(alert["code"] for alert in result["alerts"])


def test_a_quiet_day_raises_nothing_and_the_first_day_says_so():
    assert dc.diff(aggregate(), aggregate())["alerts"] == []
    first = dc.diff(aggregate(), None)
    assert first["alerts"] == [] and first["info"] == [{"code": "first_day"}]


@pytest.mark.parametrize("change,expected", [
    ({"gate": {"keyless": True, "census_equals_live_after_aging": True, "unknown_reasons": 2}}, ["unknown_reasons"]),
    ({"index_state": "missing"}, ["grant_dark"]),
    ({"gate": {"keyless": True, "census_equals_live_after_aging": False, "unknown_reasons": 0}}, ["index_stale"]),
    ({"caps": {"max_permitted_records": 100, "protected_vocabulary_band": "under_50pct"}, "census_members": 4900},
     ["cap_headroom"]),                                   # 100 left of a 5,000 cap
    ({"caps": {"max_permitted_records": 4960, "protected_vocabulary_band": "50_to_90pct"}}, ["protected_vocabulary"]),
    ({"job_state": {"last": {"search_index_restore": {"grant_states": ["failed"], "seconds_before_copy": 900}}}},
     ["refresh_failed"]),
    ({"census_members": 20}, ["p_impl_below_decay"]),       # 40 predicted still in the window, 20 found
    ({"node_source": {"checked": True, "drift": ["search_index.SearchIndexService._members"]}}, ["node_source_drift"]),
    ({"gate": {"keyless": True, "census_equals_live_after_aging": True, "unknown_reasons": 0,
               "void_reasons": ["node_source_drift_with_unexplained_members"]}}, ["census_void"]),
    ({"pool": {"eligible": 50, "linked_total": 60, "p_impl_zero_on": "2026-10-20",
               "p_impl_by_event_day": {"2026-09-01": 10, "2026-09-10": 30}}}, ["pool_zero_earlier"]),
])
def test_each_alert_fires_on_its_own_signal(change, expected):
    assert codes(dc.diff(aggregate(**change), aggregate())) == expected


def test_losses_alert_on_a_new_reason_or_real_growth_and_ignore_masked_rows():
    grown = aggregate(withheld_in_window=[
        {"source_id": "imessage", "reason_code": "provenance_unlinked", "policy_veto": "none", "reason_class": "engineering", "count": 30},
        {"source_id": "imessage", "reason_code": "protected_content_unknown_model", "policy_veto": "none",
         "reason_class": "engineering", "count": 3},
        {"source_id": "imessage", "reason_code": "provenance_unlinked", "policy_veto": "not_owner_authored",
         "reason_class": "engineering", "count": 300}])
    assert codes(dc.diff(grown, aggregate())) == ["loss_growth", "new_loss_reason"]
    small = aggregate(withheld_in_window=[
        {"source_id": "imessage", "reason_code": "provenance_unlinked", "policy_veto": "none", "reason_class": "engineering", "count": 24}])
    assert codes(dc.diff(small, aggregate())) == []      # +4 rows is under the absolute minimum


def test_assessment_lag_is_measured_against_owner_shaped_rows():
    lagging = aggregate(withheld_in_window=[
        {"source_id": "imessage", "reason_code": "unassessed", "policy_veto": "none", "reason_class": "engineering", "count": 4},
        {"source_id": "imessage", "reason_code": "review_stale_context", "policy_veto": "none", "reason_class": "engineering", "count": 2},
        {"source_id": "imessage", "reason_code": "provenance_unlinked", "policy_veto": "not_owner_authored",
         "reason_class": "engineering", "count": 30}])
    result = dc.diff(lagging, None)                      # 6 of 70 owner-shaped rows: over 5%
    assert codes(result) == ["assessment_lag"] and result["alerts"][0]["owner_shaped"] == 70


def test_the_quiet_gate_reads_modification_times_only(tmp_path):
    for rel in dc.QUIET_SECONDS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    now = time.time()
    assert dc.quiet(tmp_path, now, mtime=lambda path: now - 3600)
    assert not dc.quiet(tmp_path, now, mtime=lambda path: now - (60 if path.name == "ledger.db" else 3600))


def test_the_disk_floor_is_eight_gibibytes_after_the_copy(tmp_path):
    usage = lambda path: SimpleNamespace(free=9 * GIB)
    assert dc.preflight(int(0.5 * GIB), tmp_path, disk_usage=usage)
    assert not dc.preflight(2 * GIB, tmp_path, disk_usage=usage)
    assert dc.FLOOR_BYTES == 8 * GIB


def fake_source(root: Path) -> Path:
    durable = root / "permissions-v2"
    durable.mkdir(parents=True)
    for name in ("ledger.db", "evidence-reviews.db", "evidence-reviews.db.enrollment.json"):
        (durable / name).write_text("x")
    (root / "database.db").write_text("x")
    (durable / "config.json").write_text(json.dumps({"canonical_database_path": str(root / "database.db"),
                                                     "ledger_path": str(durable / "ledger.db")}))
    return root


def test_every_path_deletes_the_copy_and_no_key_is_ever_taken(tmp_path):
    source, scratch, out = fake_source(tmp_path / "node"), tmp_path / "scratch", tmp_path / "out"
    seen = {}

    def copy(source_, dest_root, private_root, *, keys, stores):
        seen["keys"], seen["private_root"] = keys, private_root
        target = Path(dest_root) / "run"
        target.mkdir(parents=True)
        (target / "census-copy-manifest.json").write_text("{}")
        return {"path": str(target)}

    def failing_census(**_kwargs):
        raise RuntimeError("census failed")
    old = lambda path: time.time() - 7200
    roomy = lambda path: SimpleNamespace(free=100 * GIB)
    with pytest.raises(Exception):
        dc.daily(source, scratch, out, make_copy=copy, census_run=failing_census, mtime=old, disk_usage=roomy)
    assert seen == {"keys": False, "private_root": None} and not (scratch / "copy").exists()
    assert not list(tmp_path.rglob("keys.db"))
    status, _ = dc.daily(source, scratch, out, make_copy=lambda *a, **k: {"void": True, "attempts": [1, 2, 3, 4]},
                         mtime=old, disk_usage=roomy)
    assert status == "void" and not (scratch / "copy").exists()
    sleeps = []
    status, detail = dc.daily(source, scratch, out, sleep=sleeps.append, mtime=lambda path: time.time(), disk_usage=roomy)
    assert (status, detail["attempts"], len(sleeps)) == ("skipped_busy", 4, 3)
    status, _ = dc.daily(source, scratch, out, mtime=old, disk_usage=lambda path: SimpleNamespace(free=GIB))
    assert status == "skipped_disk"


def test_the_keyless_census_agrees_with_the_index_by_count(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    resolver = node.index.resolver
    durable = root_for(resolver.path).parent
    census = gc.run(canonical=Path(resolver.path), reviews=durable / Path(node.index.reviews.path).name,
                    ledger=node.ledger.path, index_root=durable / "message-search", keys=None, binding=resolver.binding,
                    live_canonical=None, now=node.now[0], keyless=True)
    result = gc.aggregate(census, run_at="t")
    assert result["gate"] == {"keyless": True, "census_equals_live_after_aging": True, "unknown_reasons": 0,
                              "node_source_drift": None, "void_reasons": []}
    assert result["index_comparison"]["census_members"] == result["index_comparison"]["live_members"] == 1
    assert dc.diff(result, None)["alerts"] == []


def test_a_small_loss_growth_is_noise_and_aged_out_members_are_not_expected_back():
    def losses(n):
        return [{"source_id": "imessage", "reason_code": "provenance_unlinked", "policy_veto": "none",
                 "reason_class": "engineering", "count": n}]
    assert codes(dc.diff(aggregate(withheld_in_window=losses(13)), aggregate(withheld_in_window=losses(10)))) == []
    assert codes(dc.diff(aggregate(withheld_in_window=losses(16)), aggregate(withheld_in_window=losses(10)))) == [
        "loss_growth"]
    # Yesterday's 10 members dated 1 Sep fall before today's edge (5 Sep): only 30 are expected back.
    moved = {"lower_utc": "2026-09-05T00:00:00+00:00"}
    assert codes(dc.diff(aggregate(window=moved, census_members=28), aggregate())) == []
    assert codes(dc.diff(aggregate(window=moved, census_members=26), aggregate())) == ["p_impl_below_decay"]
