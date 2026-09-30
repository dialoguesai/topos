"""OD-52 P7 / IF-5: a browsing interest joins a knowledge grant's index, and releases, only as the owner allowed.

protects: the release boundary for browsing. An interest is a member only when the node flag is on, the signed
grant names the ``interest`` kind, the ``activity_events`` table and the ``browser_visits`` source, the month is
wholly inside the grant's window, the label's assessment is current and releasable, and the grant's rules permit
it as the owner's ambient activity. Every one of those is re-decided from the current rows at release. What leaves
is the IF-5 §3 record: a label, a month, a band. No page, host, title or visit.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2 import interest_index as ii
from topos.permissions_v2 import interest_review as ir
from topos.permissions_v2 import knowledge_contract as kc
from topos.permissions_v2.entity_boundary import EntityBoundary

from tests.permissions_v2.interest_fixtures import (NOW_US, OWNER, at, attest_app, cluster, install,
                                                    month_of_visits, open_db, visit)
from tests.permissions_v2.test_interest_review import ANSWER, Transport

LABEL = "sourdough / baking / starter"
NOW = NOW_US // 1_000_000
KEY = bytes(range(32))
DAY = 86_400


def _atom(attribute, values):
    return {"kind": "atom", "attribute": attribute, "operator": "intersects", "values": values}


def _rule(rule_id, effect, predicate, *, sources=("imessage", "browser_visits"), ceiling="raw",
          processors=("owner-engine-local",)):
    return {"rule_id": rule_id, "effect": effect,
            "evidence_use": {"sources": {"kind": "only", "values": list(sources)}, "predicate": predicate,
                             "purpose": "work-assistant", "processors": {"kind": "only", "values": list(processors)},
                             "new_records": "include_if_predicate"},
            "release": {"predicate": predicate, "ceiling": ceiling,
                        "forms": [{"family": "canonical_record", "operation": "search",
                                   "view_id": "canonical.knowledge_search.v1", "tables": ["conversation_messages"]}]}}


PERMIT = {"kind": "all_of", "terms": [_atom("domain", ["hobbies", "work"]), _atom("sensitivity", ["none", "personal"])]}
DENY_PRIVATE = _atom("domain", ["health", "family", "finance", "relationships", "home"])


def policy(*, rules=None, result_types=("message", "interest"), tables=("conversation_messages", "activity_events"),
           max_age_days=90, release_event_time="day", capability="permissions-beta/p2c-v3", form_tables=None):
    """A parsed knowledge grant, then widened the way IF-5 §2 widens the shared grammar.

    The engine's ``knowledge_contract`` cannot parse ``interest`` or ``activity_events`` until the shared copy lands
    (IF-5 Q2), so the literal fields are set after parsing; every other field went through the real validator.
    """
    from tests.permissions_v2.test_knowledge_search import knowledge_policy
    raw = knowledge_policy()
    raw["source_universe"]["source_ids"] = ["imessage", "signal", "browser_visits"]
    raw["rules"] = rules if rules is not None else [_rule("permit-hobbies", "permit", PERMIT),
                                                   _rule("deny-private", "deny", DENY_PRIVATE)]
    raw["search"]["window"]["max_age_seconds"] = max_age_days * DAY
    raw["search"]["release_event_time"] = release_event_time
    parsed = kc.KnowledgePolicy.model_validate(raw)
    widened_rules = []
    for rule in parsed.rules:
        forms = [form.model_copy(update={"tables": list(form_tables if form_tables is not None
                                                        else ("conversation_messages", "activity_events"))})
                 for form in rule.release.forms]
        widened_rules.append(rule.model_copy(update={"release": rule.release.model_copy(update={"forms": forms})}))
    search = parsed.search.model_copy(update={"result_types": list(result_types), "tables": list(tables)})
    versions = parsed.versions.model_copy(update={"capability": capability})
    return parsed.model_copy(update={"rules": widened_rules, "search": search, "versions": versions})


@pytest.fixture()
def flag(monkeypatch):
    monkeypatch.setenv(ii.FLAG, "1")


@pytest.fixture()
def db(tmp_path, flag):
    conn = open_db(tmp_path / "index.db")
    install(conn)
    attest_app(conn)
    cluster(conn, "tc_hobby", LABEL)
    month_of_visits(conn, 0, 5, [3, 9, 17])
    month_of_visits(conn, 100, 16, [1, 5, 19], month=9)
    conn.commit()
    assess(conn)
    yield conn
    conn.close()


def assess(conn, answer=ANSWER):
    """Assess every label afresh with a fake model answer (a republish replaces the stored row)."""
    objects = fam.build(conn, owner_id=OWNER, now_us=NOW_US).objects
    boundary = EntityBoundary(conn)
    seen = set()
    for obj in objects:
        if obj.label_revision in seen:
            continue
        seen.add(obj.label_revision)
        prepared = ir.prepare(obj, boundary)
        labels = asyncio.run(ir.assess(prepared, transport=Transport(answer)))
        ir.publish(conn, owner_id=OWNER, prepared=prepared, classification=labels, boundary=boundary,
                   now=1_700_000_000)
    conn.commit()


def members(conn, grant=None, now=NOW, **kwargs):
    return ii.members(conn, owner_id=OWNER, policy=grant or policy(**kwargs), now=now,
                      boundary=EntityBoundary(conn))


def release(conn, sealed, grant=None, now=NOW, grant_id="grant-search", **kwargs):
    return ii.release(conn, sealed, key=KEY, grant_id=grant_id, owner_id=OWNER, policy=grant or policy(**kwargs),
                      now=now, boundary=EntityBoundary(conn))


def by_month(entries):
    return {entry["interest"]["month"]: entry for entry in entries}


# --- who is admitted ---------------------------------------------------------------------------

def test_a_qualifying_month_is_a_member(db):
    entries = by_month(members(db))
    assert set(entries) == {"2026-08", "2026-09"}
    entry = entries["2026-09"]
    assert {k: entry[k] for k in ("table", "source_id", "dataset_id", "record_id", "rank_text")} == {
        "table": "activity_events", "source_id": "browser_visits", "dataset_id": None,
        "record_id": "interest:tc_hobby:2026-09", "rank_text": LABEL}
    assert entry["rank_event_us"] == fam.month_span("2026-09")[0]
    assert entry["interest"]["allow_clause_id"] == "permit-hobbies"
    for forbidden in ("example.test", "Synthetic page", "https://", "browser:v"):
        assert forbidden not in json.dumps(entries)


def test_the_flag_off_admits_nothing(db, monkeypatch):
    monkeypatch.delenv(ii.FLAG)
    assert members(db) == []
    monkeypatch.setenv(ii.FLAG, "0")
    assert members(db) == []


@pytest.mark.parametrize("kwargs", [
    {"result_types": ("message", "fact")},
    {"tables": ("conversation_messages",)},
    {"capability": "permissions-beta/p2c-v1"},
    {"form_tables": ("conversation_messages",)},
    {"rules": [_rule("permit-hobbies", "permit", PERMIT, sources=("imessage",))]},
])
def test_a_grant_that_does_not_name_interests_admits_none(db, kwargs):
    assert not ii.admits(policy(**kwargs))
    assert members(db, **kwargs) == []


def test_admits_needs_every_part():
    assert ii.admits(policy())
    assert not ii.admits(object())


def test_only_months_wholly_inside_the_window(db):
    assert set(by_month(members(db, max_age_days=30))) == {"2026-09"}
    assert set(by_month(members(db, max_age_days=60))) == {"2026-08", "2026-09"}
    assert members(db, max_age_days=10) == []  # September began 19.5 days ago


@pytest.mark.parametrize("answer", [
    {"domains": ["hobbies"], "sensitivity": "special", "protected_content": "none"},
    {"domains": ["hobbies"], "sensitivity": "unknown", "protected_content": "none"},
    {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "unknown"},
])
def test_an_unreleasable_assessment_admits_nothing(db, answer):
    assess(db, answer)
    assert members(db) == []


def test_no_assessment_admits_nothing(db):
    db.execute(f"DELETE FROM {ir.TABLE}")
    db.commit()
    assert members(db) == []


@pytest.mark.parametrize("answer,verdict", [
    ({"domains": ["hobbies"], "sensitivity": "none", "protected_content": "none"}, "permit"),
    ({"domains": ["hobbies", "work"], "sensitivity": "personal", "protected_content": "none"}, "permit"),
    ({"domains": ["hobbies", "plans"], "sensitivity": "none", "protected_content": "none"}, "deny"),
    ({"domains": ["hobbies", "home"], "sensitivity": "none", "protected_content": "none"}, "deny"),
])
def test_the_grants_rules_decide_every_domain(answer, verdict):
    labels = ir.InterestClassification(label_revision="a" * 64, **answer)
    assert ii.decide(policy(), labels)[0] == verdict


def test_an_interest_is_ambient_never_authored():
    labels = ir.InterestClassification(label_revision="a" * 64, **ANSWER)
    authored = {"kind": "all_of", "terms": [PERMIT, _atom("actor_role", ["authored"])]}
    assert ii.decide(policy(rules=[_rule("p", "permit", authored)]), labels)[0] == "deny"
    ambient = {"kind": "all_of", "terms": [PERMIT, _atom("actor_role", ["ambient"])]}
    assert ii.decide(policy(rules=[_rule("p", "permit", ambient)]), labels) == ("permit", "p")


def test_rules_that_do_not_apply_are_skipped():
    labels = ir.InterestClassification(label_revision="a" * 64, **ANSWER)
    # The grammar itself refuses a hosted processor or a non-raw ceiling; a rule over other sources is skipped.
    for bad in ({"processors": ("hosted",)}, {"ceiling": "summary"}):
        with pytest.raises(ValueError):
            policy(rules=[_rule("p", "permit", PERMIT, **bad)])
    assert ii.decide(policy(rules=[_rule("p", "permit", PERMIT, sources=("imessage",))]), labels) == ("deny", None)
    deny_elsewhere = _rule("d", "deny", _atom("domain", ["hobbies"]), sources=("imessage",))
    assert ii.decide(policy(rules=[_rule("p", "permit", PERMIT), deny_elsewhere]), labels) == ("permit", "p")


def test_an_undecidable_rule_is_never_a_permit(monkeypatch):
    labels = ir.InterestClassification(label_revision="a" * 64, **ANSWER)
    grant = policy(rules=[_rule("p", "permit", PERMIT), _rule("d", "deny", _atom("subject", ["other"]))])
    assert ii.decide(grant, labels) == ("permit", "p")
    # An attribute the labels cannot supply is Unknown: a deny that might apply is indeterminate, never a permit.
    original = ii.attributes
    monkeypatch.setattr(ii, "attributes", lambda c: {**original(c), "subject": None})
    assert ii.decide(grant, labels) == ("indeterminate", None)
    only_unknown = policy(rules=[_rule("p", "permit", _atom("subject", ["owner"]))])
    assert ii.decide(only_unknown, labels) == ("indeterminate", None)


# --- release -----------------------------------------------------------------------------------

def test_the_released_record_is_the_if5_shape(db):
    entry = by_month(members(db))["2026-09"]
    record = release(db, entry)
    opaque = record["record_id"]
    assert opaque.startswith("r.") and len(opaque) == 66
    assert record == {"kind": "interest", "record_id": opaque, "content": LABEL, "label": LABEL, "month": "2026-09",
                      "strength": "medium", "source_ids": ["browser_visits"],
                      "citations": [{"record_id": opaque, "source_id": "browser_visits",
                                     "content": f"{LABEL}, 2026-09"}],
                      "event_at": fam.month_span("2026-09")[0] // 1_000_000}
    assert release(db, entry, grant_id="grant-other")["record_id"] != opaque
    from topos.permissions_v2.opaque_ids import opaque_record_id
    assert opaque == opaque_record_id(KEY, grant_id="grant-search", table="activity_events",
                                      source_id="browser_visits", dataset_id=None,
                                      record_id="interest:tc_hobby:2026-09")


@pytest.mark.parametrize("precision,expected", [("none", None), ("day", "month_start"), ("second", None)])
def test_released_time_is_the_months_first_day_at_day_precision_only(db, precision, expected):
    entry = by_month(members(db))["2026-08"]  # a whole month: it releases under every precision
    record = release(db, entry, release_event_time=precision)
    assert record["event_at"] == (fam.month_span("2026-08")[0] // 1_000_000 if expected else None)


@pytest.mark.parametrize("precision", ["day", "second"])
def test_the_open_month_releases_when_the_grant_releases_days(db, precision):
    entry = by_month(members(db, release_event_time=precision))["2026-09"]
    assert release(db, entry, release_event_time=precision) is not None


def test_a_grant_that_releases_no_time_sees_whole_months_only(db):
    """WS0's I1 ruling: without day-level time the day a month crosses a band would be new information."""
    assert set(by_month(members(db, release_event_time="none"))) == {"2026-08"}
    assert members(db, release_event_time="none", max_age_days=30) == []
    september = by_month(members(db))["2026-09"]
    assert release(db, september, release_event_time="none") is None
    assert release(db, by_month(members(db))["2026-08"], release_event_time="none") is not None


def test_the_record_validates_against_the_shared_grammar_when_it_exists(db):
    record = release(db, by_month(members(db))["2026-09"])
    shared = getattr(kc, "InterestResult", None)
    if shared is None:
        pytest.skip("knowledge_contract.InterestResult has not landed on this engine (IF-5 Q2)")
    assert shared.model_validate(record).model_dump() == record
    assert shared.model_json_schema()["properties"] == ii.InterestRecord.model_json_schema()["properties"]


def test_the_local_record_refuses_what_the_contract_refuses(db):
    record = release(db, by_month(members(db))["2026-09"])
    for bad in ({"month": "2026-9"}, {"strength": "frequent"}, {"label": ""}, {"url": "https://x.test"},
                {"source_ids": ["other"]}):
        with pytest.raises(ValueError):
            ii.InterestRecord.model_validate({**record, **bad})


def test_every_check_is_made_again_at_release(db):
    entry = by_month(members(db))["2026-09"]
    assert release(db, entry) is not None
    assert ii.member_current(db, entry, owner_id=OWNER, policy=policy(), now=NOW, boundary=EntityBoundary(db))


def _changed(db, change):
    entry = by_month(members(db))["2026-09"]
    change(db)
    db.commit()
    return release(db, entry)


def test_a_new_visit_changes_the_object_and_the_member_goes(db):
    assert _changed(db, lambda c: visit(c, 400, at(9, 20, hour=2))) is None


def test_a_relabel_goes(db):
    assert _changed(db, lambda c: cluster(c, "tc_hobby", "sourdough / baking")) is None


def test_a_reassessment_goes_even_with_the_same_answer(db):
    entry = by_month(members(db))["2026-09"]
    assert release(db, entry) is not None
    assess(db, {"domains": ["hobbies", "work"], "sensitivity": "none", "protected_content": "none"})
    assert release(db, entry) is None


def test_an_opt_out_goes(db):
    entry = by_month(members(db))["2026-09"]
    assert ii.release(db, entry, key=KEY, grant_id="g", owner_id=OWNER, policy=policy(), now=NOW,
                      boundary=EntityBoundary(db), opt_outs=frozenset({fam.opt_out_key("tc_hobby")})) is None


def test_an_off_limits_entry_goes(db):
    assert _changed(db, lambda c: c.execute(
        "INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, rebuild_state) "
        "VALUES ('bh-1','','Sourdough','sourdough','complete')")) is None


def test_a_revoked_receipt_goes(db):
    receipt = cr.receipts(db, owner_id=OWNER)[0]["receipt_id"]
    assert _changed(db, lambda c: cr.revoke(c, owner_id=OWNER, receipt_id=receipt, now=1_700_000_500)) is None


def test_the_window_is_checked_at_the_request_time(db):
    entry = by_month(members(db, max_age_days=30))["2026-09"]
    assert release(db, entry, max_age_days=30) is not None
    # 13 days later September began 32.5 days ago; the index built earlier cannot release it.
    assert release(db, entry, max_age_days=30, now=NOW + 13 * DAY) is None


def test_the_flag_turned_off_goes(db, monkeypatch):
    entry = by_month(members(db))["2026-09"]
    monkeypatch.delenv(ii.FLAG)
    assert release(db, entry) is None


def test_a_narrower_grant_goes(db):
    entry = by_month(members(db))["2026-09"]
    assert release(db, entry, rules=[_rule("permit-work", "permit", _atom("domain", ["work"]))]) is None
    assert release(db, entry, result_types=("message",)) is None


@pytest.mark.parametrize("tamper", [
    {"table": "conversation_messages"}, {"source_id": "browser_events"}, {"dataset_id": "d"},
    {"record_id": "interest:tc_hobby:2026-08"}, {"interest": None},
    {"interest": {"cluster_id": "tc_other", "month": "2026-09"}},
])
def test_a_tampered_or_foreign_member_releases_nothing(db, tamper):
    entry = by_month(members(db))["2026-09"]
    forged = {**entry, **tamper}
    if isinstance(tamper.get("interest"), dict):
        forged["interest"] = {**entry["interest"], **tamper["interest"]}
    assert release(db, forged) is None


def test_a_forged_binding_releases_nothing(db):
    entry = by_month(members(db))["2026-09"]
    for field in ("content_revision", "assessment_revision", "allow_clause_id"):
        forged = {**entry, "interest": {**entry["interest"], field: "x"}}
        assert release(db, forged) is None


def test_a_deny_that_applies_wins_over_a_permit_that_holds(db):
    labels = ir.InterestClassification(label_revision="a" * 64, domains=["hobbies"], sensitivity="personal",
                                       protected_content="none")
    grant = policy(rules=[_rule("permit-hobbies", "permit", PERMIT),
                          _rule("deny-personal", "deny", _atom("sensitivity", ["personal", "special"]))])
    assert ii.decide(grant, labels) == ("deny", None)
    assert ii.decide(policy(rules=[_rule("permit-hobbies", "permit", PERMIT)]), labels) == ("permit", "permit-hobbies")
    assess(db, {"domains": ["hobbies"], "sensitivity": "personal", "protected_content": "none"})
    assert members(db, grant) == []


def test_a_member_whose_record_id_and_month_disagree_releases_nothing(db):
    """Every other field of August's member, with September's month in the binding: not a consistent member."""
    august = by_month(members(db))["2026-08"]
    inconsistent = {**august, "interest": {**august["interest"], "month": "2026-09"}}
    assert release(db, august) is not None
    assert release(db, inconsistent) is None
