"""W3C context and content-free spans; exception messages may contain user data."""

from __future__ import annotations


from contextlib import contextmanager
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from .runtime import current

_propagator = TraceContextTextMapPropagator()


def extract_context(carrier):
    # Never propagate baggage, which can carry arbitrary user data.
    if not isinstance(carrier, dict):
        return Context()
    safe = {key: value for key in ("traceparent", "tracestate")
            if isinstance((value := carrier.get(key)), str) and len(value) <= 512}
    return _propagator.extract(safe, context=Context())


def inject_context():
    carrier = {}
    _propagator.inject(carrier)
    return carrier


def mark_error(error_type="operation_failed"):
    try:
        span = trace.get_current_span()
        span.set_attribute("error.type", error_type)
        span.set_status(trace.Status(trace.StatusCode.ERROR))
    except Exception:
        pass


@contextmanager
def start_span(name, *, context=None, kind=trace.SpanKind.INTERNAL, attributes=None):
    # No automatic exception events, descriptions or stack traces.
    try:
        span = current().tracer.start_span(name, context=context, kind=kind, attributes=attributes)
    except Exception:
        span = trace.NonRecordingSpan(trace.get_current_span(context).get_span_context())
    with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
        try:
            yield span
        except BaseException as exc:
            mark_error(type(exc).__name__)
            raise
        finally:
            try:
                span.end()
            except Exception:
                pass
