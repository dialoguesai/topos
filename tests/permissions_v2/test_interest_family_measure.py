"""OD-52 P7: the count-only interest measurement counts what the node would build, and prints nothing else.

protects: the no-print rule for browsing. The measurement runs over a copy of the owner's data and its output is
shared with other sessions: it must carry integers and fixed codes only, never a label, URL, title, host or id,
and it must refuse the live store.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2 import interest_review as ir
from topos.permissions_v2.entity_boundary import EntityBoundary

from tests.permissions_v2.interest_fixtures import (NOW_US, OWNER, at, attest_app, cluster, install,
                                                    month_of_visits, open_db, visit)
from tests.permissions_v2.test_interest_review import ANSWER, Transport

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
measure_module = importlib.import_module("interest_family_measure")

SECRETS = ("sourdough", "velocipedia", "Hydration", "https://", "example.test", "browser:v", "tc_", "Quennell")


@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "measure.db")
    install(conn)
    attest_app(conn)
    cluster(conn, "tc_hobby", "sourdough / baking / starter")
    month_of_visits(conn, 0, 5, [3, 9, 17], title="Hydration tips")          # Aug: qualifies
    month_of_visits(conn, 100, 16, [1, 5, 19], month=9)                     # Sep: qualifies, medium
    cluster(conn, "tc_site", "velocipedia / bikes")
    month_of_visits(conn, 200, 6, [2, 4, 6], cluster_id="tc_site", host="velocipedia.example")  # label names host
    cluster(conn, "tc_thin", "knitting / patterns")
    month_of_visits(conn, 300, 6, [2, 2], cluster_id="tc_thin")             # two days only
    cluster(conn, "tc_private", "chess / openings")
    month_of_visits(conn, 400, 4, [2, 4, 6], cluster_id="tc_private")
    visit(conn, 410, at(8, 8), cluster_id="tc_private", incognito=1)        # the fifth visit was private
    cluster(conn, "tc_foreign", "orchids / repotting")
    month_of_visits(conn, 500, 5, [2, 4, 6], cluster_id="tc_foreign", writer="cp_relay", app=None)  # unproven
    cluster(conn, "tc_old", "kayak / rolls")
    for i, day in enumerate([2, 4, 6, 8, 10]):
        visit(conn, 600 + i, at(3, day, year=2025), cluster_id="tc_old")   # 18 months ago
    conn.commit()
    yield conn
    conn.close()


def report(conn, **kwargs):
    return measure_module.measure(conn, owner_id=OWNER, now_us=NOW_US, **kwargs)


def test_the_funnel_counts_every_guard_in_order(db):
    out = report(db)
    d365, d30 = out["windows"]["d365"], out["windows"]["d30"]
    assert d365["candidates"] == 6          # tc_old's month is older than a year
    assert d365["threshold_all"] == 5       # tc_thin: two days
    assert d365["after_incognito"] == 4     # tc_private
    assert d365["after_nsfw"] == d365["after_excluded"] == 4
    assert d365["after_provenance"] == 3    # tc_foreign
    assert d365["after_browsing"] == d365["after_label_form"] == 3
    assert d365["after_label_host"] == 2    # tc_site
    assert d365["after_offlimits"] == 2
    assert d365["after_assessment"] == 0    # nothing assessed yet
    assert (d30["candidates"], d30["threshold_all"], d30["after_offlimits"]) == (1, 1, 1)  # September only
    assert out["windows_whole_months_only"]["d30"]["after_offlimits"] == 0  # no whole month fits 30 days
    assert out["windows_whole_months_only"]["d365"]["after_offlimits"] == 1
    # Objects exist for every qualifying month; the window applies only where a grant admits members.
    assert out["labels_pending_assessment"] == 2
    assert out["band_mix_deterministic"] == {"low": 2, "medium": 1, "high": 0}
    stages = measure_module._stages()
    for window in out["windows"].values():
        values = [window[stage] for stage in stages]
        assert values == sorted(values, reverse=True)


def test_assessed_labels_reach_the_last_row(db):
    boundary = EntityBoundary(db)
    obj = next(o for o in fam.build(db, owner_id=OWNER, now_us=NOW_US).objects if o.cluster_id == "tc_hobby")
    prepared = ir.prepare(obj, boundary)
    labels = asyncio.run(ir.assess(prepared, transport=Transport(ANSWER)))
    ir.publish(db, owner_id=OWNER, prepared=prepared, classification=labels, boundary=boundary, now=1)
    db.commit()
    out = report(db)
    assert out["windows"]["d365"]["after_assessment"] == 2
    assert out["windows"]["d30"]["after_assessment"] == 1
    assert out["labels_pending_assessment"] == 1


def test_the_report_and_table_carry_no_label_host_title_or_id(db):
    out = report(db)
    text = json.dumps(out) + measure_module.table(out)
    for secret in SECRETS:
        assert secret.lower() not in text.lower()
    assert out["preconditions"] == {"activity_writer_columns": True, "browser_install_certified": True,
                                    "live_receipts_activity": 1, "offlimits_active": False, "opt_outs_read": 0}


def test_opt_outs_are_counted_as_their_own_guard(db):
    out = report(db, opt_outs=frozenset({fam.opt_out_key("tc_hobby")}))
    assert out["windows"]["d365"]["after_excluded_label"] == 2
    assert out["windows"]["d365"]["after_opted_out"] == 0


def test_main_refuses_the_live_store_and_an_unscratched_environment(tmp_path, monkeypatch):
    import census_support as cs
    monkeypatch.delenv("TOPOS_DATABASE_PATH", raising=False)
    with pytest.raises(cs.CensusRefused):
        measure_module.main(["--copy", str(tmp_path), "--out", str(tmp_path / "out.json")])
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(tmp_path / "scratch.db"))
    monkeypatch.setenv("TOPOS_ENV_FILE", str(tmp_path / "scratch.env"))
    live = tmp_path / "live-home"  # stands in for ~/.topos, which a test never touches
    live.mkdir()
    monkeypatch.setattr(cs, "LIVE_HOME", live)
    with pytest.raises(cs.CensusRefused, match="live_store_refused"):
        measure_module.main(["--copy", str(live), "--out", str(tmp_path / "out.json")])
    with pytest.raises(cs.CensusRefused, match="live_store_refused"):
        measure_module.main(["--copy", str(tmp_path), "--out", str(live / "out.json")])
