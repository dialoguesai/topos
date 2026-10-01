"""IF-5 Q&A I7: browsing interests through the knowledge search door, end to end under a signed grant.

protects: the only way browsing leaves the node. A knowledge grant that signs the `interest` kind, lists
`activity_events` and permits the `browser_visits` source receives monthly interest records, and nothing else
of a visit: the IF-5 §3 record (label, month, strength band; content = label; one self-citation
"<label>, <month>"; the record id minted from `interest:<cluster>:<month>`). Each of these withholds, at the
build and again at release:
  - fewer than 5 counted visits on 3 days; NSFW and private-window visits never count; visits the owner's
    attested plugin did not write never count;
  - an Off-limits name in any visit of the month, a label naming a person, a special-category label;
  - the node flag off; a grant that does not sign `interest` or does not permit the source;
  - the current month's elapsed part under a grant that releases no time (WS0's I1 ruling).
A label that is a bad name (it names a site, echoes a page title) withholds the month too, until a second label
passes every one of those label checks (interest_relabel); the cluster's own label never leaves.
One `max_k` holds across families. The index stays current while visits keep arriving (a later visit only
withholds its own month until the next build), and goes stale when what it holds moves: a relabel, a row the
build read, the label rubric, the owner's opt-out.

All browsing here is synthetic: invented labels, hosts, titles and names.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.interest_fixtures import NOW_US, at, cluster, install, month_of_visits, open_db, visit
from tests.permissions_v2.message_search_harness import Node, owner
from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2 import interest_index as ii
from topos.permissions_v2 import interest_review as ir
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.evidence import EvidenceResolver, EvidenceReviewStore
from topos.permissions_v2.opaque_ids import opaque_record_id
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.permissions_v2.search_index import index_path, root_for

OWNER = mc.OWNER_ID                 # the harness's owner, and the owner every policy here is bound to
DATASET = f"{OWNER}:topos:default"
APP = "browser-history-plugin"
SOURCE = "browser_visits"
LABEL = "sourdough / baking / starter"
NOW = NOW_US // 1_000_000           # 2026-09-20T12:00:00Z: August is whole, September is the open month
DAY = 86_400
ANSWER = {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "none"}
# The recipient grant's shape of rule: every domain, sensitivity none or personal.
DOMAINS = ["family", "finance", "health", "hobbies", "home", "plans", "relationships", "work"]
FORBIDDEN = ("example.test", "Synthetic page", "https://", "browser:v", "tc_hobby", "interest:")


@contextmanager
def _db(path):
    conn = sqlite3.connect(str(path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _visits(conn, start, count, days, *, month=8, **kwargs):
    return month_of_visits(conn, start, count, days, month=month, **{"dataset": DATASET, **kwargs})


def _attest(conn):
    preview = cr.preview(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, app_id=APP)
    return cr.attest(conn, owner_id=OWNER, table="activity_events", source_id=SOURCE, app_id=APP,
                     preview_digest=preview["preview_digest"], confirm=True, now=1_700_000_000)


@pytest.fixture()
def flag(monkeypatch):
    monkeypatch.setenv(ii.FLAG, "true")


@pytest.fixture()
def canonical(tmp_path, flag):
    """A migrated node database, the browser source installed in the owner's dataset, the plugin not yet attested."""
    path = tmp_path / "canonical.db"
    conn = open_db(path)
    conn.execute("UPDATE engine_config SET value=? WHERE key='user_id'", (OWNER,))
    install(conn, user=OWNER, dataset=DATASET)
    conn.commit()
    conn.close()
    ensure_protection_clock(path, owner_id=OWNER)
    return path


@pytest.fixture()
def browsing(canonical):
    """The plugin attested; one cluster with 5 visits on 3 days in August and 16 on 3 days in September."""
    with _db(canonical) as conn:
        _attest(conn)
        cluster(conn, "tc_hobby", LABEL)
        _visits(conn, 0, 5, [3, 9, 17])
        _visits(conn, 100, 16, [1, 5, 19], month=9)
    _assess(canonical)
    return canonical


def _assess(path, answer=ANSWER, now_us=NOW_US):
    """The owner's node assesses every label the deterministic checks let through (a fixed model answer)."""
    with _db(path) as conn:
        boundary = EntityBoundary(conn)
        seen = set()
        for obj in fam.build(conn, owner_id=OWNER, now_us=now_us, boundary=boundary).objects:
            if obj.label_revision in seen:
                continue
            seen.add(obj.label_revision)
            prepared = ir.prepare(obj, boundary)
            ir.publish(conn, owner_id=OWNER, prepared=prepared, boundary=boundary, now=1_700_000_000,
                       classification=ir.InterestClassification(label_revision=obj.label_revision, **answer))


def _second_label(path, answer, now_us=NOW_US):
    """The node's second try at each bad label, with a fixed model answer: `interest_relabel.publish` judges it."""
    from topos.permissions_v2 import interest_relabel as rl
    with _db(path) as conn:
        boundary = EntityBoundary(conn)
        built = fam.build(conn, owner_id=OWNER, now_us=now_us, boundary=boundary)
        for prepared in rl.pending(conn, owner_id=OWNER, built=built):
            rl.publish(conn, owner_id=OWNER, prepared=prepared, answer=answer, now_us=now_us, boundary=boundary,
                       now=1_700_000_000)


def _policy(*, kinds=("message", "fact", "goal", "relationship", "interest"), precision="day", max_k=10,
            sources=(SOURCE,), tables=("conversation_messages", "activity_events"), max_age_days=90,
            max_permitted=None):
    from tests.permissions_v2.test_knowledge_search import knowledge_policy
    raw = knowledge_policy(max_k)
    permit = copy.deepcopy(next(rule for rule in raw["rules"] if rule["effect"] == "permit"))
    predicate = {"kind": "all_of", "terms": [mc._atom("domain", DOMAINS), mc._atom("sensitivity", ["none", "personal"])]}
    permit["evidence_use"]["sources"] = {"kind": "only", "values": list(sources)}
    permit["evidence_use"]["predicate"] = copy.deepcopy(predicate)
    permit["release"]["predicate"] = copy.deepcopy(predicate)
    for form in permit["release"]["forms"]:
        form["tables"] = list(tables)
    raw["rules"] = [permit]
    # The universe names the browser source either way; whether a permit rule lists it is the policy's choice.
    raw["source_universe"]["source_ids"] = list(dict.fromkeys([*raw["source_universe"]["source_ids"], *sources, SOURCE]))
    raw["search"].update(tables=list(tables), result_types=list(kinds), release_event_time=precision)
    raw["search"]["window"]["max_age_seconds"] = max_age_days * DAY
    if max_permitted is not None:
        raw["search"]["max_permitted_records"] = max_permitted
    return raw


def _node(path, tmp_path, monkeypatch, *, now=NOW, model=None, **policy):
    monkeypatch.setattr(mc, "NOW", now)       # the policies' validity is read from it
    resolver = EvidenceResolver(path, binding=mc.BINDING)
    with owner():
        reviews = EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)
    return Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path / "search-node",
                model=model, search_raw=_policy(**policy), now=now)


def _rebuild(node):
    with owner():
        return node.index.rebuild("grant-search", now=node.now[0])


def _interests(output):
    return {record["month"]: record for record in output["records"] if record["kind"] == "interest"}


def _built(path, tmp_path, monkeypatch, **policy):
    node = _node(path, tmp_path, monkeypatch, **policy)
    return node, _rebuild(node)


def _authority(node):
    with node.ledger._transaction() as db:
        return node.ledger._authority(db, "grant-search", node.now[0])[0]


# --- the record ---------------------------------------------------------------------------------------

def test_a_qualifying_month_releases_exactly_the_if5_record(browsing, tmp_path, monkeypatch):
    node, state = _built(browsing, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 2}
    output, refused = node.search_request("sourdough baking", k=10)
    assert refused is None
    records = _interests(output)
    assert set(records) == {"2026-08", "2026-09"} and len(output["records"]) == 2
    key = node.index.keys.get("grant-search", create=False)
    opaque = opaque_record_id(key, grant_id="grant-search", table="activity_events", source_id=SOURCE,
                              dataset_id=None, record_id="interest:tc_hobby:2026-09")
    assert records["2026-09"] == {
        "kind": "interest", "record_id": opaque, "content": LABEL, "label": LABEL, "month": "2026-09",
        "strength": "medium", "source_ids": [SOURCE],
        "citations": [{"record_id": opaque, "source_id": SOURCE, "content": f"{LABEL}, 2026-09"}],
        "event_at": fam.month_span("2026-09")[0] // 1_000_000}
    assert records["2026-08"]["strength"] == "low"
    text = json.dumps(output)
    assert not any(forbidden in text for forbidden in FORBIDDEN)


def test_the_receipt_records_the_release_under_the_permit_rule(browsing, tmp_path, monkeypatch):
    """The ledger's set checkpoint checks every member binding against its record (kind signed, source in the rule,
    `activity_events` in the search and the rule's forms, projection digest) before it writes this receipt."""
    from topos.permissions_v2.canonical import digest
    node, _state = _built(browsing, tmp_path, monkeypatch)
    output, refused = node.search_request("sourdough", k=10, request_id="interest-1")
    assert refused is None and len(output["records"]) == 2
    with sqlite3.connect(node.ledger.path) as conn:
        receipt, decision = (json.loads(value) for value in conn.execute(
            "SELECT receipt_json, decision_json FROM p2a_receipts WHERE request_id='interest-1'").fetchone())
    assert (receipt["version"], receipt["verdict"], receipt["record_count"]) == ("topos-local-receipt/v3", "permit", 2)
    assert receipt["output_hash"] == digest(output)
    assert decision["matched_allow_clause_ids"] == [node.search_raw["rules"][0]["rule_id"]]


# --- every guard withholds ------------------------------------------------------------------------------

def _august(conn, *, case):
    """Five visits on three days in August, altered by one guard's case."""
    if case == "below_threshold":
        _visits(conn, 0, 4, [3, 9, 17])
        return
    _visits(conn, 0, 5, [3, 9, 17])
    if case == "nsfw":
        if "content_nsfw" not in {row[1] for row in conn.execute("PRAGMA table_info(activity_events)")}:
            conn.execute("ALTER TABLE activity_events ADD COLUMN content_nsfw INTEGER")
        conn.execute("UPDATE activity_events SET content_nsfw=1 WHERE event_id='browser:v4'")
    elif case == "incognito":
        conn.execute("DELETE FROM activity_events WHERE event_id='browser:v4'")
        conn.execute("DELETE FROM topic_cluster_members WHERE record_id='browser:v4'")
        visit(conn, 4, at(8, 17), dataset=DATASET, incognito=1)
    elif case == "relay_write":
        conn.execute("DELETE FROM activity_events WHERE event_id='browser:v4'")
        conn.execute("DELETE FROM topic_cluster_members WHERE record_id='browser:v4'")
        visit(conn, 4, at(8, 17), dataset=DATASET, writer="cp_relay", app=None)
    elif case == "offlimits_title":
        visit(conn, 20, at(8, 21), dataset=DATASET, title="An evening with Pemberly Hollis")
        conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
                     "rebuild_state) VALUES ('bh-1','','Pemberly Hollis','pemberly hollis','complete')")
    elif case == "offlimits_name_part_label":
        conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
                     "rebuild_state) VALUES ('bh-1','','Pemberly Hollis','pemberly hollis','complete')")
        cluster(conn, "tc_hobby", "hollis / woodworking")
    elif case == "person_label":
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json) "
                     "VALUES ('p-1','person','Orla Quennell','orla quennell','[]')")
        cluster(conn, "tc_hobby", "orla quennell interviews")
    elif case == "special_label":
        cluster(conn, "tc_hobby", "anxiety / sleep / routines")


# The stage that withholds each case, read from the family's own build: (visit stage, count) or the label check.
WITHHELD_BY = {"below_threshold": ("all", 4), "nsfw": ("nsfw", 4), "incognito": ("incognito", 4),
               "relay_write": ("provenance", 4), "unattested_plugin": ("provenance", 0),
               "offlimits_title": "offlimits", "offlimits_name_part_label": "offlimits",
               "person_label": "label_person", "special_label": "special"}


def _withheld_by(path):
    with _db(path) as conn:
        built = fam.build(conn, owner_id=OWNER, now_us=NOW_US)
        (august,) = [c for c in built.candidates if c.month == "2026-08"]
        if built.objects:                         # through every deterministic check: the assessment decides
            (obj,) = built.objects
            revision, _terms = ir.context(EntityBoundary(conn))
            return ir.current(conn, owner_id=OWNER, obj=obj, context_revision=revision).classification.sensitivity
        if august.label_withheld:
            return august.label_withheld
        stage = next(stage for stage in ("all", *fam.VISIT_CHECKS) if not august.qualifies(stage))
        return stage, august.visits[stage]


@pytest.mark.parametrize("case", list(WITHHELD_BY))
def test_every_guard_withholds_the_month(canonical, tmp_path, monkeypatch, case):
    with _db(canonical) as conn:
        if case != "unattested_plugin":
            _attest(conn)
        cluster(conn, "tc_hobby", LABEL)
        _august(conn, case=case)
    _assess(canonical)
    assert _withheld_by(canonical) == WITHHELD_BY[case]
    node, state = _built(canonical, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 0}
    for query in ("sourdough baking", "orla quennell interviews", "anxiety sleep routines", "hollis woodworking"):
        output, refused = node.search_request(query, k=10)
        assert refused is None and _interests(output) == {}


def test_the_control_for_the_guards_releases(canonical, tmp_path, monkeypatch):
    with _db(canonical) as conn:
        _attest(conn)
        cluster(conn, "tc_hobby", LABEL)
        _august(conn, case="clean")
    _assess(canonical)
    node, state = _built(canonical, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 1}
    output, _refused = node.search_request("sourdough baking", k=10)
    assert set(_interests(output)) == {"2026-08"}


@pytest.mark.parametrize("answer", [
    {"domains": ["hobbies"], "sensitivity": "special", "protected_content": "none"},
    {"domains": ["hobbies"], "sensitivity": "unknown", "protected_content": "none"},
    {"domains": ["hobbies"], "sensitivity": "unknown", "protected_content": "unknown"},
    {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "present"},
])
def test_an_unreleasable_label_assessment_withholds(browsing, tmp_path, monkeypatch, answer):
    _assess(browsing, answer)
    node, state = _built(browsing, tmp_path, monkeypatch)
    assert state["member_count"] == 0
    output, _refused = node.search_request("sourdough baking", k=10)
    assert _interests(output) == {}


def test_the_models_uncertainty_about_protected_content_no_longer_withholds(browsing, tmp_path, monkeypatch):
    """Floors v2 (owner direction, 1 Oct 2026): an interest is included unless something explicit excludes it.
    The model's `unknown` for protected content is not such a thing; both months reach the recipient."""
    _assess(browsing, {"domains": ["hobbies"], "sensitivity": "none", "protected_content": "unknown"})
    node, state = _built(browsing, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 2}
    output, refused = node.search_request("sourdough baking", k=10)
    assert refused is None and set(_interests(output)) == {"2026-08", "2026-09"}


# --- a bad label: a second try, held to the same checks ----------------------------------------------------

SITE_LABEL, SITE_HOST, SECOND = "velocipedia / bikes", "velocipedia.example", "cycling gear reviews"


@pytest.fixture()
def site(canonical):
    """One cluster whose own label names the site its five August pages are on."""
    with _db(canonical) as conn:
        _attest(conn)
        cluster(conn, "tc_hobby", SITE_LABEL)
        _visits(conn, 0, 5, [3, 9, 17], host=SITE_HOST)
    return canonical


def test_a_bad_label_withholds_the_month_until_a_second_label_passes_every_check(site, tmp_path, monkeypatch):
    _assess(site)
    assert _withheld_by(site) == "label_host"
    node, state = _built(site, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 0}
    _second_label(site, "velocipedia reviews")                 # refused: it names the site again
    _assess(site)
    assert _withheld_by(site) == "label_host" and _rebuild(node)["member_count"] == 0
    _second_label(site, SECOND)                                # the one try left: it passes
    assert _rebuild(node)["member_count"] == 0                 # ...and is not a member until it is assessed
    _assess(site)
    assert _rebuild(node) == {"state": "ready", "member_count": 1}
    output, refused = node.search_request("cycling gear", k=10)
    assert refused is None
    (record,) = output["records"]
    assert (record["kind"], record["label"], record["content"], record["month"], record["strength"]) == \
        ("interest", SECOND, SECOND, "2026-08", "low")
    assert record["citations"][0]["content"] == f"{SECOND}, 2026-08"
    text = json.dumps(output)
    assert "velocipedia" not in text and not any(forbidden in text for forbidden in FORBIDDEN)


@pytest.mark.parametrize("answer, exclusion", [
    ("pemberly hollis cycling", "INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, "
     "normalized_name, rebuild_state) VALUES ('bh-1','','Pemberly Hollis','pemberly hollis','complete')"),
    ("hollis cycling tours", "INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, "
     "normalized_name, rebuild_state) VALUES ('bh-1','','Pemberly Hollis','pemberly hollis','complete')"),
    ("orla quennell cycling", "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, "
     "aliases_json) VALUES ('p-1','person','Orla Quennell','orla quennell','[]')"),
])
def test_a_second_label_with_an_off_limits_name_or_a_person_never_releases(site, tmp_path, monkeypatch, answer,
                                                                           exclusion):
    with _db(site) as conn:
        conn.execute(exclusion)
    _second_label(site, answer)
    _second_label(site, answer)
    _assess(site)
    assert _withheld_by(site) == "label_host"
    node, state = _built(site, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 0}
    for query in ("cycling", answer):
        output, refused = node.search_request(query, k=10)
        assert refused is None and _interests(output) == {}


def test_a_second_label_that_stops_passing_stops_releasing_at_once(site, tmp_path, monkeypatch):
    """Release decides again from the rows: a page that arrives after the build and makes the second label echo
    a title withholds the month at the next read, and the deep sweep drops the index."""
    _second_label(site, SECOND)
    _assess(site)
    node, state = _built(site, tmp_path, monkeypatch)
    assert state["member_count"] == 1
    with _db(site) as conn:
        visit(conn, 40, at(8, 23), dataset=DATASET, host=SITE_HOST, title="Cycling gear reviews for commuters")
    output, refused = node.search_request("cycling gear", k=10)
    assert refused is None and _interests(output) == {}
    with owner():
        assert node.index.sweep(now=NOW) == 1


def test_with_the_second_label_switch_off_the_door_is_what_it_was(site, tmp_path, monkeypatch):
    """`TOPOS_PERMISSIONS_V2_INTEREST_RELABEL=off` with interests on: a stored second label stands in for
    nothing. The index is the one the node built before second labels: the same basis bytes with the switch on or
    off, no member for the bad label, and a member built while it was on stops releasing at the next read."""
    from topos.permissions_v2 import interest_relabel as rl

    def basis():
        with sqlite3.connect(index_path(root_for(site), "grant-search")) as raw:
            return raw.execute("SELECT basis_json FROM meta").fetchone()[0]

    _assess(site)
    node, state = _built(site, tmp_path, monkeypatch)
    before = (state, basis(), node.search_request("cycling gear", k=10)[0]["records"])
    assert before[0] == {"state": "ready", "member_count": 0}
    _second_label(site, SECOND)
    _assess(site)
    assert _rebuild(node)["member_count"] == 1
    on_basis = basis()
    monkeypatch.setenv(rl.FLAG, "off")
    output, refused = node.search_request("cycling gear", k=10)
    assert refused is None and output["records"] == []         # decided again at the read: withheld at once
    with owner():
        assert node.index.sweep(now=NOW) == 1                  # and the deep sweep drops the index
    after = (_rebuild(node), basis(), node.search_request("cycling gear", k=10)[0]["records"])
    assert after == before and on_basis == before[1]           # the switch is in no index basis


def test_with_the_flag_off_a_stored_second_label_changes_nothing(site, tmp_path, monkeypatch):
    """Flag off: the family is invisible whether or not a second label is stored. The same index basis, byte for
    byte, the same member count, the same answer."""
    def basis():
        with sqlite3.connect(index_path(root_for(site), "grant-search")) as raw:
            return raw.execute("SELECT basis_json FROM meta").fetchone()[0]

    monkeypatch.delenv(ii.FLAG)
    node, state = _built(site, tmp_path, monkeypatch)
    before = (state, basis(), node.search_request("cycling gear", k=10)[0]["records"])
    _second_label(site, SECOND)
    _assess(site)
    after = (_rebuild(node), basis(), node.search_request("cycling gear", k=10)[0]["records"])
    assert before == after and before[0] == {"state": "ready", "member_count": 0} and before[2] == []
    assert "interest" not in before[1]
    monkeypatch.setenv(ii.FLAG, "true")                        # the control: with the flag on it is a member
    assert _rebuild(node)["member_count"] == 1


# --- what the grant must say ----------------------------------------------------------------------------

def test_with_the_flag_off_the_family_is_invisible(browsing, tmp_path, monkeypatch):
    monkeypatch.delenv(ii.FLAG)
    node, state = _built(browsing, tmp_path, monkeypatch)
    assert state == {"state": "ready", "member_count": 0}
    output, _refused = node.search_request("sourdough baking", k=10)
    assert _interests(output) == {}
    # Built with the flag on, then turned off: the basis moves, the index is stale, nothing releases.
    monkeypatch.setenv(ii.FLAG, "true")
    assert _rebuild(node)["member_count"] == 2
    monkeypatch.delenv(ii.FLAG)
    output, refused = node.search_request("sourdough baking", k=10)
    assert output is None and refused == "permission_denied"
    assert not index_path(root_for(browsing), "grant-search").exists()


@pytest.mark.parametrize("policy", [
    {"kinds": ("message", "fact", "goal", "relationship")},                    # `interest` not signed
    {"kinds": ("message", "interest"), "sources": ("imessage",)},              # the source not permitted
])
def test_a_grant_that_does_not_ask_for_interests_gets_none(browsing, tmp_path, monkeypatch, policy):
    node, state = _built(browsing, tmp_path, monkeypatch, **policy)
    assert state["member_count"] == 0
    output, _refused = node.search_request("sourdough baking", k=10)
    assert _interests(output) == {}


@pytest.mark.parametrize("precision, months, event_at", [
    ("none", {"2026-08"}, False),                 # whole months only (I1)
    ("day", {"2026-08", "2026-09"}, True),        # the elapsed part of September too, dated by its first day
    ("second", {"2026-08", "2026-09"}, False),    # day-level or finer admits it; an interest is never timed finer
])
def test_the_current_month_releases_only_under_day_level_time(browsing, tmp_path, monkeypatch, precision, months,
                                                               event_at):
    node, state = _built(browsing, tmp_path, monkeypatch, precision=precision)
    assert state["member_count"] == len(months)
    output, _refused = node.search_request("sourdough baking", k=10)
    records = _interests(output)
    assert set(records) == months
    for month, record in records.items():
        assert record["event_at"] == (fam.month_span(month)[0] // 1_000_000 if event_at else None)


def test_a_query_window_ending_before_the_build_withholds_the_open_month(browsing, tmp_path, monkeypatch):
    node, _state = _built(browsing, tmp_path, monkeypatch)
    window = {"after": NOW - 89 * DAY, "before": NOW - 5 * DAY}     # all of August, part of September
    output, refused = node.search_request("sourdough baking", k=10, window=window)
    assert refused is None and set(_interests(output)) == {"2026-08"}
    # Starts after August began; ends (exclusive) one second after the build instant, which the open month's
    # sealed elapsed part ends at.
    narrow = {"after": NOW - 30 * DAY, "before": NOW + 1}
    output, _refused = node.search_request("sourdough baking", k=10, window=narrow)
    assert set(_interests(output)) == {"2026-09"}


def test_the_member_cap_counts_interests(browsing, tmp_path, monkeypatch):
    _node_, state = _built(browsing, tmp_path, monkeypatch, max_permitted=1)
    assert state == {"state": "over_cap", "member_count": 0}


# --- one k across families ------------------------------------------------------------------------------

def _with_journal(path, tmp_path, monkeypatch):
    """The same node with the journal family on and one proven, assessed journal entry; a grant over both."""
    from topos.permissions_v2 import automatic_message_review as amr
    from topos.permissions_v2.evidence_families import JOURNAL_FLAG
    monkeypatch.setenv(JOURNAL_FLAG, "true")
    with _db(path) as conn:
        install(conn, source="time_log", user=OWNER, dataset=DATASET)
        conn.execute("INSERT INTO journal_entries (entry_id, entry_at, content, source_id, writer_class, "
                     "writer_dataset_id) VALUES ('e1','2026-09-10T08:30:00',?,'time_log','owner_import',?)",
                     ("Baked a sourdough loaf with the new starter.", DATASET))
    node = _node(path, tmp_path, monkeypatch, kinds=("journal_entry", "interest"), sources=(SOURCE, "time_log"),
                 tables=("journal_entries", "activity_events"))
    resolver, reviews = node.index.resolver, node.index.reviews
    identity = resolver._identity("journal_entries", "e1", "time_log")
    with owner():
        prepared = amr.prepare(resolver, reviews, identity)
        labels = amr.parse_assessment(dict(domains=["hobbies"], sensitivity="none", speech="original_message",
                                           protected_content="none"), prepared["snapshot"].message)
        amr.publish(resolver, reviews, prepared, labels, now=1)
    return node


def test_max_k_holds_across_families(browsing, tmp_path, monkeypatch):
    """A journal entry and two interest months in one index: k is one bound over every family, no per-family merge."""
    node = _with_journal(browsing, tmp_path, monkeypatch)
    assert _rebuild(node) == {"state": "ready", "member_count": 3}
    for k in (1, 2):
        output, refused = node.search_request("sourdough starter", k=k)
        assert refused is None and len(output["records"]) == k
    output, _refused = node.search_request("sourdough starter", k=3)
    assert sorted(record["kind"] for record in output["records"]) == ["interest", "interest", "journal_entry"]
    assert node.search_request("sourdough starter", k=11) == (None, "permission_denied")   # above the signed max_k


def test_an_undecidable_interest_check_withholds_interests_and_nothing_else(browsing, tmp_path, monkeypatch):
    from topos.permissions_v2.canonical import PolicyError
    node = _with_journal(browsing, tmp_path, monkeypatch)

    def undecidable(*_args, **_kwargs):
        raise PolicyError("message_protection_too_large")

    monkeypatch.setattr(ii, "members", undecidable)
    assert _rebuild(node) == {"state": "ready", "member_count": 1}
    output, refused = node.search_request("sourdough starter", k=10)
    assert refused is None and [record["kind"] for record in output["records"]] == ["journal_entry"]


# --- the index stays current while browsing goes on, and stales when what it holds moves --------------------

def test_a_visit_after_the_build_withholds_only_its_month_until_the_next_build(browsing, tmp_path, monkeypatch):
    node, _state = _built(browsing, tmp_path, monkeypatch)
    with _db(browsing) as conn:
        visit(conn, 300, "2026-09-20T12:01:00.000Z", dataset=DATASET)      # after the build instant
    node.now[0] = NOW + 120
    output, refused = node.search_request("sourdough baking", k=10)
    assert refused is None                                     # the index is still the one built
    assert set(_interests(output)) == {"2026-08"}               # September's count moved: withheld until rebuilt
    with owner():
        assert node.index.sweep(now=node.now[0]) == 0
    assert _rebuild(node)["member_count"] == 2
    output, _refused = node.search_request("sourdough baking", k=10)
    assert set(_interests(output)) == {"2026-08", "2026-09"}


def test_a_relabelled_cluster_releases_its_new_label_after_the_rebuild(browsing, tmp_path, monkeypatch):
    node, _state = _built(browsing, tmp_path, monkeypatch)
    with _db(browsing) as conn:
        cluster(conn, "tc_hobby", "sourdough / bread")
    with owner():
        assert node.index.sweep(now=NOW) == 1                # the daemon sweep drops it as drift
    assert _rebuild(node)["member_count"] == 0               # the new label has no assessment yet
    _assess(browsing)
    assert _rebuild(node)["member_count"] == 2
    output, _refused = node.search_request("sourdough bread", k=10)
    assert {record["label"] for record in _interests(output).values()} == {"sourdough / bread"}


def test_the_basis_carries_the_interest_rubric_and_moves_with_it(browsing, tmp_path, monkeypatch):
    from topos.permissions_v2.search_index import _family_rubric_basis
    node, _state = _built(browsing, tmp_path, monkeypatch)
    with sqlite3.connect(index_path(root_for(browsing), "grant-search")) as raw:
        basis = json.loads(raw.execute("SELECT basis_json FROM meta").fetchone()[0])
    assert basis["automatic_rubric_revisions"] == {"interest": ir.rubric_revision()}
    assert _family_rubric_basis() == {"automatic_rubric_revisions": {"interest": ir.rubric_revision()}}
    monkeypatch.setattr(ir, "FLOORS_VERSION", "interest-label-floors/v3")
    output, refused = node.search_request("sourdough baking", k=10)
    assert output is None and refused == "permission_denied"


def test_the_owners_opt_out_of_a_cluster_takes_effect(browsing, tmp_path, monkeypatch):
    node, _state = _built(browsing, tmp_path, monkeypatch)
    with owner():
        node.index.reviews.opt_out(fam.opt_out_key("tc_hobby"), now=NOW)
    output, refused = node.search_request("sourdough baking", k=10)
    assert output is None and refused == "permission_denied"   # the review digest moved: the index is stale
    assert _rebuild(node)["member_count"] == 0
    output, _refused = node.search_request("sourdough baking", k=10)
    assert _interests(output) == {}


def test_an_interest_is_embedded_at_build_from_its_label(browsing, tmp_path, monkeypatch):
    node = _node(browsing, tmp_path, monkeypatch, model="fake-model")
    seen = []
    node.index.passage_embedder = lambda text, model: seen.append(text) or [1.0, 0.0, 0.5]
    assert _rebuild(node)["member_count"] == 2
    assert seen == [LABEL, LABEL]
    with sqlite3.connect(index_path(root_for(browsing), "grant-search")) as raw:
        assert raw.execute("SELECT count(DISTINCT opaque_id) FROM vectors").fetchone()[0] == 2


def test_the_census_copy_check_expects_the_interest_basis_the_node_writes(browsing, tmp_path, monkeypatch):
    """census_copy.consistency compares each index's stored basis with the one it expects; with the interest flag on
    the node writes the interest label rubric revision into a knowledge grant's basis, and the census expects it."""
    import importlib
    import sys
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    census_copy = importlib.import_module("census_copy")
    _node_, state = _built(browsing, tmp_path, monkeypatch)
    assert state["state"] == "ready"
    with sqlite3.connect(index_path(root_for(browsing), "grant-search")) as raw:
        basis = json.loads(raw.execute("SELECT basis_json FROM meta").fetchone()[0])
    extras = census_copy.knowledge_basis_extras()
    assert extras["automatic_rubric_revisions"] == {"interest": ir.rubric_revision()}
    assert {key: value for key, value in basis.items() if key.startswith("automatic_")} == extras


# --- the release step on its own ------------------------------------------------------------------------

def test_the_release_step_holds_an_interest_to_the_grant_on_its_own(browsing, tmp_path, monkeypatch):
    """Defence in depth: the build drops what the release refuses, so the end-to-end tests cannot tell them apart.
    `_interest_member` must refuse on its own: the kind, the table, the window, the open month, the binding."""
    from topos.permissions_v2.registry import parse_policy
    from topos.permissions_v2.search_index import unseal
    node, _state = _built(browsing, tmp_path, monkeypatch)
    key = node.index.keys.get("grant-search", create=False)
    loaded = node.index.load("grant-search", _authority(node))
    sealed = {}
    for member in loaded.members:
        value = unseal(key, member.opaque_id, member.sealed)
        sealed[value["interest"]["month"]] = (member.opaque_id, value)
    september_start = fam.month_span("2026-09")[0]

    def release(month="2026-09", *, raw=None, lower=september_start - 40 * DAY * 10**6, upper=NOW_US, tables=None,
                binding=None):
        policy = parse_policy(raw or _policy())
        opaque, value = sealed[month]
        if binding is not None:
            value = {**value, "interest": {**value["interest"], **binding}}
        with node.index.resolver._read() as (conn, _floor), node.index.reviews._db() as review_db:
            return node.search._interest_member(conn, review_db, key, "grant-search", opaque, value, policy, True,
                                                set(tables or policy.search.tables), {}, lower, upper)

    record, binding, _revision = release()
    assert record["kind"] == "interest" and binding["evidence_tables"] == ["activity_events"]
    assert binding["kind"] == "interest" and binding["source_ids"] == [SOURCE]
    assert release(raw=_policy(kinds=("message", "fact"))) is None              # the kind is not signed
    assert release(tables={"conversation_messages"}) is None                     # the table is not in the search
    assert release(raw=_policy(precision="none")) is None                        # the open month, no time detail
    assert release("2026-08", raw=_policy(precision="none")) is not None         # a whole month, no time detail
    assert release(upper=NOW_US - 1) is None                                     # the window ends before the build
    assert release(lower=september_start + 1) is None                            # the month began before the window
    assert release("2026-08", upper=fam.month_span("2026-08")[1] - 2) is None    # August not yet over in the window
    assert release(binding={"built_at": None}) is None                           # no build instant
    assert release(binding={"allow_clause_id": "another-rule"}) is None          # not the rule it was admitted under
    assert release(binding={"content_revision": "0" * 64}) is None               # not the object it was built from
    opaque, value = sealed["2026-09"]
    with node.index.resolver._read() as (conn, _floor), node.index.reviews._db() as review_db:
        other = node.search._interest_member(conn, review_db, key, "grant-other", opaque, value,
                                             parse_policy(_policy()), True, {"activity_events"}, {}, 0, NOW_US)
    assert other is None                                                         # minted under another grant's ids
    with owner():                                                                # the owner deselects the cluster
        node.index.reviews.opt_out(fam.opt_out_key("tc_hobby"), now=NOW)
    assert release() is None and release("2026-08") is None


def test_a_change_between_the_build_and_its_publish_is_caught(browsing, tmp_path, monkeypatch):
    """The build reads an ungated snapshot; the publish re-decides every interest member on the gated read, so a
    relabel landing in between never publishes the old label: that attempt is refused and the next one builds."""
    from topos.permissions_v2.search_index import SearchIndexService
    node = _node(browsing, tmp_path, monkeypatch)
    original, calls = SearchIndexService._unchanged, []

    def relabel_then_check(self, *args):
        if not calls:
            with _db(browsing) as conn:
                cluster(conn, "tc_hobby", "sourdough / bread")
        calls.append(1)
        return original(self, *args)

    monkeypatch.setattr(SearchIndexService, "_unchanged", relabel_then_check)
    assert _rebuild(node) == {"state": "ready", "member_count": 0}   # the new label has no assessment yet
    assert len(calls) == 2


def test_a_stored_interest_object_is_never_what_releases(browsing, tmp_path, monkeypatch):
    """WS0's I6: a stored `browsing_interest` object is node-internal. The door decides from the canonical rows at
    build and at release, so an object persisted before its visits stopped being provable releases nothing."""
    with _db(browsing) as conn:
        assert fam.persist(conn, fam.build(conn, owner_id=OWNER, now_us=NOW_US))["inserted"] == 2
        receipt = cr.receipts(conn, owner_id=OWNER)[0]["receipt_id"]
        cr.revoke(conn, owner_id=OWNER, receipt_id=receipt, now=1_700_000_500)
        stored = conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='browsing_interest' "
                              "AND valid_to IS NULL").fetchone()[0]
    assert stored == 2
    node, state = _built(browsing, tmp_path, monkeypatch)
    assert state["member_count"] == 0
    output, _refused = node.search_request("sourdough baking", k=10)
    assert _interests(output) == {}


def test_the_interests_scope_is_a_grant_scope_and_names_no_stored_object():
    """WS0's I6: `interests:read` never serves raw `browsing_interest` objects. The engine registry gives the scope no
    raw table, no signal object, no summary or inference object, and does not advertise it live, so no legacy or UMA
    read can name the stored objects; interests leave only through a p2c-v3 grant (the tests above). No other scope
    names the object type either. The control plane's bundled copy must match (its parity tests compare both)."""
    from topos.query.scope_registry_loader import get_scope_entry, list_scopes
    entry = get_scope_entry("interests:read")
    assert entry is not None
    assert {field: entry[field] for field in ("raw_tables", "signal_objects", "summary_objects", "inference_objects")} == {
        "raw_tables": [], "signal_objects": [], "summary_objects": [], "inference_objects": []}
    assert entry["implementation_status"] != "live"
    assert not [scope["scope_id"] for scope in list_scopes()
                if fam.OBJECT_TYPE in (scope.get("signal_objects") or []) + (scope.get("summary_objects") or [])]


# --- the release decides every interest at its read clock -----------------------------------------------

SEP30 = 1_790_812_740   # 2026-09-30T23:59:00Z: September is still the open month
# A change to the browsing -> the months that may still release right after it. The first six are visible at the
# build instant too (the daemon sweep drops the index for them); the last three exist only at the read's clock.
CHANGED = {"relabel": set(), "backfilled_visit": {"2026-08"}, "edited_visit": {"2026-08"}, "receipt_revoked": set(),
           "reassessed": set(), "new_person": set(),
           "visit_after_build": {"2026-08"}, "offlimits_visit_after_build": {"2026-08"}, "month_rolled_over": {"2026-08"}}


def _change(path, node, change):
    with _db(path) as conn:
        if change == "relabel":
            cluster(conn, "tc_hobby", "sourdough / bread")
        elif change == "backfilled_visit":                     # written after the build, visited before it
            visit(conn, 300, at(9, 19, hour=11), dataset=DATASET)
        elif change == "edited_visit":
            conn.execute("UPDATE activity_events SET url='https://example.test/moved' WHERE event_id='browser:v100'")
        elif change == "receipt_revoked":
            receipt = cr.receipts(conn, owner_id=OWNER)[0]["receipt_id"]
            cr.revoke(conn, owner_id=OWNER, receipt_id=receipt, now=1_700_000_500)
        elif change == "new_person":
            conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
                         "VALUES ('p-2','person','Sourdough Baking','sourdough baking')")
        elif change == "visit_after_build":                    # counted, after the build instant
            visit(conn, 300, "2026-09-20T12:01:00.000Z", dataset=DATASET)
        elif change == "offlimits_visit_after_build":          # a private-window visit: never counted, still checked
            visit(conn, 300, "2026-09-20T12:01:00.000Z", dataset=DATASET, incognito=1,
                  title="An evening with Pemberly Hollis")
    if change == "reassessed":
        _assess(path, {"domains": ["hobbies", "home"], "sensitivity": "none", "protected_content": "none"})
    node.now[0] = SEP30 + 120 if change == "month_rolled_over" else NOW + 120


def _built_for(path, tmp_path, monkeypatch, change):
    if change == "offlimits_visit_after_build":                # Off-limits already exists; the build stays clean
        with _db(path) as conn:
            conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, "
                         "rebuild_state) VALUES ('bh-1','','Pemberly Hollis','pemberly hollis','complete')")
        _assess(path)                                          # the protected vocabulary moved: assessed again
    node, state = _built(path, tmp_path, monkeypatch, now=SEP30 if change == "month_rolled_over" else NOW)
    assert state == {"state": "ready", "member_count": 2}
    output, refused = node.search_request("sourdough baking", k=10)
    assert refused is None and set(_interests(output)) == {"2026-08", "2026-09"}
    return node


@pytest.mark.parametrize("change", list(CHANGED))
def test_a_changed_interest_never_releases_even_when_the_index_does_not_see_the_change(browsing, tmp_path,
                                                                                       monkeypatch, change):
    """WS0: the index's interest currency check runs on the deep sweeps only, like lineage, so between sweeps a
    member whose browsing changed can still be in the index. What keeps it from a recipient is the release: `_accept`
    decides every interest again with `interest_index.release_object` at the read's own clock. Here the index's
    check is switched off outright, sweeps included, so the release is the only thing that can withhold: every
    changed month is withheld, every unchanged one still releases, and the search itself is answered."""
    from topos.permissions_v2.search_index import SearchIndexService
    monkeypatch.setattr(SearchIndexService, "_interests_current", lambda self, *args, **kwargs: True)
    node = _built_for(browsing, tmp_path, monkeypatch, change)
    _change(browsing, node, change)
    output, refused = node.search_request("sourdough baking", k=10)
    assert refused is None
    assert set(_interests(output)) == CHANGED[change]


@pytest.mark.parametrize("change", list(CHANGED))
def test_a_request_withholds_a_change_and_the_sweep_drops_what_the_build_saw(browsing, tmp_path, monkeypatch, caplog,
                                                                              change):
    """A recipient's request runs no interest currency check (deep=False): it is answered and the release withholds
    the changed month. The daemon's deep sweep then drops the index exactly when the change is visible at the build's
    own instant (a relabel, a reassessment, a changed, backfilled or unproven visit, a new person name), with the
    `interest` stale stage; a change that exists only after the build (a later visit, a later Off-limits visit, the
    month rolling over) keeps the index, and the release keeps withholding it until the next build."""
    import logging
    caplog.set_level(logging.WARNING, logger="topos.permissions_v2.search_index")
    node = _built_for(browsing, tmp_path, monkeypatch, change)
    _change(browsing, node, change)
    output, refused = node.search_request("sourdough baking", k=10)
    assert refused is None and set(_interests(output)) == CHANGED[change]
    assert not [r for r in caplog.records if "stale" in r.getMessage()]
    seen_at_build = change not in ("visit_after_build", "offlimits_visit_after_build", "month_rolled_over")
    with owner():
        assert node.index.sweep(now=node.now[0]) == int(seen_at_build)
    assert index_path(root_for(browsing), "grant-search").exists() is not seen_at_build
    assert [r.getMessage() for r in caplog.records if "stale" in r.getMessage()] == (
        ["message search index stale (interest)"] if seen_at_build else [])
