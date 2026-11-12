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

## No model residency

The gateway never loads, warms, unloads or pins a model. Starting it, reading
`/admission` and stopping it send the provider nothing: no probe, no model list,
no warm-up. A forwarded request is the caller's bytes unchanged; the gateway adds
no `keep_alive`, option or model of its own.

The provider reads a generate request with no prompt, or a chat request with no
messages, as "load this model" (with `keep_alive` 0, "unload it"), and a negative
`keep_alive` as "never unload it". Those are model management under a generation
route, so they are refused with a 400 (`model_residency_not_proxied`) before
admission: nothing is queued, journaled or sent. A blank prompt is refused the
same way, because it is the shape a warm-up takes. Generation with a finite
`keep_alive`, including 0 ("answer, then unload"), is forwarded. How long a model
stays loaded after a forwarded request is therefore the provider's own default or
the caller's finite value; the gateway sets no upper limit on that value.

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
non-200 provider replies and forced shutdown remain uncertain. One timeout is
shorter than the observation deadline: the gateway's HTTP client gives up after
60 seconds without a byte from the provider, so a non-streaming request whose
whole reply takes longer than that is recorded as uncertain even though the
300-second deadline has not passed. Streaming requests are bounded by the gap
between frames. This has not been commissioned against a real slow model. Even an HTTP error
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

## Beside what the node already does

The node has three mechanisms of its own. The gateway replaces none of them and
is not on any node request path; `tests/engine/test_provider_gateway_opt_in.py`
fails if a node module imports, names or configures `topos.compute`, if a fresh
node process loads one of its modules or serves one of its routes, or if the
package gains a command for it.

- **Engine HTTP worker budget** (`topos/engine/admission.py`, `POST /v1/engine/run`,
  `GET /v1/engine/admission`; see `docs/compute-overload-contract.md`). It bounds
  worker threads in the node process. The gateway bounds requests to one provider
  from a separate process, and has to hold a streaming response, which a thread
  pool of synchronous calls cannot. The two keep the same policy: the same
  defaults (1 active, 4 waiting, 30 seconds queued), the same accepted ranges,
  FIFO order, expiry before dispatch, a queued cancel that never dispatches, an
  active disconnect that keeps its slot, and 499 for a caller that hung up.
  `GET /admission` carries every key `GET /v1/engine/admission` does, with the
  same meaning, plus the gateway's own (`uncertain`, `epoch`, `storage_failed`,
  `timings`). The same test file pins the shared defaults, ranges and keys.
- **Overload outcomes** (`topos/engine/overload.py`). A gateway refusal is a
  `ComputeUnavailable`: `compute_busy` for a full or expired queue,
  `compute_unavailable` for a stopping or quarantined gateway, HTTP 503, no
  invented Retry-After. The reasons `queue_full`, `queue_expired` and `stopping`
  carry the code the Engine budget gives them. The response's `detail` is the
  object a node client already reads with `unavailable_from_detail`. One gap if
  the gateway were ever put in front of the node's own provider URL: the node's
  provider client reads a 503 as busy only when it carries the provider's own
  queue message, so it would report a gateway `compute_busy` as
  `compute_unavailable`.
- **Local serving rules** (`topos/local_serving.py`). Every model call the node
  makes goes through them: one context per model, a per-model slot with priority,
  and a watchdog that unloads only models the node itself has used. The gateway
  is not a node caller and takes no part in that. To the node, a model loaded by
  a request that came through the gateway is another client's model, which the
  watchdog leaves alone.
- **Request budgets** (`X-Topos-Timeout-Ms`, `topos/engine/request_budget.py`).
  The gateway does not read a caller's budget. Its queue and observation
  deadlines are its own.

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
`tests/engine/test_provider_gateway_opt_in.py` holds the separation, residency
and shared-vocabulary guards described above.
