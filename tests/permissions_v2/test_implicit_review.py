"""Implicit review: a qualifying fact is available unless the owner deselected it (EVIDENCE.md).

What is pinned here: an unreviewed owner fact qualifies with the node's own labels, an
owner-asserted `owner_only` fact qualifies while other disclosures still withhold, the
owner's opt-out beats every review and its undo restores availability, opt-outs enter the
store's authority digest (a rolled-back store is refused), the review queue is least
confident first with the fields the owner surface needs, the totals give "N of M" per
source, the handlers are owner-only, the runtime defaults (flag on, store path derived,
startup enrolment) need no owner edit, and the p2c index is built over every qualifying
non-deselected fact with the node write gate held only to freeze and to publish.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node
from tests.permissions_v2.test_evidence import attest, corpus, decision, edit, owner, payload  # noqa: F401
from tests.permissions_v2.test_evidence_reviews import message, paired_runtime  # noqa: F401
from topos.core.handlers import handle_control_plane_request
from topos.features.facts.store import FactStore
from topos.permissions_v2 import evidence as evidence_module, search_index
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import IMPLICIT_FALLBACK, IMPLICIT_LABELS, EvidenceReviewStore, implicit_labels
from topos.permissions_v2.evidence_review_runtime import ReviewEnrollmentRuntime
from topos.permissions_v2.evidence_reviews import (EvidenceLookup, EvidenceReviewService, FactOptIn, FactOptOut,
    OptOutMutation, ReviewQueuePage, ReviewQueueRequest, ReviewTotals, ReviewTotalsRequest)
from topos.permissions_v2.runtime import DEFAULT_EVIDENCE_REVIEW_STORE, EVIDENCE_REVIEWS_FLAG, load_runtime
from topos.principal import OWNER_APP, THIRD_PARTY, Principal

REF = {"table": "conversation_messages", "dataset_id": "dataset-1", "source_id": "source-1", "record_id": "message-1"}


def add_fact(corpus, *, predicate, value, confidence, disclosure="owner_only", asserted_by="owner", refs=None):
    with sqlite3.connect(corpus[0].path) as conn:
        return FactStore(conn).assert_fact(subject_entity_id="self", predicate=predicate, object_value=value,
            confidence=confidence, disclosure=disclosure, asserted_by=asserted_by,
            source_refs=[REF] if refs is None else refs)["object_id"]


# --- qualification ------------------------------------------------------------------------------

def test_an_unreviewed_owner_fact_qualifies_implicitly_with_the_nodes_labels(corpus):
    result = decision(corpus)
    assert result.verdict == "qualified" and result.reason_code == "implicit_review_current_evidence"
    evidence = result.evidence
    assert evidence.review_mode == "implicit" and evidence.review_id == "implicit:" + corpus[2]
    assert {item.domains[0] for item in evidence.classifications} == {"hobbies"}   # `prefers`
    assert {item.sensitivity for item in evidence.classifications} == {"personal"}
    assert all(item.authorship == "owner_authored" and item.speech == "direct_self_statement"
               and item.independent_copies == "none_known" for item in evidence.classifications)
    # Nothing was written: the store has no review row and no opt-out row.
    with sqlite3.connect(corpus[1].path) as db:
        assert db.execute("SELECT count(*) FROM fact_reviews").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM fact_opt_outs").fetchone()[0] == 0


def test_implicit_labels_cover_every_known_predicate_and_fall_back_to_the_most_protective_pairing():
    from topos.features.facts.store import KNOWN_PREDICATES
    assert KNOWN_PREDICATES <= set(IMPLICIT_LABELS)
    for predicate in KNOWN_PREDICATES:
        domains, sensitivity = implicit_labels({"predicate": predicate})
        assert domains and sensitivity in {"none", "personal", "special"}
    assert implicit_labels({"predicate": "practices"}) == (("health",), "special")
    assert implicit_labels({"predicate": "owes"}, "profile") == IMPLICIT_FALLBACK == (("relationships",), "special")
    assert implicit_labels({"predicate": "unknown_thing"}, "work") == (("work",), "none")


@pytest.mark.parametrize("disclosure, verdict, reason", [
    ("owner_only", "qualified", "implicit_review_current_evidence"),
    ("scoped", "qualified", "implicit_review_current_evidence"),
    (None, "withheld", "owner_only"), ("unknown", "withheld", "owner_only"), ("public", "withheld", "owner_only")])
def test_owner_only_no_longer_withholds_an_owner_asserted_fact_while_other_disclosures_do(corpus, disclosure, verdict, reason):
    payload(corpus, disclosure=disclosure)
    result = decision(corpus)
    assert (result.verdict, result.reason_code) == (verdict, reason)


def test_a_fact_asserted_by_someone_else_is_still_withheld_under_implicit_review(corpus):
    payload(corpus, disclosure="scoped", asserted_by="contact:other")
    assert decision(corpus).reason_code == "not_owner_self_statement"


def test_the_integrity_checks_still_run_on_an_implicit_review(corpus):
    edit(corpus, "UPDATE conversation_messages SET is_from_self=0")
    assert decision(corpus).reason_code == "not_owner_authored"


def test_opt_out_withholds_and_opt_in_restores_and_both_are_idempotent(corpus):
    with owner():
        assert corpus[1].opt_out(corpus[2], now=1200, note="not for apps") is True
        assert corpus[1].opt_out(corpus[2], now=1201) is False
    assert decision(corpus).reason_code == "owner_opted_out"
    with owner():
        assert corpus[1].opt_in(corpus[2]) is True
        assert corpus[1].opt_in(corpus[2]) is False
    assert decision(corpus).reason_code == "implicit_review_current_evidence"


def test_the_owners_deselection_beats_an_explicit_review_and_a_revoked_review_returns_to_implicit(corpus):
    review = attest(corpus)
    assert decision(corpus).reason_code == "owner_reviewed_current_evidence"
    with owner():
        corpus[1].opt_out(corpus[2], now=1200)
    assert decision(corpus).reason_code == "owner_opted_out"
    with owner():
        corpus[1].opt_in(corpus[2])
        assert decision(corpus).evidence.review_mode == "explicit"
        corpus[1].revoke_review(review.review_id)
    assert decision(corpus).evidence.review_mode == "implicit"


@pytest.mark.parametrize("principal", [None, dict(cls=THIRD_PARTY, channel="cp_relay", actor="owner-1"),
                                       dict(cls=OWNER_APP, channel="uds", actor="someone-else")])
def test_opt_out_and_opt_in_need_the_owner(corpus, principal):
    @contextmanager
    def as_principal():
        if principal is None:
            yield
        else:
            with owner(**principal):
                yield
    with as_principal(), pytest.raises(PolicyError, match="owner_authority_required"):
        corpus[1].opt_out(corpus[2], now=1200)
    with as_principal(), pytest.raises(PolicyError, match="owner_authority_required"):
        corpus[1].opt_in(corpus[2])


def test_a_message_backing_a_deselected_sibling_fact_is_withheld_for_raw_release(corpus):
    sibling = add_fact(corpus, predicate="lives_in", value="a town", confidence=0.4)
    assert corpus[0].qualify(corpus[2], reviews=corpus[1]).verdict == "qualified"
    def released(evidence, _rows):
        return evidence.review_mode
    assert corpus[0].with_qualified(corpus[2], reviews=corpus[1], callback=released, discloses_sources=True) == "implicit"
    with owner():
        corpus[1].opt_out(sibling, now=1200)
    with pytest.raises(PolicyError, match="owner_opted_out"):
        corpus[0].with_qualified(corpus[2], reviews=corpus[1], callback=released, discloses_sources=True)
    # The scalar path never runs the sibling floor, so the fact itself still qualifies.
    assert corpus[0].qualify(corpus[2], reviews=corpus[1]).verdict == "qualified"


# --- the store: digest and reopen ---------------------------------------------------------------

def test_opt_outs_enter_the_authority_digest_and_a_rolled_back_store_is_refused(corpus, tmp_path):
    resolver = corpus[0]
    store_path = tmp_path / "enrolled-reviews.db"
    runtime = ReviewEnrollmentRuntime(canonical_database=resolver.path, binding=resolver.binding, path=store_path)
    with owner():
        service = runtime.get(require_existing=False)
        before_digest = service.reviews.current_authority_digest()
        untouched = store_path.read_bytes()
        mutation = service.opt_out(FactOptOut(fact_id=corpus[2]), now=1200)
        assert mutation.action == "opted_out" and mutation.changed is True and mutation.state.opted_out is True
        assert mutation.state.review_mode == "opted_out" and mutation.state.qualification.reason_code == "owner_opted_out"
        after_digest = service.reviews.current_authority_digest()
    assert after_digest != before_digest
    marker = json.loads(store_path.with_name(store_path.name + ".enrollment.json").read_text())
    assert marker["authority_digest"] == after_digest and marker["state"] == "active"
    # An in-place restore of the file from before the deselection would widen release: refused.
    store_path.write_bytes(untouched)
    with owner(), pytest.raises(PolicyError, match="review_store_rollback"):
        service.read(EvidenceLookup(fact_id=corpus[2]))


def test_a_store_without_the_opt_out_table_reopens_with_its_marker_intact(corpus, tmp_path):
    """A store an older engine wrote has no `fact_opt_outs`; the reopen creates it and the digest is unchanged."""
    resolver = corpus[0]
    store_path = tmp_path / "older-reviews.db"
    runtime = ReviewEnrollmentRuntime(canonical_database=resolver.path, binding=resolver.binding, path=store_path)
    with owner():
        service = runtime.get(require_existing=False)
        attest((resolver, service.reviews, corpus[2]))
        digest_before = service.reviews.current_authority_digest()
    with sqlite3.connect(store_path) as db:
        db.execute("DROP TABLE fact_opt_outs")
    reopened = ReviewEnrollmentRuntime(canonical_database=resolver.path, binding=resolver.binding, path=store_path)
    with owner():
        again = reopened.get(require_existing=True)
        assert again.reviews.current_authority_digest() == digest_before
        assert again.read(EvidenceLookup(fact_id=corpus[2])).qualification.verdict == "qualified"
    with sqlite3.connect(store_path) as db:
        assert db.execute("SELECT count(*) FROM fact_opt_outs").fetchone()[0] == 0


def test_the_store_file_stays_private(corpus):
    assert stat.S_IMODE(os.stat(corpus[1].path).st_mode) == 0o600


# --- the queue and the totals ----------------------------------------------------------------------

def test_review_queue_is_least_confident_first_with_labels_standing_and_source_counts(corpus):
    service = EvidenceReviewService(corpus[0], corpus[1])
    low = add_fact(corpus, predicate="works_on", value="the roadmap", confidence=0.31)
    other = add_fact(corpus, predicate="lives_in", value="a town", confidence=0.55, asserted_by="contact:other", disclosure="scoped")
    with owner():
        corpus[1].opt_out(low, now=1200, note="private")
        page = service.queue(ReviewQueueRequest())
    assert isinstance(page, ReviewQueuePage) and page.total == 3 and page.offset == 0 and page.limit == 50
    assert [item.fact_id for item in page.items] == [low, other, corpus[2]]           # 0.31, 0.55, 0.7
    first, second, third = page.items
    assert (first.predicate, first.object_value, first.domains, first.sensitivity) == ("works_on", "the roadmap", ["work"], "none")
    assert first.opted_out is True and first.review_mode == "opted_out"
    assert first.qualification.reason_code == "owner_opted_out"
    assert second.qualification.reason_code == "not_owner_self_statement" and second.review_mode == "implicit"
    assert second.asserted_by == "contact:other" and second.disclosure == "scoped"
    assert third.qualification.verdict == "qualified" and third.review_mode == "implicit"
    assert third.source_ids == ["source-1"] and third.terminal_source_count == 1 and third.confidence_permille == 700
    assert third.altitude is None and third.disclosure == "scoped"
    # Never a terminal message's text.
    dumped = json.dumps(page.model_dump())
    assert "history books" in dumped and "I enjoy reading" not in dumped
    with owner():
        without = service.queue(ReviewQueueRequest(include_opted_out=False))
        assert [item.fact_id for item in without.items] == [other, corpus[2]] and without.total == 2
        paged = service.queue(ReviewQueueRequest(offset=1, limit=1))
        assert [item.fact_id for item in paged.items] == [other] and paged.total == 3
        none = service.queue(ReviewQueueRequest(source_id="no-such-source"))
        assert none.items == [] and none.total == 0


def test_totals_give_n_of_m_facts_shareable_per_source(corpus):
    service = EvidenceReviewService(corpus[0], corpus[1])
    low = add_fact(corpus, predicate="works_on", value="the roadmap", confidence=0.31)
    add_fact(corpus, predicate="lives_in", value="a town", confidence=0.55, asserted_by="contact:other", disclosure="scoped")
    add_fact(corpus, predicate="member_of", value="a club", confidence=0.6, refs=[])   # lineage_missing: withheld
    with owner():
        corpus[1].opt_out(low, now=1200)
        totals = service.totals(ReviewTotalsRequest())
    assert isinstance(totals, ReviewTotals)
    assert (totals.facts, totals.qualifying, totals.opted_out, totals.withheld) == (4, 1, 1, 2)
    by_source = {item.source_id: item for item in totals.sources}
    assert (by_source["source-1"].facts, by_source["source-1"].qualifying, by_source["source-1"].opted_out) == (3, 1, 1)
    assert by_source[None].facts == 1 and by_source[None].withheld == 1


def test_queue_and_totals_are_owner_only(corpus):
    service = EvidenceReviewService(corpus[0], corpus[1])
    with owner(cls=THIRD_PARTY, channel="cp_relay"), pytest.raises(PolicyError, match="owner_authority_required"):
        service.queue(ReviewQueueRequest())
    with pytest.raises(PolicyError, match="owner_authority_required"):
        service.totals(ReviewTotalsRequest())


# --- the handlers -----------------------------------------------------------------------------------

def frame(corpus, operation, body):
    """One relay frame for an owner evidence operation; `body` may be empty (`message()` would substitute a lookup)."""
    return {"id": "request-1", "type": "permissions_v2_evidence_" + operation,
            "payload": {"binding": corpus[0].binding.model_dump(), "request": body}}


@pytest.mark.asyncio
async def test_owner_handlers_serve_the_queue_the_totals_and_the_deselection(corpus, paired_runtime):
    principal = Principal(OWNER_APP, "cp_relay", acting_user="owner-1")
    queued = await handle_control_plane_request(frame(corpus, "review_queue", {"limit": 10}), principal=principal)
    assert queued["status"] == "ok"
    page = ReviewQueuePage.parse(queued["payload"])
    assert [item.fact_id for item in page.items] == [corpus[2]] and page.items[0].review_mode == "implicit"
    assert page.items[0].confidence_permille == 700
    totals = await handle_control_plane_request(frame(corpus, "totals", {}), principal=principal)
    assert ReviewTotals.parse(totals["payload"]).qualifying == 1
    out = await handle_control_plane_request(frame(corpus, "opt_out", {"fact_id": corpus[2], "note": "keep private"}),
                                             principal=principal)
    mutation = OptOutMutation.parse(out["payload"])
    assert mutation.action == "opted_out" and mutation.changed is True and mutation.state.review_mode == "opted_out"
    totals = await handle_control_plane_request(frame(corpus, "totals", {}), principal=principal)
    assert ReviewTotals.parse(totals["payload"]).opted_out == 1
    back = await handle_control_plane_request(frame(corpus, "opt_in", {"fact_id": corpus[2]}), principal=principal)
    assert OptOutMutation.parse(back["payload"]).state.qualification.reason_code == "implicit_review_current_evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, body", [("review_queue", {}), ("totals", {}), ("opt_out", {"fact_id": "x"}), ("opt_in", {"fact_id": "x"})])
async def test_the_new_handlers_refuse_every_non_owner(corpus, paired_runtime, operation, body):
    for principal in (None, Principal(THIRD_PARTY, "cp_relay", acting_user="owner-1"), Principal(OWNER_APP, "cp_relay", acting_user="other")):
        response = await handle_control_plane_request(frame(corpus, operation, body), principal=principal)
        assert response["code"] == 403 and "payload" not in response


# --- runtime defaults ---------------------------------------------------------------------------------

def node_config(corpus, tmp_path, **overrides):
    durable = tmp_path / "permissions-v2"
    durable.mkdir(mode=0o700, exist_ok=True)
    signing = durable / "node.key"
    signing.write_text(bytes(range(32)).hex())
    signing.chmod(0o600)
    config = {"version": "topos-policy-node-config/v1", "identity": corpus[0].binding.model_dump(),
              "cp_issuer_id": "beta-cp", "frontend_client_id": "permissions-beta-web", "trusted_cp_keys": {"cp-key": "a" * 64},
              "node_signing_kid": "node-key", "node_signing_key_path": str(signing), "canonical_database_path": str(corpus[0].path),
              "ledger_path": str(durable / "ledger.db"), **overrides}
    path = durable / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path, durable


def test_reviews_are_on_by_default_and_the_store_path_defaults_into_the_durable_directory(corpus, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
    monkeypatch.delenv(EVIDENCE_REVIEWS_FLAG, raising=False)
    path, durable = node_config(corpus, tmp_path)   # no evidence_review_store_path
    runtime = load_runtime(path, active_database=corpus[0].path)
    try:
        assert runtime.evidence_review_store_path == durable / DEFAULT_EVIDENCE_REVIEW_STORE
        assert not runtime.evidence_review_store_path.exists()
        # Startup enrolment, as the node's own process: no owner action, no request principal.
        assert runtime.ensure_evidence_reviews() is True
        store = runtime.evidence_review_store_path
        assert store.exists() and stat.S_IMODE(store.stat().st_mode) == 0o600
        assert store.with_name(store.name + ".enrollment.json").exists()
        service = runtime.evidence_reviews(require_existing=True)
        with owner():
            assert service.read(EvidenceLookup(fact_id=corpus[2])).review_mode == "implicit"
        monkeypatch.setenv(EVIDENCE_REVIEWS_FLAG, "false")
        with pytest.raises(PolicyError, match="evidence_reviews_disabled"):
            runtime.evidence_reviews(require_existing=True)
    finally:
        runtime.close()


def test_a_configured_store_path_is_still_honoured(corpus, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
    path, durable = node_config(corpus, tmp_path, evidence_review_store_path=str(durable_path(tmp_path) / "reviews.db"))
    runtime = load_runtime(path, active_database=corpus[0].path)
    try:
        assert runtime.evidence_review_store_path == durable / "reviews.db"
        assert runtime.ensure_evidence_reviews() is True and (durable / "reviews.db").exists()
    finally:
        runtime.close()


def durable_path(tmp_path):
    return tmp_path / "permissions-v2"


# --- the p2c index ---------------------------------------------------------------------------------------

@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=11, counts={"clean_positive_C": 2, "unreviewed": 2, "not_scoped": 1,
        "sibling_owner_only": 1, "opted_out": 1, "sibling_opted_out": 1, "adjacent_negative": 1})
    node = Node(corpus, tmp_path, model=None)
    node.rebuild()
    return node


def members_of(node):
    with sqlite3.connect(search_index.index_path(node.index.root, node.search_raw["binding"]["grant_id"])) as conn:
        return conn.execute("SELECT count(*) FROM members").fetchone()[0]


def test_the_index_holds_every_qualifying_non_deselected_fact_and_follows_opt_out_and_opt_in(node):
    expected = sum(unit.search_release for unit in node.corpus.units)
    assert expected == 6 and members_of(node) == expected
    released = next(unit for unit in node.corpus.units if unit.kind == "unreviewed")
    with owner(actor=mc.OWNER_ID):
        node.corpus.reviews.opt_out(released.fact_id, now=mc.NOW - 30)
    node.rebuild()
    assert members_of(node) == expected - 1
    with owner(actor=mc.OWNER_ID):
        node.corpus.reviews.opt_in(released.fact_id)
    node.rebuild()
    assert members_of(node) == expected


def test_the_build_reads_outside_the_node_write_gate_and_publishes_inside_it(node, monkeypatch):
    reads = []
    original = evidence_module.EvidenceResolver._read

    def recording(self, *, gated=True):
        reads.append(gated)
        return original(self, gated=gated)
    monkeypatch.setattr(evidence_module.EvidenceResolver, "_read", recording)
    node.rebuild()
    # One ungated snapshot read for the build itself, between the gated freeze and the gated re-check.
    assert reads.count(False) == 1 and reads.count(True) >= 2
    assert reads.index(False) > 0 and reads.index(False) < len(reads) - 1


def test_a_review_change_during_the_build_is_detected_and_the_build_retried(node, monkeypatch):
    index = node.index
    original = index._unchanged
    calls = []

    def flaky(frozen, floor, clock):
        calls.append(1)
        return len(calls) > 1 and original(frozen, floor, clock)
    monkeypatch.setattr(index, "_unchanged", flaky)
    assert node.rebuild()
    assert len(calls) == 2 and members_of(node) == 6


@pytest.mark.skipif(os.environ.get("TOPOS_MEASURE_INDEX_BUILD") != "1", reason="set TOPOS_MEASURE_INDEX_BUILD=1 to time the build")
def test_measure_index_build_on_300_facts_and_27k_signal_objects(tmp_path):
    """The MERGE GATE measurement: total build time and time inside the node write gate."""
    corpus = mc.build(tmp_path / "corpus", seed=29, counts={"unreviewed": 300})
    with sqlite3.connect(corpus.path) as conn:
        now = mc._iso(mc.NOW)
        conn.executemany("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                         "confidence, source_refs_json, valid_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [(f"insight-{number}", "profile", "insight", f"insight:{number}", json.dumps({"text": "x"}), 0.5, "[]",
                           now, now, now) for number in range(27_000)])
        conn.commit()
    node = Node(corpus, tmp_path, model=None)
    gate_time, depth, entered = [0.0], [0], [0.0]

    @contextmanager
    def timed_gate():
        if depth[0] == 0:
            entered[0] = time.perf_counter()
        depth[0] += 1
        try:
            with search_index._real_gate():
                yield
        finally:
            depth[0] -= 1
            if depth[0] == 0:
                gate_time[0] += time.perf_counter() - entered[0]
    search_index._real_gate = search_index.with_db_write
    search_index.with_db_write = timed_gate
    evidence_module.with_db_write = timed_gate
    try:
        started = time.perf_counter()
        with owner(actor=mc.OWNER_ID):
            result = node.index.rebuild(node.search_raw["binding"]["grant_id"], now=mc.NOW)
        total = time.perf_counter() - started
    finally:
        search_index.with_db_write = search_index._real_gate
        evidence_module.with_db_write = search_index._real_gate
    print(f"\nindex build: state={result['state']} members={result['member_count']} total={total:.2f}s gate_held={gate_time[0]:.2f}s")
    assert result["member_count"] == 300
