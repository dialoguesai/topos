"""IF-6 v1b follow-up: while the derived-facts flag is on, a journal review must carry the model's own label.

protects: v1b's guard 1 reads the protected_content the model itself gave an entry, before the journal floor
(`MachineMessageReview.model_protected_content`). A review published before v1b records none, and on the owner's node
that is every journal review: every inferred fact withheld, and nothing made the node assess an entry again. While
`TOPOS_PERMISSIONS_V2_DERIVED_FACTS` is on (with the journal family it needs), such a review is not current
(`automatic_message_review.lacks_model_label`), and the flag names the rule among the revisions the refresh loop's
catch-up compares (`assessment_revisions`), so the node assesses those entries again with no owner action. Pinned:
  - flag off: `is_current`, `assessment_revisions` and the index basis are what they were; a review without the
    label stays current, the worker keeps it, the entry releases and the owner's preview shows its labels;
  - flag on: only a journal review without the label is stale. A message review (conversation_messages,
    ai_chat_messages) without it stays current, and a review v1b's `publish` wrote is current;
  - the round trip: the old review stales, its entry and fact withhold, the worker assesses the entry again, and the
    new review is current and lets the inferred fact pass guard 1;
  - an index built before the rule keeps its basis; the release re-qualifies the stale entry and withholds it;
  - the catch-up sees the flag's rule as a rule change and runs one full pass.
Synthetic fixtures only: invented names, no owner data.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_automatic_message_review import answer, chat_setup, setup
from tests.permissions_v2.test_inferred_facts import _inferred, _review_of
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 (fixture)
from tests.permissions_v2.test_journal_family import (_identity, _labels, _prepare, _resolver,  # noqa: F401
                                                      node, owner)
from tests.permissions_v2.test_journal_typed_items import DAY, _code, _kind, _node, _search
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from tests.permissions_v2.test_refresh_loop import (Clock, FakeWorker, RealRulesLoop, daytime, finish, settled,
                                                    stored_state)
from topos.permissions_v2 import automatic_message_review as amr
from topos.permissions_v2 import inferred_facts
from topos.permissions_v2.automatic_message_review import (JOURNAL_MODEL_LABEL_VERSION, MachineMessageReview,
                                                           is_current, lacks_model_label, machine_key)
from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
from topos.permissions_v2.canonical import canonical_bytes
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.evidence_families import JOURNAL_FLAG
from topos.permissions_v2.inferred_facts import FLAG
from topos.permissions_v2.message_evidence import preview_message, qualify_automatic_message
from topos.permissions_v2.message_review_contract import AutomaticReviewRequest
from topos.permissions_v2.refresh_loop import assessment_revisions

WINDOW = AutomaticReviewRequest(after=DAY - 7 * 86400, before=DAY + 2 * 86400)   # holds the default entry's day


@pytest.fixture()
def derived(monkeypatch):
    monkeypatch.setenv(FLAG, "true")


def _without_label(review, review_id="auto-pre-v1b"):
    """`review` as a node published it before v1b: the same labels and revisions, no model label."""
    dumped = {key: value for key, value in review.model_dump().items() if key != "model_protected_content"}
    return MachineMessageReview.parse({**dumped, "review_id": review_id})


def _store(reviews, review):
    """Make `review` its record's current machine review, written as `publish` writes one."""
    key = machine_key(review.snapshot.message.identity)
    with owner(), reviews._db() as db:
        db.execute("UPDATE fact_reviews SET active=0 WHERE fact_id=? AND active=1", (key,))
        db.execute("INSERT INTO fact_reviews VALUES(?,?,?,1)",
                   (review.review_id, key, canonical_bytes(review.model_dump()).decode("ascii")))
    return review


def _store_of(path):
    resolver = _resolver(path)
    with owner():
        return resolver, EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)


def _pre_v1b(path, entry_id="e1"):
    """The entry's review replaced by what the owner's node holds: one published before v1b."""
    _resolver_, reviews = _store_of(path)
    return _store(reviews, _without_label(_review_of(path, entry_id)))


def _worker(path):
    """The automatic review worker over the node; every model answer is an ordinary label, and each call is counted."""
    resolver, reviews = _store_of(path)
    calls = []

    async def classify(prepared):
        calls.append(prepared["snapshot"].message.identity.record_id)
        return _labels(prepared, domains=["work"])
    return AutomaticReviewWorker(resolver, reviews, classifier=classify), calls


def _pass(worker):
    with owner():
        asyncio.run(worker._process(WINDOW, refresh=False))
        return worker.status()


def _origin(path, entry_id="e1"):
    resolver, reviews = _store_of(path)
    with owner():
        return preview_message(resolver, reviews, _identity(resolver, entry_id))["classification_origin"]


# --- flag off: nothing moves -----------------------------------------------------------------------------------------

def test_with_the_flag_off_a_review_without_the_label_is_current_and_nothing_moves(node, tmp_path, monkeypatch):
    from topos.permissions_v2.search_index import _family_rubric_basis
    monkeypatch.delenv(FLAG, raising=False)
    fact = _inferred(node)
    labelled = _review_of(node)
    old = _pre_v1b(node)
    prepared = _prepare(node, "e1")[2]
    assert old.model_protected_content is None and "model_protected_content" not in old.model_dump()
    assert is_current(old, prepared) and is_current(labelled, prepared) and not lacks_model_label(old)
    assert "journal_model_label" not in assessment_revisions()
    assert _family_rubric_basis() == {"automatic_rubric_revisions": {
        "journal_entry": amr.rubric_revision_for("journal_entries")}}
    worker, calls = _worker(node)
    assert (_pass(worker).current, calls) == (1, [])                  # kept: no model call
    assert _origin(node) == "automatic"
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1                                 # the entry releases as before
    assert _code(search, "signal_objects", fact) == "fact_not_grounded"


@pytest.mark.parametrize("env", [{FLAG: "false"}, {FLAG: "0"}, {FLAG: "", JOURNAL_FLAG: "true"},
                                 {FLAG: "true", JOURNAL_FLAG: "false"}])
def test_every_reading_of_the_flag_as_off_leaves_the_rule_out(node, monkeypatch, env):
    _inferred(node)
    old = _without_label(_review_of(node))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert not inferred_facts.enabled() and not lacks_model_label(old)
    assert "journal_model_label" not in assessment_revisions()


# --- flag on: only a journal review without the label is stale ---------------------------------------------------

def test_with_the_flag_on_a_journal_review_without_the_label_is_stale_and_one_publish_wrote_is_current(
        node, monkeypatch, derived):
    _inferred(node)
    labelled = _review_of(node)
    prepared = _prepare(node, "e1")[2]
    assert labelled.model_protected_content == "none" and is_current(labelled, prepared)
    for label in ("present", "unknown"):                              # a recorded label is current; guard 1 decides
        assert is_current(labelled.model_copy(update={"model_protected_content": label}), prepared)
    old = _without_label(labelled)
    assert lacks_model_label(old) and not is_current(old, prepared)
    on = assessment_revisions()
    monkeypatch.delenv(FLAG)
    assert on == {**assessment_revisions(), "journal_model_label": JOURNAL_MODEL_LABEL_VERSION}   # the one change
    assert is_current(old, prepared)                                  # and back with the flag


def test_the_owners_preview_agrees_with_the_rule(node, monkeypatch, derived):
    _inferred(node)
    assert _origin(node) == "automatic"
    _pre_v1b(node)
    assert _origin(node) == "pending"                                 # not current: no automatic labels to show
    monkeypatch.delenv(FLAG)
    assert _origin(node) == "automatic"


@pytest.mark.parametrize("table", ["conversation_messages", "ai_chat_messages"])
def test_with_the_flag_on_a_message_review_without_the_label_stays_current(legacy, monkeypatch, table):
    from topos.storage.db.migrations.signal_dimension_harness import apply_signal_dimension_harness_up
    apply_signal_dimension_harness_up(legacy[1])          # the journal table a node with the family on has
    monkeypatch.setenv(JOURNAL_FLAG, "true")
    monkeypatch.setenv(FLAG, "true")
    assert inferred_facts.enabled()
    if table == "conversation_messages":
        resolver, reviews, identity, _prepared = setup(legacy)
    else:
        resolver, reviews, identity_of = chat_setup(legacy, monkeypatch)
        identity = identity_of("u2")
    with owner():
        prepared = amr.prepare(resolver, reviews, identity)
        labelled = amr.publish(resolver, reviews, prepared, answer(prepared), now=1)
    old = _store(reviews, _without_label(labelled))
    with owner():
        prepared = amr.prepare(resolver, reviews, identity)
    assert identity.table == table and old.model_protected_content is None
    assert not lacks_model_label(old) and is_current(old, prepared)
    with resolver._read() as (conn, floor), reviews._db() as db:
        qualified, _rows = qualify_automatic_message(resolver, conn, floor, identity, reviews, db)
    assert qualified.review_id == old.review_id
    with owner():
        assert preview_message(resolver, reviews, identity)["classification_origin"] == "automatic"


# --- the round trip ----------------------------------------------------------------------------------------------

def test_an_old_review_stales_is_assessed_again_and_its_fact_then_passes_guard_one(node, tmp_path, monkeypatch,
                                                                                   derived):
    fact = _inferred(node)
    _pre_v1b(node)
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 0                                 # the entry withholds, and its fact with it
    assert _code(search, "signal_objects", fact) == "review_stale"
    worker, calls = _worker(node)
    status = _pass(worker)
    assert (status.assessed, calls) == (1, ["e1"])                    # no owner action: the pass assessed it again
    review = _review_of(node)
    assert review.model_protected_content == "none" and is_current(review, _prepare(node, "e1")[2])
    again, calls_again = _worker(node)
    assert (_pass(again).current, calls_again) == (1, [])             # current now: never assessed twice
    with owner():
        assert search.index.rebuild("grant-search", now=search.now[0])["member_count"] == 2
    assert _code(search, "signal_objects", fact) is None              # guard 1 passes on the model's own label
    records, _bindings = _search(search, monkeypatch, "Atlas parser")
    (item,) = _kind(records, "fact")
    assert item["assertion"] == "inferred" and len(_kind(records, "journal_entry")) == 1


def test_an_index_built_before_the_rule_withholds_the_stale_entry_at_release(node, tmp_path, monkeypatch, derived):
    """A node whose flag was already on keeps its index: the basis names no such rule. The release re-qualifies each
    member, so the stale entry withholds from the install until the catch-up assesses it again, and that new review
    moves the index's review revision, so the index is rebuilt then."""
    _inferred(node)
    _pre_v1b(node)
    rule = amr.lacks_model_label
    monkeypatch.setattr(amr, "lacks_model_label", lambda review: False)       # the build before this rule
    search, state = _node(node, tmp_path, monkeypatch)
    assert state["member_count"] == 1                                 # the entry; its fact withheld at guard 1
    records, _bindings = _search(search, monkeypatch, "Atlas parser")
    assert len(_kind(records, "journal_entry")) == 1 and _kind(records, "fact") == []
    monkeypatch.setattr(amr, "lacks_model_label", rule)                       # this build
    assert search.index.sweep(now=search.now[0]) == 0                 # the same basis: the index stays
    records, _bindings = _search(search, monkeypatch, "Atlas parser")
    assert records == []


# --- the catch-up ------------------------------------------------------------------------------------------------

def test_the_catch_up_sees_the_flags_rule_and_runs_one_full_pass(tmp_path, monkeypatch):
    """The node's last full pass ran under revisions without the rule: the flag was off, or the build was v1b, which
    named no such rule. With the flag on, this build's revisions differ, so a full pass starts at once, by day."""
    monkeypatch.setenv(JOURNAL_FLAG, "true")
    monkeypatch.delenv(FLAG, raising=False)
    T = daytime()
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = RealRulesLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, full_hours=(2, 6))
    settled(loop, T, assessment_revisions=assessment_revisions())
    assert loop.run_catchup() is None and worker.started == []        # flag off: nothing to do
    monkeypatch.setenv(FLAG, "true")
    clock.now = T + 400
    loop.run_catchup()
    assert len(worker.started) == 1 and worker.started[0]["ingested_after"] is None      # a full pass, now
    finish(worker, scanned=3, assessed=3)
    clock.now = T + 800
    receipt = loop.run_catchup()
    assert (receipt.cause_class, receipt.scope) == ("revision_change", "full_window")
    assert stored_state(tmp_path)["assessment_revisions"]["journal_model_label"] == JOURNAL_MODEL_LABEL_VERSION
    clock.now = T + 1200
    assert loop.run_catchup() is None and len(worker.started) == 1    # recorded: the same rules never run it twice
