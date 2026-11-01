# Node observability

The node emits OpenTelemetry traces and metrics through an optional OTLP/HTTP
protobuf pipeline. Ordinary installs make no telemetry connection. Local JSON
logs include W3C `trace_id`, `span_id` and `trace_flags` when a context exists;
their bodies are not exported. Existing local audit and usage records remain
application records, not sampled telemetry.

## Enable export

Install `topos-node[telemetry]` (from source: `uv sync --extra telemetry`). Configure
the node process before starting it:

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=topos-node
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=development
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.1
topos-node
```

Use your collector's actual address. The SDK appends `/v1/traces` and `/v1/metrics`
to the general endpoint. Signal-specific `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` are exact URLs. `TOPOS_OTEL_ENABLED=true`
also explicitly enables export with SDK default endpoints. Export is disabled by
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
| `topos.compute.rejections` | Explicit provider availability responses, by fixed reason and Engine/generation path |

`topos.*` instruments are custom conventions, not claims of standardized GenAI
metrics. Legacy `get_metric()` scalar snapshots remain compatible; use the OTel
histograms for latency distributions. The legacy core metrics facade is not enabled for export by this port. Future
callers must use static names and bounded, content-free attributes.

HTTP accepts W3C Trace Context headers. CP messages may carry a top-level
`trace_context` object containing `traceparent`/`tracestate`. Invalid context starts
a fresh trace. Baggage is not extracted. Async tasks and `asyncio.to_thread`
inherit context; signed protocol payloads are never modified. Node-side tracing
works without CP instrumentation, but a continuous CP-to-node trace requires the
CP sender to inject this carrier. This change does not instrument the CP or every
outbound HTTP/database client.

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

## Scope of this port

Adapted from the working-tree implementation on `codex/topos-opentelemetry`
(base commit `1f478452`). The source checkout is preserved. This port retains the
runtime, HTTP/RPC/Engine spans, bounded metric vocabulary, JSON log correlation,
optional dependencies, credential-free fallback usage identity and SDK tests.
The independent outbox delivery/flush changes are not included.

The remote Engine client now injects W3C trace context into its HTTP headers;
its worker, client, server, Engine and inference spans share a trace. Task bodies
and policy decisions are unchanged. Explicit compute overload is marked on the
Engine and remote spans, with one rejection counter at the Engine or direct
LLM-service boundary. This is observability, not admission or higher capacity.

## Verification

`tests/topos/test_otel.py` checks real SDK spans, histogram distributions, W3C/thread
parentage, privacy sentinels, lifecycle configuration and OTLP protobuf output with
a mocked HTTP transport. `tests/engine/test_compute_trace_context.py` verifies worker-to-remote-Engine
parentage, a single overload observation and privacy sentinels. Run these with
the `dev` extra. They do not require a live node, collector or personal database.

References: [Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/),
[HTTP metrics](https://opentelemetry.io/docs/specs/semconv/http/http-metrics/),
[trace context in logs](https://opentelemetry.io/docs/specs/otel/compatibility/logging_trace_context/).
