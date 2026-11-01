"""Hermetic SDK contract tests: real spans/metrics, synthetic data, no collector."""
import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from topos.observability import runtime
from topos.observability.metrics import record_metric, reset_metrics
from topos.observability.tracing import start_span, extract_context, inject_context

pytestmark = pytest.mark.public


@pytest.fixture
def telemetry(monkeypatch):
    exporter = InMemorySpanExporter()
    tp = TracerProvider(shutdown_on_exit=False)
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    instance = runtime.Telemetry(tp, mp)
    monkeypatch.setattr(runtime, "_runtime", instance)
    monkeypatch.setattr(runtime, "_configured", False)
    reset_metrics()
    yield exporter, reader
    instance.shutdown()


def metric_map(reader):
    data = reader.get_metrics_data()
    return {metric.name: metric for resource in data.resource_metrics
            for scope in resource.scope_metrics for metric in scope.metrics}


def test_duration_distribution_and_units(telemetry):
    record_metric("engine.inference_duration_ms", 100)
    record_metric("engine.inference_duration_ms", 300)
    metric = metric_map(telemetry[1])["topos.engine.inference.duration"]
    point = metric.data.data_points[0]
    assert metric.unit == "s"
    assert point.count == 2
    assert point.sum == pytest.approx(.4)
    assert point.min == .1 and point.max == .3


@pytest.mark.asyncio
async def test_parentage_threads_error_privacy_and_log_correlation(telemetry):
    from topos.core.logging import JsonFormatter

    def worker():
        with start_span("child"):
            record = logging.LogRecord("synthetic", logging.INFO, "", 0, "local only", (), None)
            return json.loads(JsonFormatter().format(record))

    with start_span("parent"):
        carrier = inject_context()
        logged = await asyncio.to_thread(worker)
    with pytest.raises(ValueError):
        with start_span("failed", context=extract_context(carrier)):
            raise ValueError("PRIVATE_SENTINEL")
    spans = {s.name: s for s in telemetry[0].get_finished_spans()}
    assert spans["child"].parent.span_id == spans["parent"].context.span_id
    assert logged["trace_id"] == f'{spans["parent"].context.trace_id:032x}'
    assert spans["failed"].context.trace_id == spans["parent"].context.trace_id
    assert spans["failed"].status.status_code == trace.StatusCode.ERROR
    assert spans["failed"].attributes["error.type"] == "ValueError"
    assert not spans["failed"].events
    assert "PRIVATE_SENTINEL" not in spans["failed"].to_json()


@pytest.mark.asyncio
async def test_http_templates_remote_parent_and_error_status(telemetry):
    from topos.observability.http import TelemetryMiddleware

    app = FastAPI()
    app.add_middleware(TelemetryMiddleware)

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    @app.get("/failure")
    def fail():
        raise ValueError("PRIVATE_EXCEPTION")

    parent = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://example") as client:
        assert (await client.get("/items/PRIVATE_PATH?key=PRIVATE_QUERY", headers={"traceparent": parent, "authorization": "PRIVATE_AUTH", "baggage": "secret=PRIVATE_BAGGAGE"})).status_code == 200
        assert (await client.get("/missing/PRIVATE_PATH")).status_code == 404
        assert (await client.get("/failure")).status_code == 500
    spans = telemetry[0].get_finished_spans()
    assert spans[0].name == "GET /items/{item_id}"
    assert spans[0].parent.span_id == int("2" * 16, 16)
    assert spans[1].name == "GET"
    assert spans[1].status.status_code == trace.StatusCode.UNSET
    assert spans[2].status.status_code == trace.StatusCode.ERROR
    assert "PRIVATE_" not in "".join(s.to_json() for s in spans)
    metric = metric_map(telemetry[1])["http.server.request.duration"]
    assert metric.unit == "s"
    assert sum(p.count for p in metric.data.data_points) == 3
    assert "PRIVATE_" not in str(metric)


@pytest.mark.parametrize("mode", ["completed", "failed", "deferred", "validation", "policy", "exception"])
def test_engine_outcomes_count_once_and_preserve_behavior(telemetry, monkeypatch, mode):
    from topos.engine.engine import Engine
    from topos.engine.tasks import ProcessingTask, ModelRequest
    import topos.engine.engine as module

    class Adapter:
        def run_inference(self, *args, **kwargs):
            if mode == "exception":
                raise ValueError("PRIVATE_INPUT")
            if mode == "failed":
                return {"error": "PRIVATE_INPUT"}
            if mode == "deferred":
                return {"status": "deferred"}
            return {"text": "PRIVATE_OUTPUT"}

    monkeypatch.setattr(module, "get_adapter_for_task", lambda task: Adapter())
    monkeypatch.setattr(module, "apply_blackhole_egress_policy", lambda task: (task, "PRIVATE_POLICY" if mode == "policy" else None))
    task = ProcessingTask(id="" if mode == "validation" else "PRIVATE_ID", type="enrichment", subtype="topic_extraction",
                          input={"content": "PRIVATE_INPUT"}, model_request=ModelRequest(provider="ollama", model="synthetic"))
    if mode == "exception":
        with pytest.raises(ValueError):
            Engine().run(task)
    else:
        result = Engine().run(task)
        assert result.status == ("failed" if mode in {"validation", "policy"} else mode)
    metrics = metric_map(telemetry[1])
    point = metrics["topos.engine.tasks"].data.data_points[0]
    assert point.value == 1
    assert point.attributes["topos.task.outcome"] == (mode if mode in {"completed", "deferred"} else "failed")
    assert metrics["topos.engine.task.duration"].data.data_points[0].count == 1
    assert ("topos.engine.inference.duration" in metrics) == (mode not in {"validation", "policy"})
    assert "PRIVATE_" not in "".join(s.to_json() for s in telemetry[0].get_finished_spans())
    if mode in {"failed", "exception"}:
        inference = next(s for s in telemetry[0].get_finished_spans() if s.name == "topos.engine.inference")
        assert inference.status.status_code == trace.StatusCode.ERROR


def test_runtime_disabled_and_opt_in_lifecycle(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("OTEL_") or key == "TOPOS_OTEL_ENABLED":
            monkeypatch.delenv(key)
    runtime.shutdown()
    assert not runtime.configure()
    monkeypatch.setenv("TOPOS_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert not runtime.configure()
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=resource-name,service.instance.id=synthetic-node")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "explicit-name")
    try:
        assert runtime.configure()
        first = runtime.current()
        assert runtime.configure() and runtime.current() is first
        attrs = first.tracer_provider.resource.attributes
        assert attrs["service.name"] == "explicit-name"
        assert attrs["service.instance.id"] == "synthetic-node"
        assert attrs["service.version"]
        runtime.shutdown()
        assert runtime.configure()
        assert runtime.current() is not first
    finally:
        runtime.shutdown()


def test_usage_identity_contains_no_credential(monkeypatch):
    from topos.engine.usage_observation import build_usage_observation_envelope, settings

    monkeypatch.setattr(settings, "topos_key", "PRIVATE_CREDENTIAL_SENTINEL")
    envelope = build_usage_observation_envelope(action="llm.generate", quantity=1,
                                               producer="engine", canonical_action_identity={"task": "synthetic"})
    assert envelope["topos_id"] == "engine"
    assert "PRIVATE_" not in json.dumps(envelope)


@pytest.mark.asyncio
async def test_rpc_remote_parent_worker_and_response_failure(telemetry, monkeypatch):
    from topos.control_plane_client import ControlPlaneClient

    async def handler(message):
        def work():
            with start_span("worker"):
                pass
        await asyncio.to_thread(work)
        return {"id": message["id"], "status": "error", "error": "PRIVATE_REPLY"}

    client = ControlPlaneClient(control_plane_url="ws://example/ws/engine", api_key="synthetic", handler=handler)
    sent = []

    async def send(ws, message):
        sent.append(message)
        return True

    monkeypatch.setattr(client, "_send_ws_json", send)
    carrier = {"traceparent": "00-" + "3" * 32 + "-" + "4" * 16 + "-01"}
    message = {"type": "PRIVATE_METHOD", "id": "PRIVATE_ID", "payload": {"text": "PRIVATE_INPUT"}, "trace_context": carrier}
    await client._handle_message(None, message)
    spans = telemetry[0].get_finished_spans()
    assert spans[1].name == "topos.rpc _OTHER"
    assert spans[1].parent.span_id == int("4" * 16, 16)
    assert spans[0].parent.span_id == spans[1].context.span_id
    assert spans[1].status.status_code == trace.StatusCode.ERROR
    assert "PRIVATE_" not in "".join(s.to_json() for s in spans)
    assert sent[0]["error"] == "PRIVATE_REPLY"  # Product response preserved.


def test_no_sdk_still_propagates_context(monkeypatch):
    monkeypatch.setattr(runtime, "_runtime", runtime.Telemetry())
    carrier = {"traceparent": "00-" + "5" * 32 + "-" + "6" * 16 + "-01"}
    with start_span("noop", context=extract_context(carrier)):
        assert inject_context() == carrier


def test_real_otlp_serialization_and_configured_endpoints(monkeypatch):
    """Real batch exporters and protobuf; capture HTTP at requests' transport."""
    import os
    import requests
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

    for key in list(os.environ):
        if key.startswith("OTEL_") or key == "TOPOS_OTEL_ENABLED":
            monkeypatch.delenv(key)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://metrics.invalid/custom")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "synthetic-service")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "60000")
    captured = []

    def send(session, request, **kwargs):
        captured.append((request.url, request.body))
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    runtime.shutdown()
    try:
        assert runtime.configure()
        with start_span("synthetic-operation"):
            record_metric("engine.inference_duration_ms", 125)
        assert runtime.current().tracer_provider.force_flush()
        assert runtime.current().meter_provider.force_flush()
        traces = [body for url, body in captured if url == "http://collector.invalid:4318/v1/traces"]
        metrics = [body for url, body in captured if url == "http://metrics.invalid/custom"]
        assert traces and metrics
        trace_request = ExportTraceServiceRequest.FromString(traces[0])
        metric_request = ExportMetricsServiceRequest.FromString(metrics[0])
        assert trace_request.resource_spans[0].scope_spans[0].spans[0].name == "synthetic-operation"
        attrs = {a.key: a.value.string_value for a in trace_request.resource_spans[0].resource.attributes}
        assert attrs["service.name"] == "synthetic-service"
        assert attrs["service.instance.id"]
        histogram = metric_request.resource_metrics[0].scope_metrics[0].metrics[0]
        assert histogram.unit == "s" and histogram.histogram.data_points[0].count == 1
    finally:
        runtime.shutdown()


def test_invalid_export_configuration_fails_open(monkeypatch, caplog):
    runtime.shutdown()
    monkeypatch.setenv("TOPOS_OTEL_ENABLED", "true")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "PRIVATE_INVALID_EXPORTER")
    assert not runtime.configure()
    with start_span("still-works"):
        pass
    assert "PRIVATE_INVALID_EXPORTER" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_lifespan_drains_telemetry_even_on_failure(monkeypatch, fails):
    from topos.observability.http import TelemetryMiddleware

    events = []
    monkeypatch.setattr(runtime, "configure", lambda: events.append("configure"))
    monkeypatch.setattr(runtime, "shutdown", lambda: events.append("shutdown"))

    async def app(scope, receive, send):
        events.append("app")
        if fails:
            raise RuntimeError("synthetic startup/shutdown failure")

    async def call():
        await TelemetryMiddleware(app)({"type": "lifespan"}, None, None)

    if fails:
        with pytest.raises(RuntimeError):
            await call()
    else:
        await call()
    assert events == ["configure", "app", "shutdown"]
