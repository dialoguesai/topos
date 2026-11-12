# Resilience candidate review — 1 November 2026

Scope: the node candidate based on 1.5.36 and the matching private benchmark
changes. Two independent reviewers checked verification/Engine lifecycle and
benchmark evidence; the coordinating reviewer checked sharing behavior, default
timeout policy, integration and the corrective diffs. A second independent pass
checked the new transport runtime. Shared provider admission remains excluded.

## Findings and corrections

1. **Default deadline could reduce useful completion.** Six previously successful
   stress verifications took 61–110s. The proposed 60s default would truncate
   them. Use a 120s default/maximum with explicit shorter caller opt-in. Preserve
   the slow-success regression and report timeouts separately from useful answers.
2. **Request-triggered local model startup was removed.** Restore it with budgeted
   startup-lock acquisition, health probes, launch timeout and polling. Cancelled
   work does not launch a new provider; remote URLs still do not launch locally.
3. **Per-request asyncio shutdown waited for stalled DNS.** Use a reusable owned
   stdlib HTTP loop and bounded native resolver resources. Cancelled HTTP callers
   can return while native DNS remains counted until actual completion. Repeated
   timeouts cannot create unlimited replacement resolver threads.
4. **Expiry during preprocessing could still dispatch a non-Ollama adapter.**
   Check the inherited budget before adapter construction and immediately before
   inference; return a typed deferred result. Cover local/native/remote provider
   selection using fake adapters without weakening the egress-policy gate.
5. **Remote termination reason was lost.** Preserve only the closed cancelled /
   deadline_exceeded enum in verification; unknown values remain unavailable and
   cannot become heuristic successes or leak arbitrary provider strings.
6. **Per-ask grant comparison was weaker than its snapshot fingerprint.** The
   private harness now uses one canonical projection of stable grant semantics
   for both. Select the frozen permission, reject drift/ambiguity, and do not
   treat unrelated grants or observation-only metadata as a changed policy.
7. **Slow preflight could consume the workload's credential lifetime.** Recheck
   both actual owner/recipient workload tokens after preflight, before dispatch.
   Preserve an incomplete report if the full window plus drain is not covered.
8. **Polling could accept a response beyond its deadline.** Bound ask/fetch by
   remaining overall time, preserve accepted IDs without resubmission, and keep
   late terminal/rejection observations while marking the client outcome timeout.

The first five apply to node code; the final three are benchmark corrections.
Polling overshoot predated this sprint but occurred in the rewritten block and
would undermine the next comparison, so it is fixed now. Initial reproductions
and failed reports remain preserved rather than replaced with only green runs.

## Scope and limits of the transport correction

This is process-local HTTP resource accounting, not the excluded global provider
admission prototype. Default limits are eight budgeted HTTP operations and four
native resolver jobs, with no waiting resolver submissions. Capacity is retained
until actual resource completion. It owns an explicit stdlib event loop, independent
of the application's uvloop/event-loop policy. Credentials and context remain
request-local; clients are closed, not shared across identities.

The close hook refuses new work permanently and never starts a replacement to
escape outstanding work. It is an explicit process-lifecycle hook, not wired into
individual FastAPI application lifespans (which may not own the process). Native
DNS cannot be forcibly killed and may delay interpreter exit. Cancel-resistant
HTTP cleanup retains a bounded slot and may make new requests busy. Those limits
and backend-drain uncertainty must be visible in future capacity assessments.
They do not establish provider/GPU cancellation, global fairness or recovery.

## Qualification

All corrections need the final integrated paired response-quality gate and
focused fake-transport tests after merge. Private review notes retain reproductions
and line references at the reviewed revision. This is code and synthetic lifecycle
review, not live model quality, capacity, permissions isolation or release approval.
No active node, model, grant, quota, or routine setting was changed for this review.

Next: freeze the corrected harness, collect the same-version control, qualify the
release artifact, then independent service baselines, pairs, the fixed focused
stress profile and per-service recovery. Keep the shared-admission experiment out.

Final integrated qualification: the frozen 705-test quality gate changed from
702 pass / 3 fail to 705 pass, with zero skips, regressions or runner errors.
The combined lifecycle/transport/sharing suite passed 396 tests in 57.15s (1624
existing deprecation warnings). Separate private benchmark suite passed 97 mocked
tests. These counts overlap; they are not additive unique coverage.

The second independent transport review found no new blocker: five targeted tests
and 160 concurrent fake calls across 20 lifetimes found no context leaks,
unreclaimed request slots or loop callback errors. This does not prove absence of
all races or native shutdown delay. The reviewed source remains a release candidate;
CI, live quality/control, artifact and published-node qualification are separate.
