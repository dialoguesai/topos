"""Recovered native sources through signed recipient dispatch, not only qualification."""
from copy import deepcopy
import json
import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_reconciliation_facts import fact_from_proof
from tests.permissions_v2.test_release import release_setup, issue, dispatch
from topos.permissions_v2.reconciliation_facts import classification
from topos.permissions_v2.canonical import PolicyError


@pytest.fixture
def corpus(legacy, request):
    sensitivity = getattr(request, 'param', 'none')
    result, reviews, fact_id = fact_from_proof(legacy, classification({
        'domains':['work','health'] if sensitivity == 'special' else ['work'], 'sensitivity':sensitivity}))
    assert result.verdict == 'qualified'
    return legacy[0].resolver, reviews, fact_id


def policy_native(policy):
    policy['source_universe']['source_ids'] = ['imessage']
    policy['rules'][0]['evidence_use']['sources']['values'] = ['imessage']


def test_signed_recipient_receives_exact_native_source_only(release_setup):
    envelope, payload = issue(release_setup, policy_change=policy_native)
    [(result, output)] = dispatch(release_setup, envelope, payload)
    assert len(output['records']) == 1
    record = output['records'][0]
    assert record['record_id'] == 'imessage:1' and record['source_id'] == 'imessage'
    assert record['content'] == 'I am working on Synthetic message at work.'
    assert 'classification' not in json.dumps(output) and 'native_event' not in json.dumps(output)


@pytest.mark.parametrize('corpus', ['special'], indirect=True)
def test_signed_recipient_gets_nothing_when_source_ceiling_hits_deny(release_setup):
    def policy(policy):
        policy_native(policy)
        deny = deepcopy(policy['rules'][0])
        deny['rule_id'] = 'deny-special'
        deny['effect'] = 'deny'
        predicate = {'kind':'atom','attribute':'sensitivity','operator':'intersects','values':['special']}
        deny['evidence_use']['predicate'] = predicate
        deny['release']['predicate'] = predicate
        policy['rules'].append(deny)
    envelope, payload = issue(release_setup, policy_change=policy)
    with pytest.raises(PolicyError, match='permission_denied'):
        dispatch(release_setup, envelope, payload, send=lambda *args: pytest.fail('withheld source sent'))


def test_revoked_reconciliation_cannot_send_using_already_signed_request(release_setup, legacy):
    envelope, payload = issue(release_setup, policy_change=policy_native)
    with owner():
        legacy[0].revoke(legacy[1], enrollment_id=legacy[3])
    with pytest.raises(PolicyError):
        dispatch(release_setup, envelope, payload, send=lambda *args: pytest.fail('revoked source sent'))
