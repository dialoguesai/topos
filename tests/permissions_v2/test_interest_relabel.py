"""OD-52 P7: a bad label gets a second try instead of dropping the interest (owner direction, 1 Oct 2026).

protects: two things at once. The owner's rule that a label which names a site, echoes a page title or is not a
short topic name must not exclude an interest by itself: such a cluster is asked about again, a bounded number of
times. And the boundary that rule must not move: a second label is one more candidate for exactly the checks the
first one failed, so a label naming a site, a page title, a person, an excluded entity, an Off-limits name or a
part of one is refused however many times it is offered; a cluster the owner explicitly excluded is never asked
about; and a stored second label is used only while it still passes on the rows as they are now.

The model is a stand-in everywhere: no test here calls a real one. All browsing is synthetic.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2 import interest_index as ii
from topos.permissions_v2 import interest_relabel as rl
from topos.permissions_v2 import interest_review as ir
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.shadow_labeler_local import MODEL
from topos.storage.db.write_gate import db_write_lock

from tests.permissions_v2.interest_fixtures import (NOW_US, OWNER, at, attest_app, cluster, install, month_of_visits,
                                                    open_db, visit)

OWN = "velocipedia / bikes"          # the cluster's own label: it names the site its pages are on
HOST = "velocipedia.example"
GOOD = "cycling gear reviews"
TITLE = "Wobbly bookshelf fixes for renters"
FORBIDDEN = ("https://", "Synthetic page", "browser:v", ".example", TITLE)


@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "relabel.db")
    install(conn)
    attest_app(conn)
    cluster(conn, "tc_site", OWN)
    month_of_visits(conn, 0, 5, [3, 9, 17], cluster_id="tc_site", host=HOST)
    visit(conn, 20, at(8, 21), cluster_id="tc_site", host=HOST, title=TITLE)
    conn.commit()
    yield conn
    conn.close()


class Model:
    """The local model, replaced: it answers in order and records what it was asked. Never under the write gate."""
    base_url = "http://127.0.0.1:11434"

    def __init__(self, *answers, model=MODEL, done=True):
        self.client, self.answers, self.model, self.done, self.requests = self, list(answers), model, done, []

    async def verify(self):
        pass

    async def post(self, url, **kwargs):
        assert not db_write_lock()._is_owned()
        self.requests.append(kwargs["json"])
        answer = self.answers.pop(0)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"model": self.model, "done": self.done, "message": {"content": content}})

    def asked(self, n=-1):
        return json.loads(self.requests[n]["messages"][1]["content"])


def build(conn, **kwargs):
    return fam.build(conn, owner_id=OWNER, now_us=NOW_US, **kwargs)


def august(conn, cluster_id="tc_site", **kwargs):
    return next(c for c in build(conn, **kwargs).candidates if c.cluster_id == cluster_id and c.month == "2026-08")


def run(conn, *answers, opt_outs=frozenset(), **kwargs):
    """Every owed cluster gets its tries from a model that gives `answers` in order."""
    model = Model(*({"label": answer} if isinstance(answer, str) else answer for answer in answers))
    counts = asyncio.run(rl.relabel_pending(conn, owner_id=OWNER, now_us=NOW_US, boundary=EntityBoundary(conn),
                                            opt_outs=opt_outs, transport=model, now=1_700_000_000, **kwargs))
    conn.commit()
    return counts, model


def row(conn, cluster_id="tc_site", label=OWN):
    return rl.stored(conn, owner_id=OWNER, base_revision=fam.label_revision(cluster_id, label))


def table_text(conn) -> str:
    return json.dumps(conn.execute(f"SELECT * FROM {rl.TABLE}").fetchall()) if rl.installed(conn) else ""


def off_limits(conn, name="Pemberly Hollis"):
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
                 "rebuild_state) VALUES ('bh-' || hex(randomblob(4)),'',?,?,'complete')", (name, name.lower()))
    conn.commit()


def person(conn, mentioned_by=None):
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json) "
                 "VALUES ('p-1','person','Orla Quennell','orla quennell','[]')")
    if mentioned_by:
        conn.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, canonical_table) "
                     "VALUES ('mn-1','p-1',?,'activity_events')", (mentioned_by,))
    conn.commit()


def excluded_entity(conn):
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
                 "VALUES ('o-1','org','Nimbus Guild','nimbus guild')")
    conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                 "VALUES ('x1','entity','o-1')")
    conn.commit()


# --- who is owed a second try ---------------------------------------------------------------------------

def test_a_label_that_names_a_site_withholds_the_month_and_is_owed_a_second_try(db):
    built = build(db)
    assert built.objects == [] and august(db).label_withheld == "label_host"
    (retry,) = built.label_retries
    assert (retry.cluster_id, retry.rules, retry.second_label) == ("tc_site", ("label_host",), None)
    (prepared,) = rl.pending(db, owner_id=OWNER, built=built)
    assert prepared["tries"] == 0 and prepared["base_revision"] == fam.label_revision("tc_site", OWN)
    assert prepared["input"] == {"name": OWN, "rules": ["label_host"], "site_words": ["velocipedia"], "last": None}


@pytest.mark.parametrize("label,rules", [
    (TITLE[:24], ("label_title",)),                                   # the clustering's fallback: a title prefix
    ("https://velocipedia.example/bikes", ("label_form", "label_host")),
    ("velocipedia bike reviews and long rides and tours of the alps", ("label_form", "label_host")),
])
def test_each_form_rule_is_a_bad_name_not_an_exclusion(db, label, rules):
    cluster(db, "tc_site", label)
    db.commit()
    (retry,) = build(db).label_retries
    assert retry.rules == rules and august(db).label_withheld == rules[0]


def test_a_label_naming_a_person_and_a_site_is_asked_about_and_the_model_is_told_both(db):
    """The own label is discarded either way; what is released must not name a person, and that is checked on
    the answer."""
    person(db, mentioned_by="browser:v2")
    cluster(db, "tc_site", "velocipedia / quennell rides")
    db.commit()
    (prepared,) = rl.pending(db, owner_id=OWNER, built=build(db))
    assert prepared["input"]["rules"] == ["label_host", "label_person"]


def test_a_well_formed_label_that_names_a_person_is_not_a_bad_name(db):
    """`label_person` alone is not a form rule: the month stays withheld and nothing is asked."""
    person(db, mentioned_by="browser:v2")
    cluster(db, "tc_site", "quennell / interviews")
    db.commit()
    assert august(db).label_withheld == "label_person" and build(db).label_retries == []
    assert run(db)[0] == {"pending": 0, "calls": 0, "relabelled": 0, "failed": 0}


@pytest.mark.parametrize("case", ["offlimits", "offlimits_name_part", "excluded_entity", "excluded_cluster",
                                  "opted_out"])
def test_a_cluster_the_owner_excluded_is_never_asked_about(db, case):
    """NEVER_RETRIED: the own label breaks a form rule and is explicitly excluded as well. No second label."""
    opt_outs = frozenset()
    if case == "offlimits":
        off_limits(db)
        cluster(db, "tc_site", "velocipedia / pemberly hollis")
    elif case == "offlimits_name_part":
        off_limits(db)
        cluster(db, "tc_site", "velocipedia / hollis rides")
    elif case == "excluded_entity":
        excluded_entity(db)
        cluster(db, "tc_site", "velocipedia / nimbus meetups")
    elif case == "excluded_cluster":
        db.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                   "VALUES ('x2','record','tc_site')")
    else:
        opt_outs = frozenset({fam.opt_out_key("tc_site")})
    db.commit()
    built = build(db, opt_outs=opt_outs)
    assert august(db, opt_outs=opt_outs).label_withheld == "label_host"       # the first failing check, as before
    assert built.label_retries == [] and rl.pending(db, owner_id=OWNER, built=built) == []
    counts, model = run(db, GOOD, opt_outs=opt_outs)
    assert counts["calls"] == 0 and model.requests == [] and not rl.installed(db)


def test_the_rules_that_earn_a_second_try_and_the_ones_that_never_do_are_named(db):
    assert fam.RETRY_CHECKS == ("label_form", "label_host", "label_title")
    assert fam.NEVER_RETRIED == ("excluded_label", "opted_out", "offlimits")
    assert set(fam.RETRY_CHECKS) | set(fam.NEVER_RETRIED) | {"label_person", "browsing"} == set(fam.LABEL_CHECKS)
    assert rl.RETRIES == 2


@pytest.mark.parametrize("label", ["", "topic cluster", "12345", "x" * 300])
def test_a_label_with_nothing_to_read_is_not_asked_about(db, label):
    db.execute("UPDATE topic_clusters SET label=? WHERE cluster_id='tc_site'", (label,))
    db.commit()
    assert august(db).label_withheld == "label_form" and build(db).label_retries == []


def test_no_month_a_label_could_serve_means_no_try(db):
    db.execute("DELETE FROM activity_events WHERE event_id IN ('browser:v3','browser:v4','browser:v20')")
    db.execute("DELETE FROM topic_cluster_members WHERE record_id IN ('browser:v3','browser:v4','browser:v20')")
    db.commit()
    assert not august(db).qualifies() and build(db).label_retries == []      # below the threshold
    month_of_visits(db, 100, 3, [3, 9, 17], cluster_id="tc_site", host=HOST)
    cluster(db, "tc_site", OWN, other_members=20)                            # qualifies, but not mostly browsing
    db.commit()
    assert august(db).label_withheld == "browsing" and build(db).label_retries == []


def test_a_cluster_whose_only_month_has_an_off_limits_page_is_not_asked_about(db):
    off_limits(db)
    visit(db, 30, at(8, 22), cluster_id="tc_site", host=HOST, title="An evening with Pemberly Hollis")
    db.commit()
    built = build(db)
    assert len(built.label_retries) == 1 and built.label_retries[0].clear() is False
    assert rl.pending(db, owner_id=OWNER, built=built) == []
    month_of_visits(db, 100, 5, [1, 5, 19], month=9, cluster_id="tc_site", host=HOST)   # a clean month as well
    db.commit()
    assert len(rl.pending(db, owner_id=OWNER, built=build(db))) == 1


# --- the tries --------------------------------------------------------------------------------------------

def test_the_first_answer_passes_and_the_interest_is_built_with_it(db):
    counts, model = run(db, GOOD)
    assert counts == {"pending": 1, "calls": 1, "relabelled": 1, "failed": 0}
    built = build(db)
    (obj,) = built.objects
    assert (obj.cluster_id, obj.month, obj.label, obj.band) == ("tc_site", "2026-08", GOOD, "low")
    assert obj.label_revision == fam.label_revision("tc_site", GOOD)
    assert august(db).label_withheld is None and august(db).relabelled and built.label_retries == []
    result = row(db)
    assert (result.tries, result.label, result.refused) == (1, GOOD, None)
    counts, model = run(db, "anything")                       # nothing is owed: nothing is asked
    assert counts["pending"] == 0 and model.requests == []


def test_the_first_answer_fails_and_the_second_passes(db):
    counts, model = run(db, "Velocipedia bike reviews", GOOD)
    assert counts == {"pending": 1, "calls": 2, "relabelled": 1, "failed": 0}
    assert model.asked(1)["last"] == {"rule": "label_host", "label": "Velocipedia bike reviews"}
    assert model.asked(1)["name"] == OWN and model.asked(0)["last"] is None
    result = row(db)
    assert (result.tries, result.label) == (2, GOOD)
    assert build(db).objects[0].label == GOOD


def test_every_try_fails_and_the_month_is_withheld_with_the_same_code_for_good(db):
    counts, _model = run(db, "velocipedia", "the velocipedia site")
    assert counts == {"pending": 1, "calls": 2, "relabelled": 0, "failed": 0}
    result = row(db)
    assert (result.tries, result.label, result.refused) == (2, None, "label_host")
    built = build(db)
    assert built.objects == [] and august(db).label_withheld == "label_host"
    assert rl.pending(db, owner_id=OWNER, built=built) == []   # still listed as a bad name, with no tries left
    counts, model = run(db, GOOD)                              # a third answer is never asked for
    assert counts == {"pending": 0, "calls": 0, "relabelled": 0, "failed": 0} and model.requests == []


def test_calls_stop_at_the_limit_and_the_try_resumes_later_told_the_rule_alone(db):
    """A budget that runs out between two tries: the first is kept, the second is made in a later run. The
    refused answer was never stored, so the resumed try is told which rule it broke and nothing more."""
    counts, _model = run(db, "velocipedia reviews", limit=1)
    assert counts == {"pending": 1, "calls": 1, "relabelled": 0, "failed": 0}
    assert (row(db).tries, row(db).refused) == (1, "label_host") and august(db).label_withheld == "label_host"
    counts, model = run(db, GOOD, limit=1)
    assert counts == {"pending": 1, "calls": 1, "relabelled": 1, "failed": 0}
    assert model.asked()["last"] == {"rule": "label_host", "label": None}
    assert row(db).tries == 2 and build(db).objects[0].label == GOOD


# --- a second label is held to every check the first was ----------------------------------------------------

@pytest.mark.parametrize("answer,rule,shown", [
    ("pemberly hollis interviews", "offlimits", None),                 # an Off-limits name
    ("hollis woodworking videos", "offlimits", None),                  # a bare part of one
    ("nimbus guild meetups", "excluded_label", None),                  # an excluded entity
    ("orla quennell interviews", "label_person", "orla quennell interviews"),   # a person entity's name
    ("quennell cycling talks", "label_person", "quennell cycling talks"),       # ...or a word of one a visit mentions
    ("velocipedia reviews", "label_host", "velocipedia reviews"),      # the site again
    ("Wobbly bookshelf fixes", "label_title", "Wobbly bookshelf fixes"),   # the words of a page title
    ("https://example.test/bikes", "label_form", "https://example.test/bikes"),
    ("one two three four five six seven eight nine", "label_form", "one two three four five six seven eight nine"),
    ("", "label_form", ""),                                            # "no topic can be named"
])
def test_a_second_label_that_breaks_any_check_is_refused(db, answer, rule, shown):
    off_limits(db)
    person(db, mentioned_by="browser:v2")
    excluded_entity(db)
    counts, model = run(db, answer, answer)
    assert counts == {"pending": 1, "calls": 2, "relabelled": 0, "failed": 0}
    result = row(db)
    assert (result.label, result.refused, result.tries) == (None, rule, 2)
    assert build(db).objects == [] and august(db).label_withheld == "label_host"
    # The next try is told the rule and shown its own previous answer, so that it can answer differently;
    # except an answer that named something the owner excluded, which is never put back in front of the model.
    # No refused answer is ever stored.
    assert model.asked(1)["last"] == {"rule": rule, "label": shown}
    if answer:
        assert answer not in table_text(db)


def test_an_answer_is_judged_as_the_index_build_will_judge_it(db):
    """Publication builds one cluster; the index builds all. A word of the name of a person whom only another
    cluster's visit mentions is refused by both, so a label is never accepted that no index build can use (it
    would be asked about again at every refresh, and every answer dropped)."""
    person(db)
    cluster(db, "tc_other", "trail running / shoes")
    month_of_visits(db, 100, 5, [2, 8, 16], cluster_id="tc_other")
    db.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, canonical_table) "
               "VALUES ('mn-9','p-1','browser:v100','activity_events')")
    db.commit()
    counts, model = run(db, "quennell cycling talks", GOOD)
    assert counts == {"pending": 1, "calls": 2, "relabelled": 1, "failed": 0}
    assert model.asked(1)["last"]["rule"] == "label_person"
    assert [o.label for o in build(db).objects if o.cluster_id == "tc_site"] == [GOOD]
    assert run(db, "anything")[0]["calls"] == 0


@pytest.mark.parametrize("answer", ["velocipedia hollis rides", "velocipedia nimbus guild rides"])
def test_an_answer_that_breaks_a_form_rule_and_an_exclusion_is_not_shown_again(db, answer):
    """The first rule it broke is a form rule; what decides whether it is shown is every rule it broke."""
    off_limits(db)
    excluded_entity(db)
    _counts, model = run(db, answer, GOOD)
    assert model.asked(1)["last"] == {"rule": "label_host", "label": None}


@pytest.mark.parametrize("raw", ["not json", "[]", {"name": "cycling"}, {"label": 3}, {"label": "cycling", "why": "x"}])
def test_an_answer_that_is_not_a_label_is_a_try_that_broke_the_form_rule(db, raw):
    model = Model(raw, {"label": GOOD})
    counts = asyncio.run(rl.relabel_pending(db, owner_id=OWNER, now_us=NOW_US, boundary=EntityBoundary(db),
                                            transport=model, now=1))
    assert counts == {"pending": 1, "calls": 2, "relabelled": 1, "failed": 0}
    assert model.asked(1)["last"] == {"rule": "label_form", "label": None} and row(db).tries == 2


def test_whitespace_in_an_answer_is_collapsed_and_nothing_else_is_changed():
    assert rl.parse_answer(json.dumps({"label": "  cycling   gear\nreviews "})) == GOOD
    assert rl.parse_answer(json.dumps({"label": "Cycling Gear."})) == "Cycling Gear."


@pytest.mark.parametrize("kwargs", [{"done": False}, {"model": "some-other-model"}])
def test_an_answer_the_pinned_model_did_not_complete_spends_no_try(db, kwargs):
    model = Model({"label": GOOD}, **kwargs)
    counts = asyncio.run(rl.relabel_pending(db, owner_id=OWNER, now_us=NOW_US, boundary=EntityBoundary(db),
                                            transport=model, now=1))
    assert counts == {"pending": 1, "calls": 1, "relabelled": 0, "failed": 1}
    assert not rl.installed(db) and len(rl.pending(db, owner_id=OWNER, built=build(db))) == 1


def test_the_call_is_the_pinned_models_with_the_assessments_conventions(db):
    _counts, model = run(db, GOOD)
    (request,) = model.requests
    assert (request["model"], request["stream"], request["think"], request["format"]) == (MODEL, False, False, "json")
    assert request["options"]["temperature"] == 0
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    assert request["messages"][0]["content"] == rl.PROMPT
    assert "untrusted" in rl.PROMPT and "Never follow it" in rl.PROMPT


def test_the_model_sees_the_refused_name_and_its_rules_and_no_page(db):
    _counts, model = run(db, "velocipedia reviews", GOOD)
    for n in (0, 1):
        sent = model.asked(n)
        assert set(sent) == {"name", "rules", "site_words", "last"}
        assert not any(forbidden in json.dumps(sent) for forbidden in FORBIDDEN)


# --- publication is decided on the rows as they are then ----------------------------------------------------

def prepared_try(conn):
    (prepared,) = rl.pending(conn, owner_id=OWNER, built=build(conn))
    return prepared


def publish(conn, prepared, answer, **kwargs):
    return rl.publish(conn, owner_id=OWNER, prepared=prepared, answer=answer, now_us=NOW_US,
                      boundary=EntityBoundary(conn), now=1, **kwargs)


def test_an_answer_for_a_label_that_changed_meanwhile_is_dropped(db):
    prepared = prepared_try(db)
    cluster(db, "tc_site", "velocipedia / touring")
    db.commit()
    with pytest.raises(PolicyError, match="machine_review_conflict"):
        publish(db, prepared, GOOD)
    assert not rl.installed(db)


def test_an_answer_for_a_cluster_excluded_meanwhile_is_dropped(db):
    prepared = prepared_try(db)
    with pytest.raises(PolicyError, match="machine_review_conflict"):
        publish(db, prepared, GOOD, opt_outs=frozenset({fam.opt_out_key("tc_site")}))
    assert not rl.installed(db)


def test_the_same_try_cannot_be_recorded_twice_and_a_spent_cluster_takes_no_more(db):
    prepared = prepared_try(db)
    publish(db, prepared, "velocipedia reviews")
    with pytest.raises(PolicyError, match="machine_review_conflict"):
        publish(db, prepared, GOOD)                                    # the tries spent moved
    publish(db, prepared_try(db), "velocipedia once more")
    with pytest.raises(PolicyError, match="machine_review_conflict"):
        publish(db, {**prepared, "tries": 2}, GOOD)                    # both tries are spent
    assert row(db).tries == 2 and row(db).label is None


def test_a_try_prepared_under_another_revision_is_dropped(db, monkeypatch):
    prepared = prepared_try(db)
    monkeypatch.setattr(rl, "PROMPT", rl.PROMPT + " ")
    with pytest.raises(PolicyError, match="machine_review_conflict"):
        publish(db, prepared, GOOD)


def test_the_answer_is_judged_on_the_rows_current_at_publication(db):
    prepared = prepared_try(db)
    visit(db, 40, at(8, 23), cluster_id="tc_site", host="cyclinggear.example")     # a new site while the model ran
    db.commit()
    result, broken = publish(db, prepared, GOOD)
    assert broken == ("label_host",) and result.label is None


# --- a stored second label is used only while it still passes ------------------------------------------------

def test_a_second_label_that_stops_passing_withholds_the_month_and_has_one_try_left(db):
    run(db, GOOD)
    visit(db, 40, at(8, 23), cluster_id="tc_site", host=HOST, title="Cycling gear reviews for commuters")
    db.commit()
    built = build(db)
    assert built.objects == [] and august(db).label_withheld == "label_host" and not august(db).relabelled
    (retry,) = built.label_retries
    assert (retry.second_label, retry.second_rules) == (GOOD, ("label_title",))
    counts, model = run(db, "bicycle commuting")
    assert counts == {"pending": 1, "calls": 1, "relabelled": 1, "failed": 0}
    assert model.asked()["last"] == {"rule": "label_title", "label": GOOD}
    assert build(db).objects[0].label == "bicycle commuting" and row(db).tries == 2
    visit(db, 41, at(8, 24), cluster_id="tc_site", host=HOST, title="Bicycle commuting in winter")
    db.commit()
    assert build(db).objects == [] and run(db, "anything")[0]["calls"] == 0        # both tries are spent


def test_a_second_label_the_owner_has_since_excluded_is_erased_and_never_shown_again(db):
    run(db, "hollis style touring bikes")
    assert build(db).objects[0].label == "hollis style touring bikes"
    off_limits(db)
    built = build(db)
    base = fam.label_revision("tc_site", OWN)
    assert built.objects == [] and august(db).label_withheld == "label_host"
    assert built.second_unusable == {base: "offlimits"}
    (prepared,) = rl.pending(db, owner_id=OWNER, built=built)
    assert prepared["input"]["last"] == {"rule": "offlimits", "label": None}
    assert rl.prune(db, owner_id=OWNER, built=built) == {"deleted": 0, "erased": 1}
    db.commit()
    assert "hollis" not in table_text(db)
    assert (row(db).label, row(db).refused, row(db).tries) == (None, "offlimits", 1)
    assert rl.prune(db, owner_id=OWNER, built=build(db)) == {"deleted": 0, "erased": 0}


def test_a_second_label_that_comes_to_name_a_person_is_withheld_kept_and_may_be_answered_again(db):
    """Naming a person entity withholds the label like any failed check, but it is not one of the owner's
    explicit exclusions: the text is not erased, and the one try left is shown it."""
    run(db, "quennell cycling routes")
    assert build(db).objects[0].label == "quennell cycling routes"
    person(db, mentioned_by="browser:v2")
    built = build(db)
    assert built.objects == [] and august(db).label_withheld == "label_host" and built.second_unusable == {}
    assert rl.prune(db, owner_id=OWNER, built=built) == {"deleted": 0, "erased": 0}
    counts, model = run(db, GOOD)
    assert counts["relabelled"] == 1
    assert model.asked()["last"] == {"rule": "label_person", "label": "quennell cycling routes"}


def test_a_second_label_is_erased_when_the_own_label_is_excluded(db):
    run(db, GOOD)
    built = build(db, opt_outs=frozenset({fam.opt_out_key("tc_site")}))
    assert built.second_unusable == {fam.label_revision("tc_site", OWN): "opted_out"} and built.label_retries == []
    assert rl.prune(db, owner_id=OWNER, built=built) == {"deleted": 0, "erased": 1}
    assert GOOD not in table_text(db)


def test_a_second_label_is_erased_when_the_own_label_comes_to_name_an_off_limits_person(db):
    """The second label itself names nothing excluded; the cluster's own label does, now. The cluster is the
    owner's explicit exclusion from then on: withheld, never asked about again, and its second label not kept."""
    cluster(db, "tc_site", "velocipedia / hollis rides")
    db.commit()
    run(db, GOOD)
    assert build(db).objects[0].label == GOOD
    off_limits(db)
    built = build(db)
    base = fam.label_revision("tc_site", "velocipedia / hollis rides")
    assert built.objects == [] and august(db).label_withheld == "label_host" and built.label_retries == []
    assert built.second_unusable == {base: "offlimits"}
    assert rl.prune(db, owner_id=OWNER, built=built) == {"deleted": 0, "erased": 1}
    assert GOOD not in table_text(db) and run(db, "anything")[0]["calls"] == 0


def test_a_relabelled_cluster_leaves_its_old_result_behind_and_prune_deletes_it(db):
    run(db, GOOD)
    cluster(db, "tc_site", "velocipedia / touring")
    db.commit()
    built = build(db)
    assert built.objects == [] and len(rl.pending(db, owner_id=OWNER, built=built)) == 1    # a new label: new tries
    assert rl.prune(db, owner_id=OWNER, built=build(db, clusters=["tc_site"])) == {"deleted": 0, "erased": 0}
    assert rl.prune(db, owner_id=OWNER, built=built) == {"deleted": 1, "erased": 0}
    assert GOOD not in table_text(db)


def test_a_result_of_another_revision_is_not_used_and_is_tried_afresh_once(db, monkeypatch):
    run(db, GOOD)
    monkeypatch.setattr(rl, "PROMPT", rl.PROMPT + " ")
    assert rl.accepted(db, owner_id=OWNER) == {} and row(db) is None and build(db).objects == []
    (prepared,) = rl.pending(db, owner_id=OWNER, built=build(db))
    assert prepared["tries"] == 0
    assert rl.prune(db, owner_id=OWNER, built=build(db)) == {"deleted": 1, "erased": 0}


def test_another_model_makes_a_result_stale(db, monkeypatch):
    run(db, GOOD)
    monkeypatch.setattr(rl, "_model_revision", lambda: "f" * 64)
    assert rl.accepted(db, owner_id=OWNER) == {} and build(db).objects == []


def test_another_owners_or_a_tampered_result_is_not_used(db):
    run(db, GOOD)
    assert rl.accepted(db, owner_id="someone-else") == {}
    base = fam.label_revision("tc_site", OWN)
    body = db.execute(f"SELECT relabel_json FROM {rl.TABLE}").fetchone()[0]
    # The owner is checked twice, on the row and in its body; each check holds without the other.
    db.execute(f"UPDATE {rl.TABLE} SET owner_id='someone-else'")           # filed under another owner
    assert rl.accepted(db, owner_id=OWNER) == {} and row(db) is None
    assert rl.accepted(db, owner_id="someone-else") == {}                  # ...whose body still names this one
    db.execute(f"UPDATE {rl.TABLE} SET owner_id=?, relabel_json=?", (OWNER, body.replace(OWNER, "someone-else")))
    assert rl.accepted(db, owner_id=OWNER) == {} and row(db) is None
    db.execute(f"UPDATE {rl.TABLE} SET relabel_json=?", (body,))
    assert rl.accepted(db, owner_id=OWNER) == {base: GOOD}
    other = fam.label_revision("tc_site", "velocipedia / touring")
    db.execute(f"UPDATE {rl.TABLE} SET base_revision=?", (other,))        # filed under another label
    assert rl.accepted(db, owner_id=OWNER) == {}
    db.execute(f"UPDATE {rl.TABLE} SET base_revision=?, relabel_json=?", (base, body.replace(GOOD, "x" * 80)))
    assert rl.accepted(db, owner_id=OWNER) == {}                          # not a label a result may hold
    db.execute(f"UPDATE {rl.TABLE} SET relabel_json='{{}}'")
    assert rl.accepted(db, owner_id=OWNER) == {} and build(db).objects == []


def test_a_stored_label_is_never_used_for_a_cluster_whose_own_label_passes(db):
    """A second label stands in for a bad name only. A good own label is the label."""
    run(db, GOOD)
    db.execute("UPDATE activity_events SET hostname='cycleshop.example', url=replace(url, 'velocipedia', 'cycleshop')")
    db.commit()
    assert build(db).objects[0].label == OWN and not august(db).relabelled


def test_with_nothing_stored_the_build_is_what_it_was(db):
    """No table, no second label: every candidate and object is the one the family built before this module,
    and building creates nothing."""
    cluster(db, "tc_hobby", "sourdough / baking / starter")
    month_of_visits(db, 100, 5, [3, 9, 17])
    db.commit()
    assert not rl.installed(db) and rl.accepted(db, owner_id=OWNER) == {}
    built = build(db)
    assert [(c.cluster_id, c.month, c.label_withheld, c.label, c.relabelled) for c in built.candidates] == [
        ("tc_hobby", "2026-08", None, "sourdough / baking / starter", False),
        ("tc_site", "2026-08", "label_host", OWN, False)]
    assert [(o.cluster_id, o.label) for o in built.objects] == [("tc_hobby", "sourdough / baking / starter")]
    assert built.second_unusable == {} and not rl.installed(db)
    assert rl.prune(db, owner_id=OWNER, built=built) == {"deleted": 0, "erased": 0} and not rl.installed(db)


# --- through the index: assessed, admitted, released, and decided again at release ---------------------------

def test_a_second_label_is_assessed_admitted_and_released_and_the_own_label_never_leaves(db, monkeypatch):
    from tests.permissions_v2.test_interest_index import KEY, NOW, policy
    from tests.permissions_v2.test_interest_review import ANSWER, Transport
    monkeypatch.setenv(ii.FLAG, "1")
    run(db, GOOD)
    boundary = EntityBoundary(db)
    (obj,) = build(db).objects
    prepared = ir.prepare(obj, boundary)
    labels = asyncio.run(ir.assess(prepared, transport=Transport(ANSWER)))
    assert prepared["input"]["target"] == GOOD                 # what is assessed is the second label
    ir.publish(db, owner_id=OWNER, prepared=prepared, classification=labels, boundary=boundary, now=1)
    db.commit()
    (entry,) = ii.members(db, owner_id=OWNER, policy=policy(), now=NOW, boundary=boundary)
    assert entry["rank_text"] == GOOD and entry["record_id"] == "interest:tc_site:2026-08"
    released = ii.release(db, entry, key=KEY, grant_id="grant-search", owner_id=OWNER, policy=policy(), now=NOW,
                          boundary=boundary)
    assert (released["label"], released["content"], released["month"]) == (GOOD, GOOD, "2026-08")
    assert "velocipedia" not in json.dumps(released) and OWN not in json.dumps(entry)
    # Release decides again from the rows: the second label gone, or no longer passing, releases nothing.
    visit(db, 40, at(8, 23), cluster_id="tc_site", host=HOST, title="Cycling gear reviews for commuters")
    db.commit()
    assert ii.release(db, entry, key=KEY, grant_id="grant-search", owner_id=OWNER, policy=policy(), now=NOW,
                      boundary=EntityBoundary(db)) is None
    assert ii.indexed_current(db, [entry], owner_id=OWNER, boundary=EntityBoundary(db)) == frozenset()


@pytest.mark.parametrize("value, on", [(None, True), ("", True), ("on", True), ("true", True), ("1", True),
                                       ("off", False), ("0", False), ("false", False), ("no", False), (" OFF ", False)])
def test_the_switch_is_on_unless_it_is_set_off(value, on):
    assert rl.enabled({} if value is None else {rl.FLAG: value}) is on
    assert rl.FLAG == "TOPOS_PERMISSIONS_V2_INTEREST_RELABEL"


def test_with_the_switch_off_the_family_builds_from_each_clusters_own_label_alone(db, monkeypatch):
    """Off: a stored second label stands in for nothing and is not even read, and no cluster is listed as owed
    one. The candidates and objects are the ones the family built before this module."""
    cluster(db, "tc_hobby", "sourdough / baking / starter")
    month_of_visits(db, 100, 5, [3, 9, 17])
    db.commit()
    before = build(db)
    assert [(o.cluster_id, o.label) for o in before.objects] == [("tc_hobby", "sourdough / baking / starter")]
    run(db, GOOD)
    assert sorted((o.cluster_id, o.label) for o in build(db).objects) == [
        ("tc_hobby", "sourdough / baking / starter"), ("tc_site", GOOD)]
    monkeypatch.setenv(rl.FLAG, "off")
    monkeypatch.setattr(rl, "accepted", lambda conn, *, owner_id: pytest.fail("read with the switch off"))
    off = build(db)
    assert off.candidates == before.candidates and off.objects == before.objects
    assert off.label_retries == [] and off.second_unusable == {} and off.own_revisions == set()
    assert august(db).label_withheld == "label_host" and not august(db).relabelled


def test_with_the_switch_off_a_released_second_label_stops_releasing(db, monkeypatch):
    from tests.permissions_v2.test_interest_index import KEY, NOW, policy
    from tests.permissions_v2.test_interest_review import ANSWER, Transport
    monkeypatch.setenv(ii.FLAG, "1")
    run(db, GOOD)
    boundary = EntityBoundary(db)
    (obj,) = build(db).objects
    prepared = ir.prepare(obj, boundary)
    ir.publish(db, owner_id=OWNER, prepared=prepared, boundary=boundary, now=1,
               classification=asyncio.run(ir.assess(prepared, transport=Transport(ANSWER))))
    db.commit()
    (entry,) = ii.members(db, owner_id=OWNER, policy=policy(), now=NOW, boundary=boundary)
    release = lambda: ii.release(db, entry, key=KEY, grant_id="grant-search", owner_id=OWNER, policy=policy(),
                                 now=NOW, boundary=EntityBoundary(db))
    assert release()["label"] == GOOD
    monkeypatch.setenv(rl.FLAG, "off")
    assert release() is None
    assert ii.members(db, owner_id=OWNER, policy=policy(), now=NOW, boundary=EntityBoundary(db)) == []
    assert ii.indexed_current(db, [entry], owner_id=OWNER, boundary=EntityBoundary(db)) == frozenset()
    monkeypatch.delenv(rl.FLAG)
    assert release()["label"] == GOOD                          # nothing was erased: back on, it stands in again


def test_with_the_flag_off_a_stored_second_label_reaches_no_grant(db, monkeypatch):
    from tests.permissions_v2.test_interest_index import NOW, policy
    monkeypatch.delenv(ii.FLAG, raising=False)
    run(db, GOOD)
    assert ii.members(db, owner_id=OWNER, policy=policy(), now=NOW, boundary=EntityBoundary(db)) == []
