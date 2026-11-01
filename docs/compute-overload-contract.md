# Compute overload contract

When the inference provider refuses generation, callers need to distinguish
capacity pressure from a broken connection and from a usable answer. This change
preserves that distinction without adding a scheduler or changing provider URLs.

## Implemented

- Ollama generation HTTP 503 with its known `maximum pending requests exceeded`
  message becomes `compute_busy`. Other 503s become `compute_unavailable`;
  429 becomes `compute_rate_limited`. Other errors retain existing behavior.
- Provider bodies are not returned in these errors. A valid integer Retry-After
  from zero through 86400 seconds is retained. Absent, malformed, HTTP-date and
  out-of-range values remain unknown; no wait estimate is invented.
- Streaming and non-streaming LLM service errors retain HTTP 503/429. The
  control-plane message keeps numeric `error_code` and adds `error_type` and,
  when known, `retry_after_seconds`. An acknowledged stream may end in error;
  acknowledgment is not inference admission or success.
- The Engine adapter returns its existing `deferred` status with a content-free
  `output.availability` object. The HTTP Engine door uses 503/429 while carrying
  the same ProcessingResult. Updated remote clients reconstruct that result.
  Older clients may raise an HTTP error; deploy compatible clients before relying
  on typed overload across a split deployment. No task-schema fields are added.
- Standard verification propagates explicit overload instead of producing a
  heuristic answer. Its local HTTP door returns 503/429; its message door returns
  the same typed terminal error as generation. Other fallback behavior is unchanged.
- None of these paths automatically retries overload, including think-parameter
  adaptation. A caller must not resubmit an already accepted asynchronous job.

## Ownership and remaining architecture

The database continues to select local or remote Engine execution through
`TOPOS_ENGINE_SERVICE_URL`; inference uses `ENGINE_OLLAMA_BASE_URL`. Policy checks
and protected-data routing stay in their existing positions. No prompts are moved
to a different host by this change.

Admission belongs to the compute service shared by all callers. An in-process
semaphore in a database node would not bound other nodes or direct provider users.
The existing Engine queue is optional and does not cover all direct LLM paths.

Ollama already bounds one pending scheduler channel using `OLLAMA_MAX_QUEUE`,
but version 0.40.13 sends requests for an already usable runner directly to it.
Consequently that channel's size is not a bound on every inference request or a
deadline policy. Source: [versioned scheduler](https://github.com/ollama/ollama/blob/v0.40.13/server/sched.go).
Do not claim global admission from setting that variable alone.

A complete compute admission design must cover all generation ingress (including
streaming), cap active and waiting work, expire queued requests before dispatch,
preserve authenticated identity/priority, and retain resource ownership until
upstream termination is observed. A client timeout alone is not backend drain.
Recovery after a gateway restart must account for work still running upstream.
Cancellation and starvation bounds need separate tests. The remote Engine HTTP
handler still executes synchronous work directly and also needs responsiveness
and bounded-dispatch treatment before it can be the shared admission service.

The decision for this slice is to fix overload transport first. Do not introduce
a second production proxy or silently restart an externally managed provider to
change its queue limits. A later admission implementation must name its enforced
scope and identify all bypass routes before claiming a machine-wide guarantee.

## Validation and limits

`tests/engine/test_compute_overload.py` exercises actual local Engine execution,
the HTTP Engine route and remote client with controlled provider IO; streaming
and non-streaming errors; verification's no-heuristic path; and local/message
terminal responses. Synthetic policy overrides isolate transport tests; they do
not prove remote authentication enforcement.

`tests/engine/test_compute_overload_burst.py` offers 24 overlapping requests to a
controlled two-admission provider across Engine, streaming and non-streaming
paths. It expects two completions, 22 typed rejections, exactly one attempt per
request, then three successful recovery requests. The artificial limit is a test
fixture, not a discovered GPU capacity or a product admission controller.

Hosted control-plane retry behavior, app busy-state rendering, accepted routine
and shared-answer terminal statuses, real provider saturation, and backend drain
are not established by these tests. Busy must be reported separately from useful
completion; do not count expected overload responses as successful answers.

## Bounded Engine HTTP dispatch

`POST /v1/engine/run` runs Engine work in a dedicated worker pool instead of on
the HTTP event loop. Admission happens before executor submission. A FIFO holds
at most the configured number of waiting jobs; a single expiry observer removes
expired waiters even while every worker is blocked. Full and expired queues return
503 with the existing deferred ProcessingResult, the original task ID, and an
explicit compute availability code. No retry time is invented.

Configuration is read at application lifespan startup:

| Environment variable | Default | Meaning |
| --- | ---: | --- |
| `TOPOS_ENGINE_HTTP_ACTIVE` | 1 | Maximum outstanding Engine HTTP workers in this process (1–32). |
| `TOPOS_ENGINE_HTTP_WAITING` | 4 | Waiting jobs outside the executor (0–128; zero rejects immediately when all workers are occupied). |
| `TOPOS_ENGINE_HTTP_QUEUE_TIMEOUT_SEC` | 30 | Maximum queue wait before submission (greater than zero, at most 300 seconds). |
| `TOPOS_ENGINE_HTTP_SHUTDOWN_GRACE_SEC` | 5 | Time to await local worker drain during lifespan shutdown (0–30 seconds). |

These conservative defaults are policy choices, not measured hardware capacity.
The dispatcher does not implement service priority or model batching; task
priority hints do not change its FIFO order. Only authenticated requests can use
the endpoint or `GET /v1/engine/admission`, which exposes content-free counters,
current active/waiting/detached counts, and the configured bounds.

Queued cancellation removes work before dispatch. HTTP disconnects and handler
cancellation detach active callers while retaining worker capacity until the
worker returns. Context variables, including trace parentage and request policy
context, cross the thread boundary. Lifespan shutdown refuses new admissions,
rejects waiters, and reports any remaining workers. A subsequent lifespan on the
same application object refuses to replace a dispatcher that still has active
workers. This is not cross-process restart reconciliation.

**Scope:** this bounds Engine HTTP workers in one process. `LocalEngineClient`,
direct provider generation/streaming, background model work, other processes and
other provider clients can bypass it. Multiple ASGI workers each have their own
budget. Worker completion can mean an upstream timeout; it does not establish
that remote inference stopped. The diagnostic response therefore explicitly
reports that a global GPU bound and backend-drain knowledge are unavailable.
A hard process restart loses these in-memory counters and queued jobs. Durable
job recovery, shared admission ownership and provider cancellation acknowledgements
remain separate requirements before claiming a whole-compute capacity limit.

Database/compute routing and API-key enforcement are unchanged. A remote GPU
host remains configurable through the existing Engine service URL. No broker is
required for this endpoint-level execution limit.
