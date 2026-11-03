"""ASGI instrumentation with route templates, never URLs, headers or bodies."""
import asyncio
from time import perf_counter

from opentelemetry.trace import SpanKind

from .metrics import measure
from .tracing import extract_context, mark_error, start_span
from . import runtime

_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "CONNECT", "TRACE"})


class TelemetryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            runtime.configure()
            try:
                return await self.app(scope, receive, send)
            finally:
                # Also drains providers when startup or another shutdown hook fails.
                await asyncio.to_thread(runtime.shutdown)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        method = method if method in _METHODS else "_OTHER"
        scheme = scope.get("scheme", "http")
        attributes = {"http.request.method": method, "url.scheme": scheme if scheme in {"http", "https"} else "http"}
        carrier = {key.decode("ascii"): value.decode("latin1")
                   for key, value in scope.get("headers", []) if key in {b"traceparent", b"tracestate"}}
        status = None
        error = None
        started = perf_counter()

        async def traced_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        span_method = "HTTP" if method == "_OTHER" else method
        with start_span(span_method, kind=SpanKind.SERVER, context=extract_context(carrier)) as span:
            try:
                await self.app(scope, receive, traced_send)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                # Starlette resolves the route during dispatch; raw paths (including
                # unmatched routes) are never used as span names or metric dimensions.
                route = getattr(scope.get("route"), "path", None)
                if route:
                    attributes["http.route"] = route
                    span.update_name(f"{span_method} {route}")
                if status is not None:
                    attributes["http.response.status_code"] = status
                if error or (status is not None and status >= 500):
                    attributes["error.type"] = error or str(status)
                    mark_error(attributes["error.type"])
                span.set_attributes(attributes)
                measure("histogram", "http.server.request.duration", perf_counter() - started,
                        unit="s", attributes=attributes)
