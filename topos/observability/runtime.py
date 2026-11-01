"""Opt-in OTLP/HTTP runtime; no node data or application log export.

App-owned providers support repeated lifespans (global OTel setters are one-shot).
OTel Context is shared, so asyncio tasks/to_thread preserve parentage.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid

from opentelemetry import metrics, trace

logger = logging.getLogger(__name__)
_lock = threading.RLock()


class Telemetry:
    def __init__(self, tracer_provider=None, meter_provider=None):
        self.tracer_provider = tracer_provider or trace.NoOpTracerProvider()
        self.meter_provider = meter_provider or metrics.NoOpMeterProvider()
        self.tracer = self.tracer_provider.get_tracer("topos.node")
        self.meter = self.meter_provider.get_meter("topos.node")
        self.instruments = {}
        self.lock = threading.Lock()

    def instrument(self, kind, name, unit):
        key = (kind, name, unit)
        with self.lock:
            if key not in self.instruments:
                self.instruments[key] = getattr(self.meter, "create_" + kind)(name, unit=unit)
            return self.instruments[key]

    def shutdown(self):
        # SDK shutdown drains bounded queues. Run off the app event loop.
        for provider in (self.tracer_provider, self.meter_provider):
            try:
                shutdown = getattr(provider, "shutdown", None)
                if shutdown:
                    shutdown()
            except Exception:
                logger.warning("Telemetry shutdown failed")


_runtime = Telemetry()
_configured = False


def current() -> Telemetry:
    return _runtime


def configure() -> bool:
    """Owner-controlled diagnostics: explicit Topos opt-in AND export destination.

    Generic OTEL variables may be inherited from a hosting environment. They are
    configuration, never consent. No vendor endpoint or implicit localhost export.
    """
    global _runtime, _configured
    with _lock:
        if _configured:
            return True
        if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
            return False
        if os.getenv("TOPOS_OTEL_ENABLED", "").strip().lower() != "true":
            return False
        providers = []
        try:
            from opentelemetry.sdk.resources import Resource, OTELResourceDetector
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.metrics.view import View, ExplicitBucketHistogramAggregation
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from ..__version__ import __version__

            resource = Resource.create({
                "service.name": "topos-node", "service.version": __version__,
                "service.instance.id": str(uuid.uuid4()),
            }).merge(OTELResourceDetector().detect())
            choices = {}
            for signal in ("TRACES", "METRICS"):
                choice = os.getenv(f"OTEL_{signal}_EXPORTER", "otlp").strip().lower()
                if choice not in {"otlp", "none"}:
                    raise ValueError("Only otlp/none exporters are supported")
                protocol = os.getenv(f"OTEL_EXPORTER_OTLP_{signal}_PROTOCOL",
                                     os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"))
                if choice == "otlp" and protocol != "http/protobuf":
                    raise ValueError("Only http/protobuf is supported")
                if choice == "otlp" and not (
                    os.getenv(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT", "").strip()
                    or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
                ):
                    raise ValueError("An owner-selected endpoint is required for each exported signal")
                choices[signal] = choice
            tp = TracerProvider(resource=resource, shutdown_on_exit=False)
            providers.append(tp)
            if choices["TRACES"] == "otlp":
                tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            readers = []
            if choices["METRICS"] == "otlp":
                readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
            seconds_buckets = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10]
            views = [View(instrument_name="http.server.request.duration",
                          aggregation=ExplicitBucketHistogramAggregation(seconds_buckets))]
            for name in ("topos.engine.task.duration", "topos.engine.inference.duration", "topos.rpc.server.duration"):
                views.append(View(instrument_name=name, aggregation=ExplicitBucketHistogramAggregation(
                    seconds_buckets + [30, 60, 120, 300])))
            mp = MeterProvider(resource=resource, metric_readers=readers, shutdown_on_exit=False, views=views)
            providers.append(mp)
            _runtime = Telemetry(tp, mp)
            _configured = True
            return True
        except Exception:
            # Exception text may contain exporter credentials.
            logger.warning("Telemetry unavailable; check the telemetry extra and OTLP HTTP configuration")
            for provider in providers:
                try:
                    provider.shutdown()
                except Exception:
                    pass
            return False


def shutdown() -> None:
    global _runtime, _configured
    with _lock:
        previous = _runtime
        _runtime = Telemetry()
        _configured = False
    previous.shutdown()
