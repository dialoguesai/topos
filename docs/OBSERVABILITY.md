# Node observability for development

## Product defaults

The default node must be fully functional without diagnostics configuration,
the telemetry extra, or a collector. Users are not expected to inspect logs or
operate observability tooling. Diagnostics must not introduce an onboarding step,
setup question, required consent decision, or readiness check. Keep collector
instructions in developer documentation, outside the normal installation flow.

OTel is primarily for developing and debugging the node. It is disabled by default;
without explicit enablement there are no SDK export workers, export queues, or
persistent OTel captures. Missing optional telemetry dependencies or invalid
configuration must not prevent the node from starting or serving requests.
Product-required operational state, health reporting, and accounting remain
independent of diagnostics. This change adds no local log sink or retention and
does not expand retention of existing product records.

Voluntary reports to Dialogues are a separate support feature, outside onboarding.
Choosing never to share must leave the node fully functional. That feature must
collect only the approved diagnostic fields for its explicit purpose and duration;
it must not enable continuous collection or retain a diagnostic history by default.

## Implementation

The node emits OpenTelemetry traces and metrics through an optional OTLP/HTTP
protobuf pipeline. Ordinary installs make no telemetry connection. Local JSON
logs include W3C `trace_id`, `span_id` and `trace_flags` when a context exists;
their bodies are not exported. Existing local audit and usage records remain
application records, not sampled telemetry.

These diagnostics belong to the node owner. They primarily support development
and debugging, with optional advanced operator troubleshooting on local or hosted nodes.
They are not a fleet-reporting channel for Topos. There is no Topos collection
endpoint, no automatic support upload, and no span/metric export over the CP socket.
Distributed defaults and hosting templates must not enable diagnostics or supply
an organization collector on the owner's behalf. Sharing a diagnostic capture
with developers is a separate, deliberate owner action.

## Enable export

Install `topos-node[telemetry]` (from source: `uv sync --extra telemetry`). Configure
the node process before starting it:

```sh
export TOPOS_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=topos-node
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=development
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.1
topos-node
```

Use your collector's actual address. The SDK appends `/v1/traces` and `/v1/metrics`
to the general endpoint. Signal-specific `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` are exact URLs. Both `TOPOS_OTEL_ENABLED=true`
and an explicit destination for every exported signal are required. Generic host
OTEL variables alone never enable diagnostics, and the SDK's implicit localhost
destination is not used. Disable unused signals with their exporter set to `none`.
Export is disabled by
`OTEL_SDK_DISABLED=true`; configuration takes effect on the next app lifespan.

Supported exporters are `OTEL_TRACES_EXPORTER=otlp|none` and
`OTEL_METRICS_EXPORTER=otlp|none`, defaulting to `otlp` once enabled. The supported
protocol is `http/protobuf`; requesting another protocol/exporter disables this
pipeline with a warning. Install and configuration failure must not block node
startup. The SDK handles OTLP HTTP headers, TLS certificates, compression,
timeouts, sampler settings, `OTEL_BSP_*`, and metric reader configuration.
Exporter queues/retries are bounded by SDK settings and are not durable storage.

The resource contains `service.name`, the installed `service.version`, and a
random per-lifespan `service.instance.id`. Standard resource environment variables
override these defaults (`OTEL_SERVICE_NAME` wins over resource attributes).
Use a non-secret identifier if explicitly overriding the instance ID.

## Signals and privacy

| Signal | Meaning |
| --- | --- |
| HTTP server spans | Route templates and methods; status and error class; no raw path/query, headers or body |
| `http.server.request.duration` | Histogram in seconds; stable HTTP semantic conventions and recommended buckets |
| `topos.rpc <method>` | Node-side CP message handling; known protocol methods only, otherwise `_OTHER` |
| `topos.rpc.server.duration` | Message handling histogram in seconds |
| `topos.engine.run` / `topos.engine.inference` | Task and adapter operations; no input/output/model prompts |
| `topos.engine.tasks` | Counter by completed/failed/deferred outcome; includes validation/policy failures and raised exceptions |
| `topos.engine.task.duration` | Total task histogram in seconds, including failures |
| `topos.engine.inference.duration` | Adapter invocation histogram in seconds, including failed/deferred invocations |
| `topos.engine.models.resident` | Last observed resident-model count |
| `topos.engine.process.memory.peak` | Last observed process peak RSS in bytes; the underlying `ru_maxrss` is not current RSS |
| `topos.engine.model.evictions` | Model-eviction counter |
| `topos.control_plane.outbox.dropped` | Counter by fixed queue name and capacity reason |

`topos.*` instruments are custom conventions, not claims of standardized GenAI
metrics. Legacy `get_metric()` scalar snapshots remain compatible; use the OTel
histograms for latency distributions. The core metrics facade now delegates to
OTel. Future callers must use static names and bounded, content-free attributes.

HTTP accepts W3C Trace Context headers. CP messages may carry a top-level
`trace_context` object containing `traceparent`/`tracestate`. Invalid context starts
a fresh trace. Baggage is not extracted. Async tasks and `asyncio.to_thread`
inherit context; signed protocol payloads are never modified. Node-side tracing
works without CP instrumentation. The companion control-plane change on
`codex/topos-context-propagation` adds HTTP/MCP ingress spans, per-attempt relay
client spans and carrier injection. Both changes must be installed for the full
CP-to-node chain. Neither instruments every outbound HTTP/database client.
Propagation carries correlation metadata, never an exporter configuration or
permission to collect. A remote request's sampled bit cannot enable a disabled
node exporter. Enabling diagnostics in the CP does not enable it on any node;
each node's owner independently controls its destination and whether to export.

Exception types and ERROR status are recorded, but exception messages and
stack traces are not. Task/request/source IDs, prompts, result text and credentials
are not emitted by the built-in instrumentation. No automatic application-log
export is installed, and `OTEL_LOGS_EXPORTER` does not enable one. Existing local
logs may contain sensitive content; any future collector log pipeline needs its
own deliberate redaction policy. Samplers affect traces, not durable usage records.

Providers belong to the app lifespan and drain after runtime work stops, including
startup/shutdown failure. Export work runs in SDK background threads; shutdown runs
off the ASGI loop. Node providers do not replace process-global providers belonging
to an embedding application. Use the node observability helpers for its pipeline.

## Outbox versus Offline Inbox

The **node outbox** buffers outgoing presence/progress/usage observations in memory.
Its bounded drop-oldest policy is intentional best effort: it preserves newer
observations and protects the socket from telemetry overload. The control plane's
`_enqueue_usage_observation` explicitly uses the same best-effort policy. The node
also records LLM usage locally; neither queue promises durable remote accounting.

The **Offline Inbox** is a separate control-plane service for incoming app writes.
It persists writes with delivery state, retention/expiry, idempotency, permission
rechecks, retries and reconciliation. None of those rules changes here.

A node flush previously cleared the whole outbox before sending and restored only
the first failed item, losing the unsent tail. Flushes now serialize and take one
item at a time. A failed/cancelled send restores the item ahead of the retained tail;
if newer arrivals filled capacity meanwhile, the older in-flight item is dropped
under the existing capacity policy. Cancellation after a remote send can cause a
retry (delivery is not exactly-once); usage events retain their idempotency keys.

The fallback usage envelope identity is now the non-secret `engine` marker rather
than a key prefix. The control-plane usage handler already derives trusted identity
from the authenticated connection, not that envelope hint.

## Verification

`tests/topos/test_otel.py` checks real SDK spans, histogram distributions, W3C/thread
parentage, privacy sentinels, lifecycle configuration and OTLP protobuf output with
a mocked HTTP transport. `tests/topos/test_outbox_telemetry.py` covers disconnection,
retry order, concurrent flushes, cancellation and bounded overflow. Run these with
the `dev` extra. They do not require a live node, collector or personal database.
The companion control-plane suite maintains the cross-service OTLP regression
through a real local Collector, covering streaming, shutdown delivery and exporter
failure. See that repository's `docs/TRACE_CONTEXT.md` for reproducible commands.

References: [Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/),
[HTTP metrics](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/),
[trace context in logs](https://opentelemetry.io/docs/specs/otel/compatibility/logging_trace_context/).

## Remote compute and overload correlation

The remote Engine client propagates W3C context across its configured HTTP endpoint. Worker, client, server and inference spans share a trace. Typed compute rejections are counted at the Engine or direct generation boundary without changing request bodies or policy decisions. `tests/engine/test_compute_trace_context.py` verifies parentage, a single overload observation and privacy sentinels.
