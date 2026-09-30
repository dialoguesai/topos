"""OD-52 P7: browsing reaches a grant only as monthly interests, never as pages.

protects: the owner's rule for browser history. A released interest is a topic label, a month and
a strength band. A cluster-month counts only with at least five of the owner's own visits on three
distinct days; private-window, excluded and unproven visits never count; a label that names a site,
a page title, a person a visit mentions, an excluded or Off-limits entity, or that is not a short
topic name, is withheld. Every check here fails closed.
"""
from __future__ import annotations

import json
from dataclasses import fields

import pytest

from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2.canonical import PolicyError

from tests.permissions_v2.interest_fixtures import (APP, NOW_US, OWNER, SOURCE, at, attest_app, cluster, install,
                                month_of_visits, open_db, visit)

LABEL = "sourdough / baking / starter"


@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "interest.db")
    install(conn)
    attest_app(conn)
    cluster(conn, "tc_hobby", LABEL)
    yield conn
    conn.close()


def build(conn, **kwargs):
    return fam.build(conn, owner_id=OWNER, now_us=NOW_US, **kwargs)


def only(conn, **kwargs):
    objects = build(conn, **kwargs).objects
    assert len(objects) == 1
    return objects[0]


def candidate(conn, cluster_id="tc_hobby", month="2026-08", **kwargs):
    return next(c for c in build(conn, **kwargs).candidates if c.cluster_id == cluster_id and c.month == month)


# --- the threshold, the band, the period ----------------------------------------------------

def test_five_visits_on_three_days_is_an_interest(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.commit()
    obj = only(db)
    assert (obj.cluster_id, obj.month, obj.band, obj.label, obj.complete) == ("tc_hobby", "2026-08", "low",
                                                                             LABEL, True)
    assert (obj.period_start_us, obj.period_end_us) == fam.month_span("2026-08")


@pytest.mark.parametrize("count,days", [(4, [1, 2, 3, 4]), (6, [1, 2]), (5, [5, 5, 6])])
def test_below_the_threshold_is_a_record_not_an_interest(db, count, days):
    month_of_visits(db, 0, count, days)
    db.commit()
    assert build(db).objects == []
    assert not candidate(db).qualifies()


@pytest.mark.parametrize("count,band", [(5, "low"), (14, "low"), (15, "medium"), (49, "medium"),
                                        (50, "high"), (120, "high")])
def test_band_steps(db, count, band):
    month_of_visits(db, 0, count, [2, 11, 25])
    db.commit()
    assert only(db).band == band
    assert fam.band_for(count) == band


def test_band_is_none_below_the_threshold():
    assert fam.band_for(4) is None and fam.band_for(0) is None


def test_months_are_separate_and_the_current_month_is_its_elapsed_part(db):
    month_of_visits(db, 0, 5, [3, 9, 17], month=8)
    month_of_visits(db, 100, 5, [1, 5, 19], month=9)
    db.commit()
    objects = {o.month: o for o in build(db).objects}
    assert set(objects) == {"2026-08", "2026-09"}
    assert objects["2026-09"].complete is False
    assert objects["2026-09"].period_end_us == NOW_US
    assert objects["2026-09"].period_start_us == fam.month_span("2026-09")[0]


def test_unknown_and_future_times_never_count(db):
    month_of_visits(db, 0, 4, [3, 9, 17])
    visit(db, 50, "2026-08-20 10:00:00")  # naive text: not explicit UTC
    visit(db, 51, "2026-09-20T12:00:01.000Z")  # one second after the build
    db.commit()
    result = build(db)
    assert result.objects == []
    assert (result.visit_counts["time_unknown"], result.visit_counts["time_future"]) == (1, 1)


def test_month_span_rejects_malformed_months():
    for month in ("2026-13", "2026-8", "26-08", None, "2026-08-01"):
        with pytest.raises(PolicyError):
            fam.month_span(month)
    assert fam.month_span("2026-12")[1] == fam.month_span("2027-01")[0]


# --- the window at month granularity ---------------------------------------------------------

def _inside(month, max_age_days, now_us=NOW_US, complete=None):
    start, end = fam.month_span(month)
    end = end if end <= now_us else now_us
    return fam.period_inside(period_start_us=start, period_end_us=end, now_us=now_us,
                             max_age_seconds=max_age_days * 86_400)


def test_a_month_counts_only_when_wholly_inside_the_window():
    assert _inside("2026-09", 30) is True  # elapsed part of September, Sep 1 is inside 30 days of Sep 20
    assert _inside("2026-08", 30) is False  # August began 50 days ago
    assert _inside("2026-08", 60) is True
    assert _inside("2026-07", 60) is False
    assert _inside("2025-09", 365) is False  # began 384 days before
    assert _inside("2025-10", 365) is True


def test_window_edges_are_inclusive_and_unknowns_withhold():
    start, end = fam.month_span("2026-08")
    span = (end - start) // 1_000_000
    # The window reaches back exactly to the month's first instant; the month's last microsecond has passed.
    assert fam.period_inside(period_start_us=start, period_end_us=end, now_us=end, max_age_seconds=span)
    assert fam.period_inside(period_start_us=start, period_end_us=end, now_us=end - 1, max_age_seconds=span)
    assert not fam.period_inside(period_start_us=start, period_end_us=end, now_us=end + 1, max_age_seconds=span)
    assert not fam.period_inside(period_start_us=start, period_end_us=end, now_us=end - 2, max_age_seconds=span)
    for bad in (None, 1.5, "1"):
        assert not fam.period_inside(period_start_us=start, period_end_us=end, now_us=bad, max_age_seconds=10)
    assert not fam.period_inside(period_start_us=end, period_end_us=start, now_us=end, max_age_seconds=10**9)
    assert not fam.period_inside(period_start_us=start, period_end_us=end, now_us=end, max_age_seconds=-1)


# --- visits that never count ------------------------------------------------------------------

def test_private_window_visits_never_count(db):
    month_of_visits(db, 0, 4, [3, 9, 17])
    visit(db, 10, at(8, 20), incognito=1)
    visit(db, 11, at(8, 21), metadata={"incognito": "true"})
    visit(db, 12, at(8, 22), metadata="not json")
    db.execute("UPDATE activity_events SET metadata_json='not json' WHERE event_id='browser:v12'")
    db.commit()
    assert build(db).objects == []
    c = candidate(db)
    assert (c.visits["all"], c.visits["incognito"]) == (7, 4)


def test_a_flat_row_that_says_not_private_counts(db):
    month_of_visits(db, 0, 4, [3, 9, 17])
    visit(db, 10, at(8, 20), incognito=0)
    db.commit()
    assert only(db).band == "low"


def test_nsfw_flagged_visits_never_count(db):
    db.execute("ALTER TABLE activity_events ADD COLUMN content_nsfw INTEGER")
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.execute("UPDATE activity_events SET content_nsfw=1 WHERE event_id='browser:v4'")
    db.commit()
    assert build(db).objects == []
    c = candidate(db)
    assert (c.visits["incognito"], c.visits["nsfw"]) == (5, 4)


def test_excluded_visits_and_visits_naming_excluded_entities_never_count(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
               "VALUES ('x1','record','browser:v0')")
    db.commit()
    assert build(db).objects == []
    db.execute("DELETE FROM intelligence_exclusions")
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) VALUES ('ent-1','org','Nimbus Guild','nimbus guild')")
    db.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, canonical_table) "
               "VALUES ('mn-1','ent-1','browser:v1','activity_events')")
    db.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
               "VALUES ('x2','entity','ent-1')")
    db.commit()
    assert build(db).objects == []
    assert candidate(db).visits["excluded"] == 4


def test_unreadable_exclusions_withhold_everything(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
               "VALUES ('x1','nonsense','k')")
    db.commit()
    with pytest.raises(PolicyError):
        build(db)


@pytest.mark.parametrize("stamp", [
    {"writer": "cp_relay", "app": None},
    {"writer": "third_party"},
    {"writer": "owner_app", "app": "some-other-app"},
    {"writer": "owner_app", "app": None},
    {"writer": "owner_app", "dataset": "other-owner:topos:default"},
    {"writer": "owner_automation"},
    {"writer": None},
])
def test_only_the_owners_attested_capture_counts(db, stamp):
    month_of_visits(db, 0, 4, [3, 9, 17])
    visit(db, 10, at(8, 20), **stamp)
    db.commit()
    assert build(db).objects == []
    assert candidate(db).visits["provenance"] == 4


def test_owner_file_import_counts(db):
    month_of_visits(db, 0, 4, [3, 9, 17])
    visit(db, 10, at(8, 20), writer="owner_import", app=None)
    db.commit()
    assert only(db).band == "low"


def test_pre_stamp_visits_count_once_the_owner_attests_them_and_stop_on_change_or_revoke(tmp_path):
    conn = open_db(tmp_path / "pre.db")
    install(conn)
    cluster(conn, "tc_hobby", LABEL)
    month_of_visits(conn, 0, 5, [3, 9, 17], writer=None)
    conn.commit()
    assert build(conn).objects == []  # no receipt yet
    receipt = attest_app(conn)
    assert receipt["row_count"] == 5
    assert only(conn).band == "low"
    # The receipt names the visit at its url and time; a rewrite falls out of it.
    conn.execute("UPDATE activity_events SET url='https://example.test/moved' WHERE event_id='browser:v0'")
    conn.commit()
    assert build(conn).objects == []
    conn.execute("UPDATE activity_events SET url='https://example.test/page/0' WHERE event_id='browser:v0'")
    conn.commit()
    assert only(conn)
    cr.revoke(conn, owner_id=OWNER, receipt_id=receipt["receipt_id"], now=1_700_000_100)
    conn.commit()
    assert build(conn).objects == []
    # Nothing was ever written to a provenance column.
    assert conn.execute("SELECT COUNT(*) FROM activity_events WHERE writer_class IS NOT NULL").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("installs", [[], [{}, {"dataset": "second:topos:default"}], [{"dataset": "*"}],
                                      [{"active": 0}]])
def test_no_single_certified_install_proves_nothing(tmp_path, installs):
    conn = open_db(tmp_path / "noinstall.db")
    for kwargs in installs:
        install(conn, **kwargs)
    cluster(conn, "tc_hobby", LABEL)
    month_of_visits(conn, 0, 8, [3, 9, 17])
    conn.commit()
    assert build(conn).objects == []
    conn.close()


def test_a_visit_of_another_source_in_the_cluster_is_ignored(db):
    month_of_visits(db, 0, 4, [3, 9, 17])
    visit(db, 10, at(8, 20), source="browser_events")
    db.commit()
    assert build(db).objects == []


# --- label checks ----------------------------------------------------------------------------

def _with_label(db, label, **kwargs):
    cluster(db, "tc_hobby", label, **kwargs)
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.commit()
    return candidate(db).label_withheld


def test_a_cluster_drawn_mostly_from_messages_is_not_browsing(db):
    assert _with_label(db, LABEL, other_members=5) == "browsing"
    assert build(db).objects == []


def test_a_cluster_just_over_half_browsing_counts(db):
    assert _with_label(db, LABEL, other_members=4) is None


def _message_member(db, cluster_id, n, when):
    db.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, source_id, sender_type, content, event_at) "
               "VALUES (?,?,?,?,?,?)", (f"msg-{n}", "conv-1", "chatgpt", "user", "synthetic", when))
    db.execute("INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id) VALUES (?,?,?,?)",
               (f"mm-{n}", cluster_id, f"msg-{n}", "chatgpt"))


def test_browsing_is_decided_per_month(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    month_of_visits(db, 100, 5, [1, 5, 19], month=9)
    for n in range(8):
        _message_member(db, "tc_hobby", n, at(9, 2 + n))  # September's messages outnumber its visits
    db.commit()
    assert [o.month for o in build(db).objects] == ["2026-08"]
    assert candidate(db, month="2026-09").label_withheld == "browsing"


def test_a_member_of_unknown_time_counts_against_every_month(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    month_of_visits(db, 100, 5, [1, 5, 19], month=9)
    for n in range(3):
        _message_member(db, "tc_hobby", n, "2026-09-02 10:00")  # naive: its month is unknown
    visit(db, 200, "not a time")
    db.commit()
    assert len(build(db).objects) == 2  # 5 of 9 in each month
    visit(db, 201, "still not a time")
    db.commit()
    assert build(db).objects == []  # 5 of 10


@pytest.mark.parametrize("label", [
    "https://example.test/page", "see www.example.test", "example.test", "news.example.org stuff",
    "path/to/page", "@somehandle", "topic cluster", "", " padded", "12345", "x" * 65,
    "one two three four five six seven eight nine"])
def test_a_label_must_be_a_short_topic_name(db, label):
    assert _with_label(db, label) == "label_form"


@pytest.mark.parametrize("label", [LABEL, "Home espresso (grinders)", "trail running / shoes", "Rust async (3)"])
def test_topic_names_pass_the_form_check(label):
    assert fam.label_form_ok(label)


def test_a_missing_label_fails_the_form_check():
    assert not fam.label_form_ok(None) and not fam.label_form_ok(3)


@pytest.mark.parametrize("label,host", [("velocipedia / bikes", "velocipedia.example"),
                                        ("Velo Pedia reviews", "velopedia.example"),
                                        ("forum / knitting", "forum.knitting-circle.example")])
def test_a_label_naming_a_site_is_withheld(db, label, host):
    cluster(db, "tc_hobby", label)
    month_of_visits(db, 0, 5, [3, 9, 17], host=host)
    db.commit()
    assert candidate(db).label_withheld == "label_host"


def test_generic_host_parts_do_not_withhold(db):
    cluster(db, "tc_hobby", "web / app / design")
    month_of_visits(db, 0, 5, [3, 9, 17], host="www.app.example.com")
    db.commit()
    assert candidate(db).label_withheld is None


def test_a_label_echoing_a_page_title_is_withheld(db):
    title = "How I finally fixed my wobbly bookshelf in one afternoon"
    cluster(db, "tc_hobby", title[:38])  # the clustering's fallback: a title prefix, cut mid-word
    month_of_visits(db, 0, 5, [3, 9, 17], title=title)
    db.commit()
    assert candidate(db).label_withheld == "label_title"


@pytest.mark.parametrize("label,title,echo", [
    ("wobbly bookshelf fixes", "Wobbly bookshelf fixes", True),
    ("Wobbly booksh", "Wobbly bookshelf fixes for renters", True),  # a short title prefix, cut mid-word
    ("wobbly / bookshelf", "Wobbly shelves", False),
    ("my wobbly bookshelf repair", "How I fixed my wobbly bookshelf repair day", True),
    ("bookshelf / repair / wood", "How I fixed my wobbly bookshelf", False),
    ("bookshelf", "Bookshelves of the world", False),
])
def test_title_echo_rule(label, title, echo):
    assert fam.echoes_title(label, [title]) is echo


def test_a_label_naming_a_person_a_visit_mentions_is_withheld(db):
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json) "
               "VALUES ('p-1','person','Orla Quennell','orla quennell','[\"OQ\"]')")
    db.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, canonical_table) "
               "VALUES ('mn-1','p-1','browser:v2','activity_events')")
    assert _with_label(db, "quennell / interviews") == "label_person"


def test_any_persons_whole_name_is_withheld(db):
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json) "
               "VALUES ('p-1','person','Orla Quennell','orla quennell','[\"Orly Q\"]')")
    assert _with_label(db, "orla quennell interviews") == "label_person"
    assert fam.names_any("orly q / talks", (frozenset({"orlyq"}), frozenset()))


def test_a_persons_surname_alone_withholds_only_when_a_visit_mentions_them(db):
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) VALUES ('p-1','person','Orla Quennell','orla quennell')")
    assert _with_label(db, "quennell / interviews") is None


def test_a_mentioned_organisation_is_not_a_person(db):
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) VALUES ('o-1','org','Quennell Works','quennell works')")
    db.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, canonical_table) "
               "VALUES ('mn-1','o-1','browser:v2','activity_events')")
    assert _with_label(db, "quennell / interviews") is None


def test_an_excluded_entity_or_cluster_withholds_the_label(db):
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) VALUES ('o-1','org','Nimbus Guild','nimbus guild')")
    db.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
               "VALUES ('x1','entity','o-1')")
    assert _with_label(db, "nimbus / meetups") == "excluded_label"
    db.execute("DELETE FROM intelligence_exclusions")
    db.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
               "VALUES ('x2','record','tc_hobby')")
    db.commit()
    assert candidate(db).label_withheld == "excluded_label"


def test_an_opted_out_cluster_is_withheld_every_month(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    month_of_visits(db, 100, 5, [1, 5, 19], month=9)
    db.commit()
    opt_outs = frozenset({fam.opt_out_key("tc_hobby")})
    assert build(db, opt_outs=opt_outs).objects == []
    assert {c.label_withheld for c in build(db, opt_outs=opt_outs).candidates} == {"opted_out"}
    assert len(build(db, opt_outs=frozenset({fam.opt_out_key("tc_other")})).objects) == 2


def _off_limits(db, name):
    db.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
               "rebuild_state) VALUES ('bh-1','',?,?,'complete')", (name, name.lower()))


def test_an_off_limits_label_is_withheld(db):
    _off_limits(db, "Sourdough")
    assert _with_label(db, LABEL) == "offlimits"


def test_an_off_limits_page_withholds_only_its_month(db):
    _off_limits(db, "Pemberly Hollis")
    month_of_visits(db, 0, 5, [3, 9, 17])
    visit(db, 20, at(8, 21), title="An evening with Pemberly Hollis")
    month_of_visits(db, 100, 5, [1, 5, 19], month=9)
    db.commit()
    assert candidate(db).label_withheld == "offlimits"
    assert [o.month for o in build(db).objects] == ["2026-09"]


def test_an_undecidable_boundary_withholds(db, monkeypatch):
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.commit()

    class Broken:
        active = True

        def legacy_veto(self, *_args):
            raise PolicyError("entity_protection_lineage_unavailable")

        def mentions_protected(self, *_texts):
            return False

    assert build(db, boundary=Broken()).objects == []


# --- revisions and what an object carries -----------------------------------------------------

def test_revisions_move_with_what_they_bind(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.commit()
    first = only(db)
    assert only(db) == first  # deterministic
    visit(db, 30, at(8, 28))
    db.commit()
    more = only(db)
    assert more.label_revision == first.label_revision and more.content_revision != first.content_revision
    cluster(db, "tc_hobby", "sourdough / baking")
    db.commit()
    relabeled = only(db)
    assert relabeled.label_revision != first.label_revision
    assert relabeled.content_revision != more.content_revision


def test_a_band_change_moves_the_content_revision_only_through_the_band():
    base = dict(label_rev="a" * 64, month="2026-08", complete=True, member_revision="b" * 64)
    assert fam.content_revision(band="low", **base) != fam.content_revision(band="medium", **base)
    assert fam.content_revision(band="medium", **{**base, "complete": False}) != fam.content_revision(
        band="medium", **base)


def test_an_object_carries_no_page_host_or_visit(db):
    title, host = "Hydration tips for long rides", "cyclingtips.example"
    month_of_visits(db, 0, 5, [3, 9, 17], title=title, host=host)
    db.commit()
    obj = only(db)
    text = repr([getattr(obj, f.name) for f in fields(obj)])
    for forbidden in (host, "https://", title, "browser:v", "v0"):
        assert forbidden not in text


def test_missing_schema_is_an_empty_build(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    result = fam.build(conn, owner_id=OWNER, now_us=NOW_US)
    assert (result.schema, result.objects, result.candidates) == ("unavailable", [], [])
    conn.close()


# --- the receipt flow for visits --------------------------------------------------------------

def test_the_visit_preview_names_counts_and_a_digest_only(tmp_path):
    conn = open_db(tmp_path / "preview.db")
    install(conn)
    cluster(conn, "tc_hobby", LABEL)
    month_of_visits(conn, 0, 3, [3], writer=None, host="private-site.example")
    visit(conn, 9, at(8, 4))  # stamped: not a pre-stamp row
    conn.commit()
    preview = cr.preview(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, app_id=APP)
    assert set(preview) == {"version", "table", "source_id", "app_id", "row_count", "statement",
                            "dataset_certified", "preview_digest"}
    assert (preview["row_count"], preview["dataset_certified"], preview["table"]) == (3, True, "activity_events")
    assert "private-site" not in repr(preview) and "browser:v" not in repr(preview)
    with pytest.raises(PolicyError, match="capture_attestation_preview_stale"):
        cr.attest(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, app_id=APP,
                  preview_digest="0" * 64, confirm=True)
    visit(conn, 10, at(8, 5), writer=None)
    conn.commit()
    with pytest.raises(PolicyError, match="capture_attestation_preview_stale"):
        cr.attest(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, app_id=APP,
                  preview_digest=preview["preview_digest"], confirm=True)
    conn.close()


def test_without_writer_columns_no_visit_is_eligible_or_proven(tmp_path):
    """Before the P1 migration lands, nothing on a visit can be proven: fail closed."""
    import sqlite3
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(str(tmp_path / "nop1.db"))
    apply_all_migrations(conn)
    install(conn)
    conn.execute("INSERT INTO activity_events (event_id, url, occurred_at, source_id) VALUES "
                 "('browser:v1','https://example.test/1',?,?)", (at(8, 3), SOURCE))
    conn.commit()
    assert cr.eligible_rows(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE) == []
    row = {"event_id": "browser:v1", "source_id": SOURCE, "url": "https://example.test/1", "occurred_at": at(8, 3)}
    assert cr.proven_rows(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, rows=[row]) == frozenset()
    conn.close()


def test_the_journal_family_is_unchanged():
    assert cr.FAMILIES["journal_entries"].revision_columns == ("source_id", "content")
    assert cr.family_of("activity_events").id_column == "event_id"


def test_proven_rows_agrees_with_proven_row_for_row(tmp_path):
    conn = open_db(tmp_path / "eq.db")
    install(conn)
    cluster(conn, "tc_hobby", LABEL)
    # Pre-stamp rows, some attested, one moved after attestation.
    month_of_visits(conn, 0, 4, [3], writer=None)
    attest_app(conn)
    conn.execute("UPDATE activity_events SET occurred_at=? WHERE event_id='browser:v0'", (at(8, 5),))
    stamps = [
        dict(writer="owner_app"), dict(writer="owner_app", app="other"), dict(writer="owner_app", app=None),
        dict(writer="owner_app", dataset="elsewhere"), dict(writer="owner_import", app=None),
        dict(writer="OWNER_APP"), dict(writer="cp_relay"), dict(writer="third_party"), dict(writer=None),
        dict(writer="owner_app", source="browser_events"), dict(writer=" "),
    ]
    for i, stamp in enumerate(stamps):
        visit(conn, 100 + i, at(8, 6), **stamp)
    conn.commit()
    columns = ["event_id", "source_id", "url", "occurred_at", "writer_class", "writer_app_id", "writer_dataset_id"]
    rows = [dict(zip(columns, r)) for r in conn.execute(f"SELECT {', '.join(columns)} FROM activity_events")]
    rows.append({"event_id": None, "source_id": SOURCE})
    rows.append({"event_id": "", "source_id": SOURCE})
    batch = cr.proven_rows(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, rows=rows)
    single = {r["event_id"] for r in rows
              if cr.proven(conn, owner_id=OWNER, table="activity_events", identity_source_id=SOURCE, row=r)}
    assert batch == frozenset(single)
    assert 0 < len(batch) < len(rows)
    for owner in ("", None, "someone-else"):
        assert cr.proven_rows(conn, owner_id=owner, table="activity_events", source_id=SOURCE, rows=rows) == \
            frozenset(r["event_id"] for r in rows if cr.proven(
                conn, owner_id=owner, table="activity_events", identity_source_id=SOURCE, row=r))
    assert cr.proven_rows(conn, owner_id=OWNER, table="nope", source_id=SOURCE, rows=rows) == frozenset()
    conn.close()


# --- the stored objects (IF-5 §1.3) -----------------------------------------------------------

def _stored(conn):
    import json
    return [(key, json.loads(payload), json.loads(refs), valid_to) for key, payload, refs, valid_to in conn.execute(
        "SELECT object_key, payload_json, source_refs_json, valid_to FROM signal_objects "
        "WHERE object_type='browsing_interest' ORDER BY created_at, object_id")]


def test_objects_are_stored_as_browsing_interests(db):
    title, host = "Hydration tips for long rides", "cyclingtips.example"
    month_of_visits(db, 0, 16, [3, 9, 17], title=title, host=host)
    db.commit()
    counts = fam.persist(db, build(db))
    db.commit()
    assert counts == {"inserted": 1, "closed": 0, "unchanged": 0}
    (key, payload, refs, valid_to), = _stored(db)
    assert key == "interest:tc_hobby:2026-08" and valid_to is None
    assert {k: payload[k] for k in ("label", "month", "strength", "cluster_id", "visit_count", "day_count")} == {
        "label": LABEL, "month": "2026-08", "strength": "medium", "cluster_id": "tc_hobby", "visit_count": 16,
        "day_count": 3}
    assert refs == [{"table": "topic_clusters", "id": "tc_hobby", "month": "2026-08"}]
    row = db.execute("SELECT * FROM signal_objects WHERE object_type='browsing_interest'").fetchone()
    for forbidden in (host, title, "https://", "browser:v"):
        assert forbidden not in repr(row)


def test_persist_is_idempotent_and_closes_what_changed_or_left(db):
    month_of_visits(db, 0, 5, [3, 9, 17])
    month_of_visits(db, 100, 5, [1, 5, 19], month=9)
    db.commit()
    assert fam.persist(db, build(db)) == {"inserted": 2, "closed": 0, "unchanged": 0}
    assert fam.persist(db, build(db)) == {"inserted": 0, "closed": 0, "unchanged": 2}
    visit(db, 300, at(9, 20, hour=1))
    db.execute("DELETE FROM topic_cluster_members WHERE record_id='browser:v0'")
    db.commit()
    assert fam.persist(db, build(db)) == {"inserted": 1, "closed": 2, "unchanged": 0}
    active = [s for s in _stored(db) if s[3] is None]
    assert [(k, p["visit_count"]) for k, p, _r, _v in active] == [("interest:tc_hobby:2026-09", 6)]
    before = db.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type != 'browsing_interest'").fetchone()
    assert fam.persist(db, fam.Build(built_at_us=NOW_US)) == {"inserted": 0, "closed": 1, "unchanged": 0}
    assert db.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type != 'browsing_interest'").fetchone() == before


def test_persist_does_not_move_the_protection_clock(db):
    from pathlib import Path
    from topos.permissions_v2 import protection_clock
    month_of_visits(db, 0, 5, [3, 9, 17])
    db.commit()
    protection_clock.ensure_protection_clock(Path(db.execute("PRAGMA database_list").fetchone()[2]), owner_id=OWNER)
    before = protection_clock.clock_state(db)
    fam.persist(db, build(db))
    db.commit()
    fam.persist(db, fam.Build(built_at_us=NOW_US))
    db.commit()
    assert protection_clock.clock_state(db) == before


def test_an_unavailable_build_stores_nothing(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    assert fam.persist(conn, fam.build(conn, owner_id=OWNER, now_us=NOW_US)) == {"inserted": 0, "closed": 0,
                                                                                 "unchanged": 0}


@pytest.mark.asyncio
async def test_the_owner_socket_attests_visits_with_counts_only(tmp_path, monkeypatch):
    """The owner's one-time confirmation of pre-stamp visits goes through the journal lane's route, table named."""
    from pathlib import Path
    from types import SimpleNamespace

    from fastapi import FastAPI

    from topos.api import permissions_capture_receipts
    from topos.config.settings import settings as runtime_settings
    from tests.permissions_v2.test_capture_receipts import _call

    path = tmp_path / "route.db"
    conn = open_db(path)
    install(conn)
    cluster(conn, "tc_hobby", LABEL)
    month_of_visits(conn, 0, 5, [3, 9, 17], writer=None, host="private-site.example")
    conn.commit()
    runtime = SimpleNamespace(protocol=SimpleNamespace(canonical_database=Path(path),
                                                       ledger=SimpleNamespace(identity=SimpleNamespace(owner_id=OWNER))))
    monkeypatch.setattr("topos.permissions_v2.runtime.get_runtime", lambda: runtime)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", "owner-key", raising=False)
    app = FastAPI()
    app.include_router(permissions_capture_receipts.router)
    body = {"table": "activity_events", "source_id": SOURCE, "app_id": APP}

    assert (await _call(app, "POST", "/preview", socket=False, json=body)).status_code == 403
    preview = (await _call(app, "POST", "/preview", json=body)).json()
    assert preview["row_count"] == 5 and preview["table"] == "activity_events"
    assert "private-site" not in json.dumps(preview) and "browser:v" not in json.dumps(preview)
    assert build(conn).objects == []
    attested = await _call(app, "POST", "/attest", json={**body, "preview_digest": preview["preview_digest"],
                                                         "confirm": True})
    assert attested.status_code == 200, attested.text
    assert only(conn).band == "low"
    assert conn.execute("SELECT COUNT(*) FROM activity_events WHERE writer_class IS NOT NULL").fetchone()[0] == 0
    conn.close()
