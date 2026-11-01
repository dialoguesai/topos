"""The configured remote Engine preserves worker parentage and overload evidence."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from topos.engine import client, engine
from topos.engine.overload import ComputeUnavailable
from topos.engine.tasks import ModelRequest, ProcessingTask
from topos.observability import runtime
from topos.observability.http import TelemetryMiddleware
from topos.observability.tracing import start_span


@pytest.mark.parametrize('busy', [False, True])
def test_remote_worker_http_engine_chain_and_rejection_count(monkeypatch, busy):
    from topos.api import engine_run
    from topos.auth import require_api_key

    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tp = TracerProvider(shutdown_on_exit=False)
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    instance = runtime.Telemetry(tp, MeterProvider(metric_readers=[reader], shutdown_on_exit=False))
    monkeypatch.setattr(runtime, '_runtime', instance)

    class Adapter:
        def run_inference(self, payload, config):
            if busy:
                return {'status': 'deferred', 'error': 'PRIVATE_PROVIDER_TEXT',
                        'availability': ComputeUnavailable('compute_busy', 5).detail()}
            return {'text': 'PRIVATE_OUTPUT'}

    monkeypatch.setattr(engine, 'get_adapter_for_task', lambda _: Adapter())
    monkeypatch.setattr(engine, 'apply_blackhole_egress_policy', lambda task: (task, None))
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware)
    app.include_router(engine_run.router)
    app.dependency_overrides[require_api_key] = lambda: None
    server = TestClient(app)
    real_client = httpx.Client
    calls = []

    def forward(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer PRIVATE_AUTH'
        assert 'traceparent' in request.headers and 'baggage' not in request.headers
        response = server.post(request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    monkeypatch.setattr(client.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(forward), **kw))
    task = ProcessingTask(id='PRIVATE_ID', type='enrichment', subtype='topic_extraction',
        input={'text': 'PRIVATE_INPUT'}, model_request=ModelRequest(provider='ollama', model='fixture'))

    async def run():
        with start_span('caller'):
            selected = client.RemoteEngineClient('https://compute.invalid', api_key='PRIVATE_AUTH')
            return await asyncio.to_thread(selected.run, task)

    try:
        result = asyncio.run(run())
        assert result.status == ('deferred' if busy else 'completed')
        assert len(calls) == 1
        spans = {s.name: s for s in exporter.get_finished_spans()}
        names = ['caller', 'topos.engine.remote', 'POST /v1/engine/run', 'topos.engine.run', 'topos.engine.inference']
        assert set(names) == set(spans)
        for parent, child in zip(names, names[1:]):
            assert spans[child].parent.span_id == spans[parent].context.span_id
        assert len({s.context.trace_id for s in spans.values()}) == 1
        assert 'PRIVATE_' not in ''.join(s.to_json() for s in spans.values())
        metrics = [m for r in reader.get_metrics_data().resource_metrics for s in r.scope_metrics for m in s.metrics]
        assert 'PRIVATE_' not in str(metrics)
        rejections = [m for m in metrics if m.name == 'topos.compute.rejections']
        assert bool(rejections) == busy
        if busy:
            assert sum(p.value for p in rejections[0].data.data_points) == 1
            assert spans['topos.engine.remote'].attributes['error.type'] == 'compute_busy'
            assert spans['topos.engine.run'].attributes['error.type'] == 'compute_busy'
    finally:
        server.close()
        instance.shutdown()
