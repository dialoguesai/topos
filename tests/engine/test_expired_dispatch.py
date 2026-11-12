"""Expiry during preprocessing must prevent new work on every Engine provider."""
from types import SimpleNamespace
import pytest
from topos.engine import engine
from topos.engine.tasks import ModelRequest, ProcessingTask
from topos.engine.request_budget import RequestBudget, budget_scope


@pytest.mark.parametrize('provider', ['ollama', 'openai', 'huggingface'])
@pytest.mark.parametrize('phase', ['policy', 'cache'])
def test_expired_preprocessing_never_dispatches_provider(monkeypatch, provider, phase):
    budget = RequestBudget(1)
    calls = []
    def policy(task):
        if phase == 'policy': budget.cancel('deadline_exceeded')
        return task, None
    def cache(*args):
        if phase == 'cache': budget.cancel('deadline_exceeded')
        return False
    monkeypatch.setattr(engine, 'apply_blackhole_egress_policy', policy)
    monkeypatch.setattr(engine.Engine, '_build_inference_config', lambda *args: {'model':'synthetic'})
    monkeypatch.setattr(engine, 'get_adapter_for_task', lambda task: SimpleNamespace(
        run_inference=lambda *a, **k: calls.append(task.id) or {}))
    monkeypatch.setattr(engine, 'get_model_cache', lambda: SimpleNamespace(touch_for_task=cache, resident_slots=lambda: []))
    monkeypatch.setattr(engine, 'get_process_rss_mb', lambda: None)
    task = ProcessingTask(id='synthetic-expired', type='truth_verdict', subtype='truth_verdict',
                         input={}, model_request=ModelRequest(provider=provider, model='synthetic'))
    with budget_scope(budget):
        result = engine.Engine().run(task)
    assert not calls
    assert result.status == 'deferred'
    assert result.output['availability']['reason'] == 'deadline_exceeded'
