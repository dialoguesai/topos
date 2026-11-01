# Verification request lifetime

Candidate based on 1.5.36. This changes default-standard `verify_claim`, not the
separate `verify_claim_owner` reader-budget investigation.

## Behavior

A request has one monotonic budget covering executor wait, retrieval checkpoints,
model capability lookup, and model generation. The default is 60 seconds;
`deadline_s` accepts a finite positive number at most 120 seconds. These are
initial engineering limits, not a measured capacity or latency promise. Invalid
values produce `invalid_deadline_s`. Expiry produces typed `compute_unavailable`
with `deadline_exceeded`, not a late successful heuristic result. Ordinary
non-timeout fallback behavior is unchanged.

The local HTTP route listens for disconnect after reading the body and cancels its
handler. Cancellation signals the worker through a shared budget. Budgeted model
I/O uses a cancellable HTTP transport with a total elapsed deadline, including
header stalls and continuously trickling bodies. It does not automatically retry
cancelled requests. Existing thinking-capability negotiation is unchanged and
shares the same remaining budget. A bounded foreground request does not launch
or wait for provider service startup; the runtime owns that lifecycle.

## Database and compute remain separate

The database-side worker keeps its own connection and principal/guard. The Engine
interface and local/remote selection are unchanged. Remote Engine requests send
`X-Topos-Timeout-Ms` as remaining duration, never a foreign monotonic timestamp.
The compute host validates and applies that duration to its queue and worker.
Both nodes need this change for server-side propagation. An older remote node
still has the caller-side transport deadline but may continue server work.
No new access to owner data is granted to the compute host by this patch.

Expiry removes queued remote Engine work. A running Engine worker retains its
active slot until it actually returns, even after its caller stops waiting.
Budget checks prevent an expired worker from starting subsequent model calls.
Content-free worker terminal logging records elapsed time and whether the request
was stopped. It is a local-worker observation, not a provider-drain claim.

## Limits

Python cannot forcibly cancel a thread or arbitrary SQLite/native code. Retrieval
checks are cooperative; a blocked database call may outlive the caller. Closing a
socket does not prove that an Ollama or remote GPU operation has stopped. This
patch is not a global compute scheduler, priority policy, or provider cancellation
protocol. Shared admission and independent drain/reconciliation evidence remain
separate work. Existing requests outside the budgeted verification/Engine path
retain their prior timeout behavior.

## Evidence

Focused regression suite: 86 passed, including real loopback fake-provider tests
for stalled headers and trickling bodies, subsequent fresh requests, cancellation,
queue expiry, retained uncooperative worker ownership, remote duration propagation,
existing responsiveness, transport contracts, and timing context.

Complete ten-family paired offline quality gate: 662 pass / 2 fail before;
664 pass after, no skips or runner errors. The two new failures were cancellation
and deadline regressions. Published production behavior was restored after
exploratory checks to capture the frozen-test baseline; the new helper was present
but unwired. Exact reports and provenance are in response-quality-runs and the
response-quality changelog. This is synthetic contract evidence, not live-model
answer quality, latency, provider drain, or improvement over a live control.

Before release, review the combined candidate, run the paired response-quality
suite and targeted lifecycle tests, then commission isolated/pair live baselines
with fixed models and grant policy. Require service recovery probes, not just
health/version success. Preserve timeouts and unknown backend state in reports.

Rollback is a code revert; no database migration or provider configuration change.
