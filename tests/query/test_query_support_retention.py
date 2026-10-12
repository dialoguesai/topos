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
