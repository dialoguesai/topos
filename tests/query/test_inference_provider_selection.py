"""Query inference honors explicit provider selection without local fallback."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from topos.config.settings import Settings, settings
from topos.query import inference


@pytest.mark.parametrize('provider,model,expected_model', [
    ('ollama', None, 'local-test-model'),
    ('redpill', None, 'openai/gpt-oss-20b'),
    ('redpill', 'meta-llama/llama-3.3-70b-instruct', 'meta-llama/llama-3.3-70b-instruct'),
    ('openai', None, 'hosted-test-model'),
    ('platform', 'explicit-model', 'explicit-model'),
])
def test_selected_provider_reaches_engine(monkeypatch, provider, model, expected_model):
    tasks = []
    def run(task):
        tasks.append(task)
        return SimpleNamespace(status='completed', output={'answer': 'supported', 'confidence': .8})
    monkeypatch.setattr(settings, 'query_inference_provider', provider)
    monkeypatch.setattr(settings, 'query_inference_model', model)
    monkeypatch.setattr(settings, 'ollama_query_model', 'local-test-model')
    monkeypatch.setattr(settings, 'openai_model', 'hosted-test-model')
    monkeypatch.setattr(inference, 'get_engine_client_or_local', lambda _: SimpleNamespace(run=run))
    result = inference.run_query_inference(query_text='What changed?', context_packet={}, scope_id='work_context:read')
    assert result['answer'] == 'supported'
    assert len(tasks) == 1
    assert tasks[0].model_request.provider == provider
    assert tasks[0].model_request.model == expected_model


@pytest.mark.parametrize('failure', ['deferred', 'failed', 'exception'])
def test_hosted_failure_never_retries_locally(monkeypatch, failure):
    calls = []
    def run(task):
        calls.append(task.model_request.provider)
        if failure == 'exception':
            raise RuntimeError('provider unavailable')
        return SimpleNamespace(status=failure, output={}, error='provider unavailable')
    monkeypatch.setattr(settings, 'query_inference_provider', 'redpill')
    monkeypatch.setattr(settings, 'query_inference_model', None)
    monkeypatch.setattr(inference, 'get_engine_client_or_local', lambda _: SimpleNamespace(run=run))
    result = inference.run_query_inference(query_text='What changed?', context_packet={}, scope_id='work_context:read')
    assert result['answer'] == 'unknown'
    assert result['error'] == 'provider unavailable'
    assert calls == ['redpill']


def test_provider_env_is_validated(monkeypatch):
    monkeypatch.setenv('TOPOS_QUERY_INFERENCE_PROVIDER', 'redpill')
    monkeypatch.setenv('TOPOS_QUERY_INFERENCE_MODEL', 'meta-llama/llama-3.3-70b-instruct')
    config = Settings(_env_file=None)
    assert config.query_inference_provider == 'redpill'
    assert config.query_inference_model == 'meta-llama/llama-3.3-70b-instruct'
    monkeypatch.setenv('TOPOS_QUERY_INFERENCE_PROVIDER', 'typo')
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
