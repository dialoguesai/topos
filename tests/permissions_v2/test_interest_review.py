"""OD-52 P7: an interest label is assessed as a short text before it can be a member.

protects: the one text a browsing interest releases. The label is assessed under the shared rubric
vocabulary and the message floors, plus a special-category cue floor; only a current assessment of
this exact label, by the pinned model and this rubric, under the same protected vocabulary, saying
sensitivity none or personal with no protected content, admits it. A special or unknown sensitivity
withholds. Protected content (floors v2, the owner's rule of 1 Oct 2026): the model's own `unknown`
is read as `none`, so its uncertainty alone excludes nothing; an Off-limits term in the label and
the model's own `present` still withhold, and nothing a deterministic floor decided is lowered.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2 import interest_review as ir
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.shadow_labeler_local import MODEL
from topos.storage.db.write_gate import db_write_lock

from tests.permissions_v2.interest_fixtures import (NOW_US, OWNER, attest_app, cluster, install, month_of_visits,
                                                    open_db)

LABEL = "sourdough / baking / starter"


@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "review.db")
    install(conn)
    attest_app(conn)
    cluster(conn, "tc_hobby", LABEL)
    month_of_visits(conn, 0, 5, [3, 9, 17])
    month_of_visits(conn, 100, 5, [1, 5, 19], month=9)
    conn.commit()
    yield conn
    conn.close()


def objects(conn):
    return fam.build(conn, owner_id=OWNER, now_us=NOW_US).objects


class Transport:
    base_url = "http://127.0.0.1:11434"

    def __init__(self, answer, *, model=MODEL, done=True):
        self.client, self.answer, self.model, self.done, self.requests = self, answer, model, done, []

    async def verify(self):
        pass

    async def post(self, url, **kwargs):
        assert not db_write_lock()._is_owned()
        self.requests.append(kwargs["json"])
        content = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"model": self.model, "done": self.done, "message": {"content": content}})


ANSWER = {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "none"}


def assess_all(conn, answer=ANSWER, **kwargs):
    boundary = EntityBoundary(conn)
    transport = Transport(answer, **kwargs)
    counts = asyncio.run(ir.assess_pending(conn, owner_id=OWNER, objects=objects(conn), boundary=boundary,
                                           transport=transport, now=1_700_000_000))
    conn.commit()
    return counts, transport


def current(conn, obj):
    revision, _ = ir.context(EntityBoundary(conn))
    return ir.current(conn, owner_id=OWNER, obj=obj, context_revision=revision)


def test_one_assessment_per_label_covers_every_month(db):
    counts, transport = assess_all(db)
    assert counts == {"pending": 1, "assessed": 1, "failed": 0}
    request = transport.requests[0]
    assert json.loads(request["messages"][1]["content"]) == {"target": LABEL, "protected_terms": [],
                                                              "before": [], "after": []}
    assert request["model"] == MODEL and ir.PROMPT in request["messages"][0]["content"]
    assert all(ir.qualifies(current(db, o)) for o in objects(db))
    assert assess_all(db)[0] == {"pending": 0, "assessed": 0, "failed": 0}


def test_the_model_sees_only_the_label_and_the_protected_vocabulary(db):
    db.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
               "rebuild_state) VALUES ('bh-1','','Tamsin Orrery','tamsin orrery','complete')")
    db.commit()
    _counts, transport = assess_all(db)
    sent = json.loads(transport.requests[0]["messages"][1]["content"])
    assert set(sent) == {"target", "protected_terms", "before", "after"}
    assert sent["target"] == LABEL and sent["protected_terms"] == ["tamsinorrery"]
    for forbidden in ("example.test", "Synthetic page", "https://", "browser:v"):
        assert forbidden not in json.dumps(sent)


@pytest.mark.parametrize("answer,reason", [
    ({"domains": ["hobbies"], "sensitivity": "special", "protected_content": "none"}, "assessment_special"),
    ({"domains": ["hobbies"], "sensitivity": "unknown", "protected_content": "none"}, "assessment_unknown"),
    ({"domains": ["hobbies"], "sensitivity": "special", "protected_content": "unknown"}, "assessment_special"),
    ({"domains": ["hobbies"], "sensitivity": "unknown", "protected_content": "unknown"}, "assessment_unknown"),
    ({"domains": ["hobbies"], "sensitivity": "personal", "protected_content": "present"}, "assessment_protected"),
    ({"domains": ["hobbies"], "sensitivity": "none", "protected_content": "present"}, "assessment_protected"),
    ({"domains": ["hobbies"], "sensitivity": "personal", "protected_content": "none"}, None),
])
def test_special_unknown_and_protected_withhold(db, answer, reason):
    assess_all(db, answer)
    assessment = current(db, objects(db)[0])
    assert ir.withheld_reason(assessment) == reason
    assert ir.qualifies(assessment) is (reason is None)


def test_the_floors_are_v2():
    assert ir.FLOORS_VERSION == "interest-label-floors/v2"


@pytest.mark.parametrize("sensitivity", ["none", "personal"])
def test_the_models_unknown_protected_content_no_longer_withholds(db, sensitivity):
    """Floors v2: the model's uncertainty about protected content is read as `none`, at the call and again
    where the assessment is stored, so the label qualifies on its sensitivity alone."""
    answer = {"domains": ["hobbies"], "sensitivity": sensitivity, "protected_content": "unknown"}
    counts, _transport = assess_all(db, answer)
    assert counts == {"pending": 1, "assessed": 1, "failed": 0}
    for obj in objects(db):
        assessment = current(db, obj)
        assert (assessment.classification.sensitivity, assessment.classification.protected_content) == \
            (sensitivity, "none")
        assert ir.qualifies(assessment) and ir.withheld_reason(assessment) is None


def test_an_off_limits_term_in_the_label_is_present_when_the_model_says_unknown(db):
    """The deterministic floor decides after the model's `unknown` is read: a protected term in the label is
    `present`, and the label is withheld."""
    db.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
               "rebuild_state) VALUES ('bh-1','','Tamsin Orrery','tamsin orrery','complete')")
    db.commit()
    boundary = EntityBoundary(db)
    obj = objects(db)[0]
    prepared = ir.prepare(obj, boundary)
    prepared["input"]["target"] = "tamsin orrery fan pages"       # as if the label named the protected person
    labels = asyncio.run(ir.assess(prepared, transport=Transport(
        {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "unknown"})))
    assert labels.protected_content == "present"
    stored = ir.publish(db, owner_id=OWNER, prepared=prepared, boundary=boundary, classification=labels.model_copy(
        update={"protected_content": "unknown"}))
    assert stored.classification.protected_content == "present" and not ir.qualifies(stored)


def test_only_the_models_unknown_is_lowered_never_a_floors():
    """The message floor answers `unknown` when a neighbour names a protected term and the target has a pronoun.
    A label has no neighbours, so it cannot fire today; the order is pinned so that it is never lowered if it
    does. The model's `none` and the floor's `unknown` end `unknown`, and that withholds (`qualifies`)."""
    labels = ir.InterestClassification(label_revision="a" * 64, domains=["hobbies"], sensitivity="none",
                                       protected_content="unknown")
    inputs = {"target": "their woodworking videos", "protected_terms": ["tamsinorrery"],
              "before": ["tamsin orrery posted again"], "after": []}
    assert ir.apply_floors(labels, inputs).protected_content == "unknown"
    assert ir.apply_floors(labels, {**inputs, "before": []}).protected_content == "none"


def test_an_assessment_that_says_unknown_or_present_never_qualifies():
    """`qualifies` names `none` itself: it does not rely on the floors having run on what it is given."""
    for protected, admitted in (("none", True), ("unknown", False), ("present", False)):
        assessment = ir.InterestAssessment(
            version=ir.VERSION, owner_id=OWNER, cluster_id="tc_hobby", assessed_at=1, model_revision="a" * 64,
            rubric_revision="b" * 64, context_revision="c" * 64,
            classification=ir.InterestClassification(label_revision="d" * 64, domains=["hobbies"],
                                                     sensitivity="none", protected_content=protected))
        assert ir.qualifies(assessment) is admitted
        assert ir.withheld_reason(assessment) == (None if admitted else "assessment_protected")


def test_no_assessment_withholds():
    assert ir.withheld_reason(None) == "assessment_missing" and not ir.qualifies(None)


@pytest.mark.parametrize("raw", [
    "not json", [], {"domains": ["hobbies"], "sensitivity": "none"},
    {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "none", "speech": "original_message"},
    {"domains": [], "sensitivity": "none", "protected_content": "none"},
    {"domains": ["gardening"], "sensitivity": "none", "protected_content": "none"},
    {"domains": ["hobbies", "hobbies"], "sensitivity": "none", "protected_content": "none"},
    {"domains": ["hobbies"], "sensitivity": "low", "protected_content": "none"},
    {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "maybe"},
])
def test_the_vocabulary_is_closed(raw):
    with pytest.raises(PolicyError, match="machine_classification_invalid"):
        ir.parse_assessment(raw if not isinstance(raw, dict) else json.dumps(raw), "a" * 64)


def test_a_failed_or_foreign_answer_publishes_nothing(db):
    counts, _ = assess_all(db, "{}")
    assert counts == {"pending": 1, "assessed": 0, "failed": 1}
    counts, _ = assess_all(db, model="some-other-model")
    assert counts["assessed"] == 0 and counts["failed"] == 1
    counts, _ = assess_all(db, done=False)
    assert counts["assessed"] == 0
    assert all(current(db, o) is None for o in objects(db))


@pytest.mark.parametrize("label", ["therapy / clinics", "church / choir", "union / strike", "visa / asylum"])
def test_a_special_category_cue_is_special_whatever_the_model_says(label):
    labels = ir.InterestClassification(label_revision="a" * 64, domains=["hobbies"], sensitivity="none",
                                       protected_content="none")
    floored = ir.apply_floors(labels, {"target": label, "protected_terms": [], "before": [], "after": []})
    assert floored.sensitivity == "special"


def test_health_is_special_and_a_protected_term_is_present():
    labels = ir.InterestClassification(label_revision="a" * 64, domains=["health"], sensitivity="personal",
                                       protected_content="none")
    inputs = {"target": "tamsin orrery fan pages", "protected_terms": ["tamsinorrery"], "before": [], "after": []}
    floored = ir.apply_floors(labels, inputs)
    assert (floored.sensitivity, floored.protected_content) == ("special", "present")


def test_floors_never_lower_an_unknown_sensitivity():
    """An unknown sensitivity stays unknown and withholds. Only the model's protected-content `unknown` is read
    as `none` (floors v2), and reading it never touches the sensitivity."""
    labels = ir.InterestClassification(label_revision="a" * 64, domains=["hobbies"], sensitivity="unknown",
                                       protected_content="unknown")
    floored = ir.apply_floors(labels, {"target": "therapy", "protected_terms": [], "before": [], "after": []})
    assert (floored.sensitivity, floored.protected_content) == ("unknown", "none")
    for sensitivity in ("none", "personal", "special"):
        kept = ir.apply_floors(labels.model_copy(update={"sensitivity": sensitivity}),
                               {"target": "sourdough", "protected_terms": [], "before": [], "after": []})
        assert (kept.sensitivity, kept.protected_content) == (sensitivity, "none")


def test_the_models_present_is_kept_by_the_floors():
    labels = ir.InterestClassification(label_revision="a" * 64, domains=["hobbies"], sensitivity="none",
                                       protected_content="present")
    floored = ir.apply_floors(labels, {"target": "sourdough", "protected_terms": [], "before": [], "after": []})
    assert floored.protected_content == "present"


def test_a_relabel_makes_the_assessment_stale(db):
    assess_all(db)
    cluster(db, "tc_hobby", "sourdough / baking")
    db.commit()
    assert all(current(db, o) is None for o in objects(db))
    assert assess_all(db)[0]["assessed"] == 1


def test_a_new_protected_term_makes_the_assessment_stale(db):
    assess_all(db)
    db.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
               "rebuild_state) VALUES ('bh-1','','Tamsin Orrery','tamsin orrery','complete')")
    db.commit()
    assert all(current(db, o) is None for o in objects(db))


@pytest.mark.parametrize("field", ["model_revision", "rubric_revision"])
def test_another_model_or_rubric_makes_the_assessment_stale(db, monkeypatch, field):
    assess_all(db)
    obj = objects(db)[0]
    assert current(db, obj) is not None
    if field == "model_revision":
        monkeypatch.setattr(ir, "_model_revision", lambda: "f" * 64)
    else:
        monkeypatch.setattr(ir, "PROMPT", ir.PROMPT + " ")
    assert current(db, obj) is None


def test_another_owners_assessment_does_not_count(db):
    assess_all(db)
    obj = objects(db)[0]
    revision, _ = ir.context(EntityBoundary(db))
    assert ir.current(db, owner_id="someone-else", obj=obj, context_revision=revision) is None


def test_a_tampered_row_does_not_count(db):
    assess_all(db)
    db.execute(f"UPDATE {ir.TABLE} SET assessment_json='{{}}'")
    db.commit()
    assert current(db, objects(db)[0]) is None


def test_publish_refuses_a_moved_vocabulary_or_another_label(db):
    obj = objects(db)[0]
    boundary = EntityBoundary(db)
    prepared = ir.prepare(obj, boundary)
    labels = ir.InterestClassification(label_revision=obj.label_revision, **ANSWER)
    with pytest.raises(PolicyError, match="review_stale"):
        ir.publish(db, owner_id=OWNER, prepared=prepared, boundary=boundary,
                   classification=labels.model_copy(update={"label_revision": "b" * 64}))
    db.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
               "rebuild_state) VALUES ('bh-1','','Tamsin Orrery','tamsin orrery','complete')")
    db.commit()
    with pytest.raises(PolicyError, match="machine_review_conflict"):
        ir.publish(db, owner_id=OWNER, prepared=prepared, boundary=EntityBoundary(db), classification=labels)


def test_publish_reapplies_the_floors(tmp_path):
    conn = open_db(tmp_path / "floors.db")
    install(conn)
    attest_app(conn)
    cluster(conn, "tc_health", "therapy / sessions")
    month_of_visits(conn, 0, 5, [3, 9, 17], cluster_id="tc_health")
    conn.commit()
    obj = fam.build(conn, owner_id=OWNER, now_us=NOW_US).objects[0]
    boundary = EntityBoundary(conn)
    prepared = ir.prepare(obj, boundary)
    published = ir.publish(conn, owner_id=OWNER, prepared=prepared, boundary=boundary,
                           classification=ir.InterestClassification(label_revision=obj.label_revision, **ANSWER))
    assert published.classification.sensitivity == "special"
    conn.close()


def test_an_assessment_filed_under_another_label_does_not_count(db):
    """A row's key and its body must name the same label: a swapped row is not this label's assessment."""
    assess_all(db)
    obj = objects(db)[0]
    body = db.execute(f"SELECT assessment_json FROM {ir.TABLE} WHERE label_revision=?", (obj.label_revision,)).fetchone()[0]
    cluster(db, "tc_hobby", "sourdough / baking")
    db.commit()
    relabeled = objects(db)[0]
    db.execute(f"INSERT INTO {ir.TABLE} (label_revision, owner_id, cluster_id, assessment_json, assessed_at) "
               "VALUES (?,?,?,?,1)", (relabeled.label_revision, OWNER, "tc_hobby", body))
    db.commit()
    assert current(db, relabeled) is None
