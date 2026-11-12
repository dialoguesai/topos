"""Node-side CP message spans; never modify signed request/response envelopes."""
import json
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

from opentelemetry.trace import SpanKind

from .metrics import measure
from .tracing import extract_context, start_span

try:
    _METHODS = frozenset(json.loads(
        (Path(__file__).parents[1] / "protocol" / "handled_message_types.json").read_text()
    )["handled_message_types"]) | {"ping", "connection_info"}
except (OSError, ValueError, KeyError, TypeError):
    # A missing/broken instrumentation vocabulary must not prevent RPC dispatch.
    _METHODS = frozenset()


@contextmanager
def observe_message(message):
    method = message.get("type")
    method = method if isinstance(method, str) and method in _METHODS else "_OTHER"
    attributes = {"topos.rpc.method": method}
    started = perf_counter()
    with start_span(f"topos.rpc {method}", kind=SpanKind.SERVER, attributes=attributes,
                    context=extract_context(message.get("trace_context"))):
        try:
            yield
        finally:
            measure("histogram", "topos.rpc.server.duration", perf_counter() - started,
                    unit="s", attributes=attributes)
