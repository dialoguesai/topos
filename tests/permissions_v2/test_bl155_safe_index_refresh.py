"""Continuous independent assessments preserve a safe, signed share snapshot.

All evidence is invented and native journal provenance uses the production
qualification path. No local model, personal database or network is used.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_journal_family import (
    node as journal_database, _entry, _journal_policy, _labels, _publish, AFTER_ITS_DAY,
)
from tests.permissions_v2.test_refresh_loop import Clock, loop_for, receipts
from topos.permissions_v2.automatic_message_review import prepare, publish
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.index_rebuilds import IndexRebuilds
from topos.permissions_v2.search_index import index_path

pytestmark = pytest.mark.public
TEXT = 'I am working on a synthetic task at work.'
NEW = 'I completed a second draft at work.'


@pytest.fixture
def sharing(journal_database, tmp_path, monkeypatch):
    _entry(journal_database, 'e1', TEXT)
    _entry(journal_database, 'e2', NEW)
    resolver, reviews = _publish(journal_database, 'e1', domains=['work'])
    monkeypatch.setattr(mc, 'NOW', AFTER_ITS_DAY)
    raw = _journal_policy(kinds=('journal_entry',))
    raw['search']['answers'] = 'records'
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path / 'search',
                model=None, search_raw=raw, now=AFTER_ITS_DAY)
    assert node.rebuild() == {'grant-search': 'ready'}
    return node


@pytest.fixture
def answers_node(sharing):
    node = sharing
    node.search_raw['search']['answers'] = 'only'
    node.search_raw['policy_version_id'] = 'synthetic-answer-policy-v2'
    node.activate(node.search_raw, generation=2)
    node.rebuild()
    return node


def assess(node, entry_id='e2', **labels):
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    identity = resolver._identity('journal_entries', entry_id, 'time_log')
    with owner():
        prepared = prepare(resolver, reviews, identity)
        return publish(resolver, reviews, prepared, _labels(prepared, domains=['work'], **labels), now=node.now[0])


def path(node):
    return index_path(node.index.root, 'grant-search')


def contents(node):
    output, refused = node.search_request('synthetic task draft work', k=10)
    assert refused is None
    return {record['content'] for record in output['records']}


def test_new_independent_assessment_keeps_the_same_serving_file(sharing):
    node = sharing
    before = path(node).read_bytes()
    assess(node)
    assert contents(node) == {TEXT}  # newest evidence is not available until publication
    assert node.index.sweep(now=node.now[0]) == 0
    assert path(node).read_bytes() == before
    assert node.index.take_refresh_needed().keys() == {path(node).name}


def test_refresh_runs_during_a_progressing_pass_and_eventually_adds_evidence(sharing):
    node = sharing
    clock = Clock(node.now[0])
    loop = loop_for(node, clock, max_defer=600, debounce=30)
    loop.observe(node.index)
    assess(node)
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    loop._pass = {'progress_at': clock.now}  # a background pass is still progressing
    assert loop.run_pending() is None  # debounce still applies
    clock.now += 30
    assert contents(node) == {TEXT}
    receipt = loop.run_pending()
    assert receipt.cause_classes == ['review_added']
    assert receipt.grants[0].state == 'ready'
    assert contents(node) == {TEXT, NEW}
    assert TEXT not in json.dumps(receipts(node))


def test_repeated_dirty_observations_do_not_postpone_the_refresh(sharing):
    node = sharing
    clock = Clock(node.now[0])
    loop = loop_for(node, clock, debounce=30)
    assess(node)
    for advance in (0, 10, 10, 10):
        clock.now += advance
        node.index.sweep(now=node.now[0])
        loop.observe(node.index)
    assert loop.run_pending().grants[0].state == 'ready'
    assert contents(node) == {TEXT, NEW}


def test_restart_rediscovers_a_safe_index_that_still_needs_refresh(sharing):
    node = sharing
    loop_for(node, Clock(node.now[0])).observe(node.index)
    assess(node)
    node.index.sweep(now=node.now[0])
    node.index.take_refresh_needed()  # the process lost its in-memory observation
    node.index.sweep(now=node.now[0])
    restarted = loop_for(node, Clock(node.now[0]))
    restarted.observe(node.index)
    assert restarted.run_pending().cause_classes == ['review_added']
    assert contents(node) == {TEXT, NEW}


def test_independent_addition_during_build_does_not_cancel_publication(sharing, monkeypatch):
    node = sharing
    real = node.index._members
    calls = []

    def members(*args, **kwargs):
        built = real(*args, **kwargs)
        if not calls:
            assess(node)
        calls.append(1)
        return built

    monkeypatch.setattr(node.index, '_members', members)
    with owner():
        assert node.index.rebuild('grant-search', now=node.now[0]) == {'state': 'ready', 'member_count': 1}
    assert calls == [1]  # no repeat caused by the unrelated review digest
    assert contents(node) == {TEXT}
    node.index.sweep(now=node.now[0])
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    assert loop.run_pending().grants[0].member_count == 2


@pytest.mark.parametrize('restriction', ['assessment', 'correction', 'opt_out', 'content', 'owner_only'])
def test_changes_to_existing_evidence_refuse_before_refresh(sharing, restriction):
    node = sharing
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    if restriction == 'assessment':
        assess(node, 'e1', sensitivity='special')
    elif restriction == 'correction':
        from topos.permissions_v2.message_evidence import record_message_review
        identity = resolver._identity('journal_entries', 'e1', 'time_log')
        with owner():
            prepared = prepare(resolver, reviews, identity)
            record_message_review(resolver, reviews, review_id='owner-correction',
                expected_snapshot=prepared['snapshot'].model_dump(),
                classification=_labels(prepared, sensitivity='special').model_dump(),
                expected_current_review_revision=None, reviewed_at=node.now[0])
    elif restriction == 'opt_out':
        from topos.permissions_v2.message_evidence import message_key
        with owner():
            reviews.opt_out(message_key(resolver._identity('journal_entries', 'e1', 'time_log')), now=node.now[0])
    else:
        with sqlite3.connect(resolver.path) as db:
            if restriction == 'content':
                db.execute("UPDATE journal_entries SET content='Private changed evidence' WHERE entry_id='e1'")
            else:
                db.execute("INSERT INTO owner_only_records(canonical_table,record_id,created_at,updated_at) "
                           "VALUES('journal_entries','e1','t','t')")
    output, refused = node.search_request('synthetic task work', k=10)
    assert output is None and refused is not None
    assert not path(node).exists()


def test_failed_build_attempts_keep_a_safe_serving_snapshot(sharing, monkeypatch):
    node = sharing
    monkeypatch.setattr(node.index, '_unchanged', lambda *args, **kwargs: False)
    with owner():
        assert node.index.rebuild('grant-search', now=node.now[0])['state'] == 'stale'
    assert contents(node) == {TEXT}


@pytest.mark.parametrize('restricted', [False, True])
def test_owner_queue_build_exception_retains_only_a_proven_safe_snapshot(sharing, monkeypatch, restricted):
    node = sharing
    if restricted:
        assess(node, 'e1', sensitivity='special')

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic build failure')

    monkeypatch.setattr(node.index, '_members', fail)
    queue = IndexRebuilds(ledger=node.ledger, root=node.index.root, index=lambda: node.index,
                          clock=lambda: node.now[0])
    assert queue._build('grant-search')[0] == 'failed'
    assert path(node).exists() is (not restricted)
    if not restricted:
        assert contents(node) == {TEXT}


def test_a_new_over_cap_snapshot_replaces_the_old_ready_snapshot(sharing):
    node = sharing
    raw = dict(node.search_raw)
    raw['search'] = {**raw['search'], 'max_permitted_records': 1}
    raw['policy_version_id'] = 'synthetic-capped-policy-v2'
    node.activate(raw, generation=2)
    node.rebuild()
    assert contents(node) == {TEXT}
    assess(node)
    with owner():
        assert node.index.rebuild('grant-search', now=node.now[0])['state'] == 'over_cap'
    output, refused = node.search_request('synthetic task draft work', k=10)
    assert output is None and refused is not None


def test_supported_answer_survives_independent_refresh_during_generation(answers_node):
    node = answers_node
    service = _service(node, 'unused')

    async def generate(_prompt, *, deadline):
        assess(node)
        with owner():
            node.index.rebuild('grant-search', now=node.now[0])
        return 'The owner is working on a synthetic task [1].'

    service.generate = generate
    try:
        answer_id = _ask(node, service, 'What is the owner working on?')
        assert _finished(node, service, answer_id)['outcome'] == 'answered'
        assert node.index._open(path(node))['count'] == 2
    finally:
        service.close()


def test_restriction_during_generation_still_withholds_the_answer(answers_node):
    node = answers_node
    service = _service(node, 'unused')

    async def generate(_prompt, *, deadline):
        assess(node, 'e1', sensitivity='special')
        return 'The owner is working on a synthetic task [1].'

    service.generate = generate
    try:
        answer_id = _ask(node, service, 'What is the owner working on?')
        assert _finished(node, service, answer_id)['outcome'] == 'no_answer'
    finally:
        service.close()


def test_index_failure_has_a_distinct_owner_local_answer_receipt(answers_node):
    node = answers_node
    path(node).unlink()
    service = _service(node, 'unused')
    try:
        answer_id = _ask(node, service, 'What is the owner working on?')
        assert _finished(node, service, answer_id)['outcome'] == 'no_answer'
        with node.ledger._transaction() as db:
            recorded = db.execute("SELECT decision_json FROM p2a_receipts WHERE request_id='answer-1'").fetchone()[0]
        assert 'index_unavailable' in recorded
    finally:
        service.close()


def test_refresh_retry_budget_survives_sweeps_and_restart(sharing, monkeypatch):
    node = sharing
    clock = Clock(node.now[0])
    loop = loop_for(node, clock, max_attempts=2, backoff=1)
    loop.observe(node.index)
    assess(node)
    monkeypatch.setattr(node.index, 'rebuild', lambda *args, **kwargs: {'state': 'stale', 'member_count': 0})
    for _ in range(2):
        node.index.sweep(now=node.now[0])
        loop.observe(node.index)
        assert loop.run_pending().grants[0].state == 'stale'
        clock.now += 1
    assert contents(node) == {TEXT}
    restarted = loop_for(node, clock, max_attempts=2, backoff=1)
    node.index.sweep(now=node.now[0])
    restarted.observe(node.index)
    assert restarted.run_pending() is None
    # A genuinely new target earns its own bounded refresh budget.
    assess(node)
    node.index.sweep(now=node.now[0])
    restarted.observe(node.index)
    assert restarted.run_pending().grants[0].state == 'stale'


@pytest.mark.parametrize('guard', [None, 'unsupported/v99'])
def test_legacy_and_unknown_guard_versions_do_not_keep_stale_evidence(sharing, guard):
    node = sharing
    with sqlite3.connect(path(node)) as db:
        basis = json.loads(db.execute('SELECT basis_json FROM meta').fetchone()[0])
        if guard is None:
            basis.pop('review_guard_version')
            basis.pop('opt_out_revision')
        else:
            basis['review_guard_version'] = guard
        db.execute('UPDATE meta SET basis_json=?', (json.dumps(basis),))
    if guard is None:
        assert contents(node) == {TEXT}  # ordinary pre-upgrade basis is still supported
    assess(node)
    assert node.index.sweep(now=node.now[0]) == 1
    assert not path(node).exists()


def test_stale_request_cannot_delete_a_concurrent_good_publication(sharing, monkeypatch):
    node = sharing
    real = node.index._current
    replaced = []

    def current(*args, **kwargs):
        if not replaced:
            replaced.append(True)
            with owner():
                node.index.rebuild('grant-search', now=node.now[0])
            return False
        return real(*args, **kwargs)

    monkeypatch.setattr(node.index, '_current', current)
    with node.ledger._transaction() as db:
        authority, _policy = node.ledger._authority(db, 'grant-search', node.now[0])
    with pytest.raises(PolicyError, match='search_index_stale'):
        node.index.check_own('grant-search', authority, now=node.now[0])
    assert path(node).exists()
    assert contents(node) == {TEXT}


def test_review_bindings_are_sealed_and_missing_coverage_fails_closed(sharing):
    from topos.permissions_v2.search_index import seal, unseal
    node = sharing
    key = node.index.keys.get('grant-search', create=False)
    with sqlite3.connect(path(node)) as db:
        opaque, encrypted = db.execute('SELECT opaque_id,sealed FROM members').fetchone()
        member = unseal(key, opaque, encrypted)
        assert len(member['review_bindings']) == 2
        for binding in member['review_bindings']:
            assert binding['key'].encode() not in path(node).read_bytes()
        member['review_bindings'].pop()
        db.execute('UPDATE members SET sealed=? WHERE opaque_id=?', (seal(key, opaque, member), opaque))
    assess(node)
    assert node.index.sweep(now=node.now[0]) == 1
    assert not path(node).exists()
