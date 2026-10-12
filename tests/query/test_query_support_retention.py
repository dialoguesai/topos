"""Specific asks keep their supporting evidence through independent lane growth."""
from datetime import datetime, timezone
from topos.query.retrieval import _rrf_fuse_summary_lists

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def test_dense_enrichment_does_not_evict_specific_source_or_client_prefix():
    enrichment = [{'record_id': f'd{i}', 'summary_text': 'Standing relationship and goal'} for i in range(80)]
    source = {'record_id': 'source', 'summary_text': 'Indigo badger calibration results', 'event_at': '2026-01-01T00:00:00Z'}
    out = _rrf_fuse_summary_lists([('derived_objects', 1.5, enrichment), ('canonical', 1, [source])], now=NOW, rare_token_groups=[{'indigo': 1, 'badger': 2}])
    assert out[0]['record_id'] == 'source'
    assert len(out) == 25
    assert any(i['record_id'].startswith('d') for i in out)


def test_deduplication_keeps_substantive_payload_not_context_pointer():
    out = _rrf_fuse_summary_lists([
        ('vector_context', 3, [{'record_id': 'same', 'summary_text': 'Nearby material'}]),
        ('canonical', 1, [{'record_id': 'same', 'summary_text': 'Indigo badger calibration'}])],
        rare_token_groups=[{'badger': 1}], context_sources=frozenset({'vector_context'}), now=NOW)
    assert out[0]['summary_text'] == 'Indigo badger calibration'


def test_budget_cannot_release_packet_that_fails_its_admission_condition():
    out = _rrf_fuse_summary_lists([('canonical', 1, [
        {'record_id': 'a', 'summary_text': 'indigo'}, {'record_id': 'b', 'summary_text': 'badger'}])],
        rare_token_groups=[{'indigo': 1, 'badger': 1}], cap=1, now=NOW)
    assert out == []


def test_one_answerable_part_survives_unanswerable_part():
    out = _rrf_fuse_summary_lists([('canonical', 1, [{'record_id': 'a', 'summary_text': 'indigo'}])],
        rare_token_groups=[{'indigo': 1}, {'badger': 0}], now=NOW)
    assert out[0]['record_id'] == 'a'


def test_context_cannot_supply_missing_evidence():
    out = _rrf_fuse_summary_lists([
        ('vector_context', 3, [{'record_id': 'same', 'summary_text': 'indigo badger'}]),
        ('canonical', 1, [{'record_id': 'same', 'summary_text': 'Unrelated work'}])],
        rare_token_groups=[{'badger': 1}], context_sources=frozenset({'vector_context'}), now=NOW)
    assert out == []


def test_primary_source_head_survives_enrichment_and_cross_lane_votes():
    """Source coverage alone is insufficient: the best source rows must survive."""
    canonical = [{'record_id': f'c{i}', 'summary_text': f'Primary finding {i}'} for i in range(10)]
    filler = [{'record_id': f'd{i}', 'summary_text': 'Derived context'} for i in range(60)]
    out = _rrf_fuse_summary_lists([
        ('derived_objects', 2, filler), ('canonical', 1, canonical),
        ('recent', 2, canonical[2:]),
    ], now=NOW, source_head_guarantees={'canonical': 2})
    assert [i['record_id'] for i in out[:2]] == ['c0', 'c1']
    assert len(out) == 25
    assert any(i['record_id'].startswith('d') for i in out)


def test_primary_head_cannot_displace_required_query_witness_or_override_empty():
    lanes = [('canonical', 1, [
        {'record_id': 'head', 'summary_text': 'Other source finding'},
        {'record_id': 'needle', 'summary_text': 'Indigo badger'},
    ])]
    out = _rrf_fuse_summary_lists(lanes, now=NOW, cap=1,
        rare_token_groups=[{'indigo': 1}], source_head_guarantees={'canonical': 2})
    assert out[0]['record_id'] == 'needle'
    assert _rrf_fuse_summary_lists(lanes, now=NOW,
        rare_token_groups=[{'unsupported': 0}], source_head_guarantees={'canonical': 2}) == []


def test_context_cannot_receive_a_primary_head_guarantee():
    out = _rrf_fuse_summary_lists([
        ('context', 0.1, [{'record_id': 'context', 'summary_text': 'Decorative context'}]),
        ('canonical', 1, [{'record_id': 'evidence', 'summary_text': 'Evidence'}]),
    ], now=NOW, cap=1, source_head_guarantees={'context': 1}, context_sources=frozenset({'context'}))
    assert out[0]['record_id'] == 'evidence'


def test_goal_index_growth_respects_existing_budget_without_losing_other_types():
    direct = [{'record_id': f'g{i}', 'retrieval_source': 'user_goal', 'summary_text': f'Goal {i}'} for i in range(8)]
    indexed = [{'record_id': f'd{i}', 'retrieval_source': 'derived:Goal', 'summary_text': f'Indexed goal {i}'} for i in range(40)]
    source = {'record_id': 'source', 'retrieval_source': 'canonical:journal_entries', 'summary_text': 'Project evidence'}
    entity = {'record_id': 'entity', 'retrieval_source': 'derived:entity_dossier', 'summary_text': 'Project relationship'}
    out = _rrf_fuse_summary_lists([('derived_objects', 1.5, indexed + [entity]), ('goals', 1.4, direct), ('canonical', 1, [source])], goal_cap=8, now=NOW)
    ids = {i['record_id'] for i in out}
    assert {i['record_id'] for i in direct} <= ids
    assert {'source', 'entity'} <= ids
    assert not any(i['retrieval_source'] == 'derived:Goal' for i in out)


def test_indexed_goal_needed_by_specific_ask_survives_ordinary_goal_budget():
    direct = [{'record_id': f'g{i}', 'retrieval_source': 'user_goal', 'summary_text': 'Standing goal'} for i in range(8)]
    needle = {'record_id': 'needle', 'retrieval_source': 'derived:user_goals', 'summary_text': 'Indigo badger field study'}
    out = _rrf_fuse_summary_lists([('goals', 1.4, direct), ('derived_objects', 1.5, [needle])],
        goal_cap=8, rare_token_groups=[{'indigo': 1, 'badger': 1}], now=NOW)
    assert out[0]['record_id'] == 'needle'
