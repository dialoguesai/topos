# Experimental provider admission gateway

This is an opt-in local compute service. It is not started by the normal TOPOS
node and does not change any existing provider, database, sharing or sanitization
routing. It provides a runnable boundary for controlled commissioning before
integrating real callers. It is not yet suitable as a drop-in Ollama replacement.

## Implemented boundary

Authenticated POST `/api/generate` and `/api/chat`, streaming or non-streaming,
share one active limit and bounded FIFO. Defaults are 1 active operation, 4
waiting, a 30-second queue deadline and a 300-second execution observation
deadline. These defaults are conservative policy choices, not measured capacity.
The observation deadline limits how long this gateway watches a request; it does
not cancel or prove termination of work at the provider.

Excess work receives a structured `compute_busy` 503 before provider dispatch.
Queue expiry also returns busy. Retry-After is omitted because no reliable retry
time is known. Queued cancellation prevents provider dispatch. Active cancellation
detaches the client while the gateway continues observing the provider.

Authenticated GET `/admission` reports active, waiting, detached and uncertain
counts, authority epoch and cumulative counters. It explicitly reports that
global GPU capacity and hardware drain are not established. Diagnostics contain
no prompts, replies, model names or provider exception text.
Counters and cumulative queue-wait/provider-observation times are per authority
epoch; unresolved ownership is persisted across epochs. Provider observation is
wall time at the gateway, not measured GPU execution time. Accepted responses
carry a generated `X-Topos-Operation-Id` for correlation.

The gateway accepts at most 1 MiB per request and 1 MiB per response JSON/frame.
Streaming subscribers have a 256 KiB buffer. A slow subscriber is detached rather
than allowing that buffer to grow or blocking provider observation; it receives
a safe error frame if still connected. Non-streaming JSON is assembled within
the frame limit. These are gateway buffer bounds, not a process RSS guarantee.
The first frame is forwarded without waiting for a large chunk to fill.

## Completion and persistence

Before sending a provider request, the authority commits its generated operation
ID and epoch to a private SQLite journal. Disk work runs off the event loop.
Neither request nor response bodies are persisted. A successful complete response
with boolean `done: true` is the provider terminal signal. Ollama documents `done`
as completion for [generate](https://docs.ollama.com/api/generate) and
[chat](https://docs.ollama.com/api/chat). This is not independent proof of GPU
idleness or proof of a useful answer. Real provider-version behavior still needs
commissioning.

EOF without completion, malformed/oversized responses, transport errors, timeouts,
non-200 provider replies and forced shutdown remain uncertain. Even an HTTP error
is treated conservatively until its execution implications are verified. Any
uncertain operation blocks new admissions and rejects queued work; already active
operations continue to be observed. This deliberately sacrifices availability
rather than claiming capacity has been recovered without evidence.

Unresolved journal rows survive process exit. Startup marks them uncertain and
does not dispatch. A POSIX file lock excludes another authority using the **same
state directory**; a resource fingerprint prevents accidental reuse for another
provider. There is no TTL reclamation, automatic replay or reset endpoint.
Do not delete the journal or choose a new directory to bypass uncertainty.
Provider reconciliation or a verified provider reset is a remaining commissioning
requirement; an automated recovery workflow is not implemented.

## Controlled launch

Use a fresh private state directory and a dedicated fixture provider, not an
owner's active provider. Supply a separately generated secret through
`TOPOS_PROVIDER_GATEWAY_TOKEN`; it must be at least 32 ASCII characters without
whitespace. Clients must send it as `Authorization: Bearer …`.

```sh
python -m topos.compute.gateway \
  --upstream http://127.0.0.1:11436 \
  --state-dir /absolute/private/fixture-provider-state \
  --port 11435 --active 1 --waiting 4
```

This first launcher supports POSIX, binds only `127.0.0.1`, uses one worker,
requires a literal loopback HTTP upstream, and disables redirects and environment
proxy configuration. It does not forward authorization to the provider. Model
management and arbitrary proxy routes are unavailable. Do not repoint existing
node settings at it: current caller authentication and management-route support
are not integrated. No remote-deployment configuration is introduced here.

## Remaining coverage and acceptance

Existing remote Engine/provider endpoints and protected local assessment routing
remain intact. A later remote compute authority must preserve their distinct
trust boundaries; a local gateway must never silently forward protected local
assessment data to a remote host.

This is one configured gateway's admission boundary. Direct provider clients,
another gateway with a different journal, native embeddings/NER/reranking/privacy
filter GPU work, external processes, model residency and management operations
are not globally governed. Enforcing provider access, resource identity and
native workload participation is required before claiming a whole-GPU bound.
Service fairness, cross-boundary deadlines, provider cancellation, automatic
reconciliation, remote auth/TLS and integration of caller credentials remain
separate follow-up work. Do not layer long independent queues without a shared
deadline policy.

`tests/engine/test_provider_gateway.py` uses controlled HTTP transports and real
ASGI disconnect messages. It tests mixed chat/generate saturation and recovery,
FIFO, cancellation, queue expiry, first-frame delivery, slow consumers, ambiguous
termination, restart, process exit, duplicate authority, persistence before
dispatch, storage failure, auth, size limits and refusal to redirect remotely.
It does not exercise real model cancellation or measure MacBook capacity.
