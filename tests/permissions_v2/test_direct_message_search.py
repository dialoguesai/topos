from types import SimpleNamespace
from copy import deepcopy
import pytest
from tests.permissions_v2.test_direct_message_evidence import setup, qualify
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.message_search_harness import Node
from tests.permissions_v2 import message_search_corpus as mc
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.release import source_message_decision
from topos.permissions_v2.message_evidence import message_key
from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds


# Some cases here run a search under a profile a node no longer serves (N8): the suite takes the lift
# (conftest.py `retired_search_profile`) so they run as they did, for the code p2c-v3 shares with it.
pytestmark = pytest.mark.usefixtures("retired_search_profile")

def direct_policy():
    raw = mc.search_policy(max_k=10)
    raw['versions']['capability'] = 'permissions-beta/p2c-v2'
    raw['versions']['subject_binding'] = dict(contract='owner_authored_message_v1',
        authorship='native_provenance_required', classification='whole-message-owner-review/v1',
        fact_prerequisite=False, exclusions='message_and_backing_fact')
    raw['evaluator']['version'] = 'hard-rules/p2c-v2'
    return raw


def node_for(legacy, tmp_path, monkeypatch, **labels):
    resolver, reviews, identity = setup(legacy, **labels)
    stamp = legacy[1].execute('SELECT event_at FROM conversation_messages').fetchone()[0]
    now = canonical_utc_microseconds(stamp) // 1_000_000 + 60
    monkeypatch.setattr(mc, 'NOW', now)
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path/'node',
                model=None, search_raw=direct_policy(), now=now)
    return node, identity


def test_signed_search_releases_real_native_message_without_a_fact(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    assert node.rebuild()['grant-search'] == 'ready'
    output, refused = node.search_request('Synthetic message', k=10)
    assert refused is None
    assert len(output['records']) == 1
    record = output['records'][0]
    assert record['content'] == 'I am working on Synthetic message at work.'
    assert set(record) == {'record_id','source_id','canonical_table','content'}
    assert record['record_id'].startswith('r.')
    assert legacy[1].execute("SELECT count(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0] == 0


@pytest.mark.parametrize('labels', [dict(sensitivity='personal'), dict(sensitivity='special'),
    dict(domains=['work','hobbies']), dict(domains=['work','health'])])
def test_each_whole_message_category_and_sensitivity_must_pass(legacy, labels):
    args = setup(legacy, **labels)
    assert source_message_decision(parse_policy(direct_policy()), qualify(*args)).verdict != 'permit'


def test_v1_cannot_consume_direct_review_or_acquire_v2_semantics(legacy):
    args = setup(legacy)
    with pytest.raises(PolicyError, match='evidence_family_mismatch'):
        source_message_decision(parse_policy(mc.search_policy()), qualify(*args))
    mixed = direct_policy()
    mixed['versions']['capability'] = 'permissions-beta/p2c-v1'
    with pytest.raises(PolicyError):
        parse_policy(mixed)


def test_optout_landing_after_ranking_blocks_final_release(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    import topos.permissions_v2.search_release as module
    original = module.rank
    def race(*args, **kwargs):
        order = original(*args, **kwargs)
        with owner():
            node.corpus.reviews.opt_out(message_key(identity), now=node.now[0])
        return order
    monkeypatch.setattr(module, 'rank', race)
    output, refused = node.search_request('Synthetic message', k=10)
    assert refused is not None or output['records'] == []
