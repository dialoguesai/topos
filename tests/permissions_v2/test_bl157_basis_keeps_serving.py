"""BL-157: a share stays served while the node-wide review digest moves (the `basis` stale class), narrowed.

Since 1.5.2 the review digest is the last thing that deletes a share index for something the index does not hold:
every assessment anywhere on the node moved it, and the sweep dropped the index until a rebuild. An index built with
the review guard (`index_review_guard`) seals, inside each member, the exact reviews that member can consume (its own
machine assessment and owner correction, absence included, and those of its context and dependencies), and the
opt-out set. When the digest moves:

- kept serving: no member's binding moved and the opt-out set is the same. Add-only proof: what the index serves is
  byte-identical, nothing new is served before the refresh build publishes it, and the build is owed;
- dropped at once: a review of a member or of its context, an owner correction, any opt-out change, and everything
  1.5.2 already dropped for (protection, content, owner-only) - exactly as before.

Invented evidence only; production-schema journal fixture; no model, network or personal data.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, owner
from tests.permissions_v2.test_journal_family import (
    node as journal_database, _entry, _journal_policy, _labels, _publish, AFTER_ITS_DAY,
)
from tests.permissions_v2.test_refresh_loop import Clock, loop_for, receipts
from topos.permissions_v2.automatic_message_review import prepare, publish
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.search_index import index_path

pytestmark = pytest.mark.public
TEXT = "I am working on a synthetic task at work."
NEW = "I completed a second synthetic draft at work."
OTHER = "Walked the long way home along the river."
QUERY = "synthetic task draft work"


@pytest.fixture
def sharing(journal_database, tmp_path, monkeypatch):
    _entry(journal_database, "e1", TEXT)
    _entry(journal_database, "e2", NEW)
    _entry(journal_database, "e3", OTHER)
    resolver, reviews = _publish(journal_database, "e1", domains=["work"])
    monkeypatch.setattr(mc, "NOW", AFTER_ITS_DAY)
    raw = _journal_policy(kinds=("journal_entry",))
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path / "search",
                model=None, search_raw=raw, now=AFTER_ITS_DAY)
    assert node.rebuild() == {"grant-search": "ready"}
    return node


def assess(node, entry_id="e2", **labels):
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    identity = resolver._identity("journal_entries", entry_id, "time_log")
    with owner():
        prepared = prepare(resolver, reviews, identity)
        return publish(resolver, reviews, prepared, _labels(prepared, **{"domains": ["work"], **labels}),
                       now=node.now[0])


def path(node):
    return index_path(node.index.root, "grant-search")


def served(node):
    """What a search of the share releases, as bytes (the walk's output)."""
    output, refused = node.search_request(QUERY, k=10)
    assert refused is None, refused
    return canonical_bytes(output)


def contents(node):
    output, refused = node.search_request(QUERY, k=10)
    assert refused is None, refused
    return {record["content"] for record in output["records"]}


def sweep(node):
    return node.index.sweep(now=node.now[0])


# --- kept serving: the add-only proof for each case ----------------------------------------------------------------

@pytest.mark.parametrize("review", ["new_shareable_item", "item_never_shareable"])
def test_a_review_outside_the_index_keeps_it_serving_byte_for_byte(sharing, review):
    node = sharing
    before_file, before_served = path(node).read_bytes(), served(node)
    if review == "new_shareable_item":
        assess(node, "e2")                                         # shareable: the next build will add it
    else:
        assess(node, "e3", domains=["hobbies"], sensitivity="special")   # never shareable under this rule
    assert sweep(node) == 0                                        # 1.5.2: 1 (dropped as `stale (basis)`)
    assert path(node).read_bytes() == before_file                  # the same file, untouched
    assert served(node) == before_served                           # the walk's output, byte for byte
    assert contents(node) == {TEXT}                                # nothing new is served before the build
    assert node.index.take_refresh_needed().keys() == {path(node).name}   # and the build is owed


def test_the_owed_build_adds_the_new_item_and_names_no_content(sharing):
    node = sharing
    clock = Clock(node.now[0])
    loop = loop_for(node, clock)
    loop.observe(node.index)
    assess(node, "e2")
    sweep(node)
    loop.observe(node.index)
    receipt = loop.run_pending()
    assert receipt.cause_classes == ["review_added"] and receipt.grants[0].state == "ready"
    assert contents(node) == {TEXT, NEW}
    assert TEXT not in json.dumps(receipts(node)) and NEW not in json.dumps(receipts(node))
    sweep(node)
    assert node.index.take_refresh_needed() == {}                  # settled: nothing owed after the build


def test_a_written_answer_is_delivered_at_once_after_a_review_outside_the_index(sharing):
    """With BL-159: the fetch of a written answer reads the same served index, so it is handed out, not held."""
    from tests.permissions_v2.test_answer_release import _service
    from tests.permissions_v2.test_bl159_answer_delivery import QUESTION, ANSWER
    from tests.permissions_v2.test_answer_release import _ask, _fetch
    import time
    node = sharing
    node.search_raw["search"]["answers"] = "only"
    node.search_raw["policy_version_id"] = "synthetic-answer-policy-v2"
    node.activate(node.search_raw, generation=2)
    node.rebuild()
    service = _service(node, ANSWER)
    try:
        answer_id = _ask(node, service, QUESTION)
        for _ in range(500):
            if service._jobs[answer_id].state == "ended":
                break
            time.sleep(.01)
        assess(node, "e2")
        assert sweep(node) == 0
        body = _fetch(node, service, answer_id)
        assert body["outcome"] == "answered"
    finally:
        service.close()


# --- dropped at once -----------------------------------------------------------------------------------------------

def _protect(node):
    from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
    with sqlite3.connect(node.corpus.path) as conn:
        apply_entity_blackhole_v1_up(conn)
        conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                     "aliases_json, created_at) VALUES ('b-1','','synthetic task','Synthetic Task','[]','t')")


def _restrict(node, change):
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    identity = resolver._identity("journal_entries", "e1", "time_log")
    if change == "member_reassessed":
        assess(node, "e1", sensitivity="special")
    elif change == "member_reassessed_alike":
        assess(node, "e1")                                         # the same labels: a new review all the same
    elif change == "member_corrected":
        from topos.permissions_v2.message_evidence import record_message_review
        with owner():
            prepared = prepare(resolver, reviews, identity)
            record_message_review(resolver, reviews, review_id="owner-correction",
                expected_snapshot=prepared["snapshot"].model_dump(),
                classification=_labels(prepared, domains=["work"]).model_dump(),
                expected_current_review_revision=None, reviewed_at=node.now[0])
    elif change in ("opt_out_member", "opt_out_other"):
        from topos.permissions_v2.message_evidence import message_key
        other = resolver._identity("journal_entries", "e3" if change == "opt_out_other" else "e1", "time_log")
        with owner():
            reviews.opt_out(message_key(other), now=node.now[0])
    elif change == "off_limits":
        _protect(node)
    else:
        with sqlite3.connect(resolver.path) as db:
            if change == "content":
                db.execute("UPDATE journal_entries SET content='I am working on a changed private matter at work.' "
                           "WHERE entry_id='e1'")
            else:
                db.execute("INSERT INTO owner_only_records(canonical_table,record_id,created_at,updated_at) "
                           "VALUES('journal_entries','e1','t','t')")


@pytest.mark.parametrize("change", ["member_reassessed", "member_reassessed_alike", "member_corrected",
                                    "opt_out_member", "opt_out_other", "off_limits", "content", "owner_only"])
def test_a_change_that_can_touch_a_member_drops_the_index_at_once(sharing, change):
    node = sharing
    _restrict(node, change)
    output, refused = node.search_request(QUERY, k=10)
    assert output is None and refused is not None                  # the request itself refuses ...
    sweep(node)
    assert not path(node).exists()                                 # ... and the index is gone
    assert node.index.take_refresh_needed() == {}


@pytest.mark.parametrize("guard", [None, "unsupported/v99"])
def test_an_index_without_this_guard_keeps_the_strict_rule(sharing, guard):
    """An index built by 1.5.2 (no guard) or under an unknown guard drops on any review, as before."""
    node = sharing
    with sqlite3.connect(path(node)) as db:
        basis = json.loads(db.execute("SELECT basis_json FROM meta").fetchone()[0])
        if guard is None:
            basis.pop("review_guard_version")
            basis.pop("opt_out_revision")
        else:
            basis["review_guard_version"] = guard
        db.execute("UPDATE meta SET basis_json=?", (json.dumps(basis),))
    if guard is None:
        assert contents(node) == {TEXT}                            # served as before while nothing moves
    assess(node, "e3", domains=["hobbies"], sensitivity="special")
    assert sweep(node) == 1
    assert not path(node).exists()


def test_bindings_are_sealed_and_a_member_missing_one_fails_closed(sharing):
    from topos.permissions_v2.search_index import seal, unseal
    node = sharing
    key = node.index.keys.get("grant-search", create=False)
    raw = path(node).read_bytes()
    with sqlite3.connect(path(node)) as db:
        opaque, encrypted = db.execute("SELECT opaque_id, sealed FROM members").fetchone()
        member = unseal(key, opaque, encrypted)
        assert len(member["review_bindings"]) == 2                 # machine assessment and owner correction keys
        for binding in member["review_bindings"]:
            assert binding["key"].encode() not in raw              # inside the seal, never in the clear
        member["review_bindings"].pop()
        db.execute("UPDATE members SET sealed=? WHERE opaque_id=?", (seal(key, opaque, member), opaque))
    assess(node, "e3", domains=["hobbies"], sensitivity="special")
    assert sweep(node) == 1
    assert not path(node).exists()


# --- builds under a review stream ----------------------------------------------------------------------------------

def test_a_review_outside_the_build_during_the_build_does_not_cancel_it(sharing, monkeypatch):
    node = sharing
    real = node.index._members
    calls = []

    def members(*args, **kwargs):
        built = real(*args, **kwargs)
        if not calls:
            assess(node, "e3", domains=["hobbies"], sensitivity="special")
        calls.append(1)
        return built

    monkeypatch.setattr(node.index, "_members", members)
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0]) == {"state": "ready", "member_count": 1}
    assert calls == [1]                                            # 1.5.2: built again (the digest moved)


def test_a_review_of_a_member_during_the_build_still_cancels_it(sharing, monkeypatch):
    node = sharing
    real = node.index._members
    calls = []

    def members(*args, **kwargs):
        built = real(*args, **kwargs)
        if not calls:
            assess(node, "e1", sensitivity="special")
        calls.append(1)
        return built

    monkeypatch.setattr(node.index, "_members", members)
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    assert len(calls) >= 2                                         # the first build did not publish
    output, refused = node.search_request(QUERY, k=10)
    assert refused is not None or TEXT not in {record["content"] for record in output["records"]}


@pytest.mark.parametrize("restricted", [False, True])
def test_a_refresh_that_cannot_publish_keeps_only_a_proven_index(sharing, monkeypatch, restricted):
    node = sharing
    if restricted:
        assess(node, "e1", sensitivity="special")
    monkeypatch.setattr(node.index, "_unchanged", lambda *args, **kwargs: False)
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "stale"
    assert path(node).exists() is (not restricted)
    if not restricted:
        assert contents(node) == {TEXT}


def test_a_stale_request_never_shreds_a_newer_publication(sharing, monkeypatch):
    node = sharing
    real = node.index._current
    replaced = []

    def current(*args, **kwargs):
        if not replaced:
            replaced.append(True)
            with owner():
                node.index.rebuild("grant-search", now=node.now[0])
            return False
        return real(*args, **kwargs)

    monkeypatch.setattr(node.index, "_current", current)
    with node.ledger._transaction() as db:
        authority, _policy = node.ledger._authority(db, "grant-search", node.now[0])
    with pytest.raises(PolicyError, match="search_index_stale"):
        node.index.check_own("grant-search", authority, now=node.now[0])
    assert path(node).exists()
    assert contents(node) == {TEXT}


def test_repeated_observations_do_not_postpone_and_an_exhausted_target_is_not_retried(sharing, monkeypatch):
    node = sharing
    clock = Clock(node.now[0])
    loop = loop_for(node, clock, max_attempts=2, backoff=1)
    loop.observe(node.index)
    assess(node, "e2")
    rebuild = node.index.rebuild
    monkeypatch.setattr(node.index, "rebuild", lambda *args, **kwargs: {"state": "stale", "member_count": 0})
    for _ in range(2):
        sweep(node)
        loop.observe(node.index)
        assert loop.run_pending().grants[0].state == "stale"
        clock.now += 1
    sweep(node)
    loop.observe(node.index)
    assert loop.run_pending() is None                               # the same target used its attempts
    assert contents(node) == {TEXT}                                 # and the index still serves
    monkeypatch.setattr(node.index, "rebuild", rebuild)
    assess(node, "e3", domains=["hobbies"], sensitivity="special")  # a new target earns new attempts
    sweep(node)
    loop.observe(node.index)
    assert loop.run_pending().grants[0].state == "ready"
    assert contents(node) == {TEXT, NEW}
